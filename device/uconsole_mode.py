"""Core logic for the uConsole MT7961 three-mode interlock.

Coordinates three strictly separated modes: CLIENT, ROUTER_AP and
STABLE_BLUETOOTH.  This module holds the pure classification logic, the
immutable observed-state record, the immutable persisted-state schema and
client snapshot, and the atomic, strictly-validated StateStore.  Host
interaction, locks, mode transitions, external process execution and
service control live elsewhere.
"""

from __future__ import annotations

import errno
import fcntl
import ipaddress
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from pathlib import Path
from typing import TextIO

CLIENT = "CLIENT"
ROUTER_AP = "ROUTER_AP"
STABLE_BLUETOOTH = "STABLE_BLUETOOTH"
UNKNOWN = "UNKNOWN"

SETTLED = "SETTLED"
TRANSITIONING_TO_CLIENT = "TRANSITIONING_TO_CLIENT"
TRANSITIONING_TO_ROUTER_AP = "TRANSITIONING_TO_ROUTER_AP"
TRANSITIONING_TO_STABLE_BLUETOOTH = "TRANSITIONING_TO_STABLE_BLUETOOTH"
RECOVERING_TO_CLIENT = "RECOVERING_TO_CLIENT"

MODES = (CLIENT, ROUTER_AP, STABLE_BLUETOOTH, UNKNOWN)
PHASES = (
    SETTLED,
    TRANSITIONING_TO_CLIENT,
    TRANSITIONING_TO_ROUTER_AP,
    TRANSITIONING_TO_STABLE_BLUETOOTH,
    RECOVERING_TO_CLIENT,
)

MANAGEMENT_GATE_ALLOWED = 0
MANAGEMENT_GATE_DENIED = 20
MANAGEMENT_GATE_OBSERVATION_UNAVAILABLE = 21
MANAGEMENT_GATE_INTERNAL_ERROR = 22
MANAGEMENT_GATE_USAGE_ERROR = 64
_MANAGEMENT_GATE_COMMAND = "management-gate"
_MANAGEMENT_GATE_CODE_RE = re.compile(r"[a-z0-9_]+\Z", re.ASCII)

WIFI_IFACE = "wlan1"
WIFI_DRIVER = "mt7921u"
WIFI_USB_IFACE = "1-1.4.3:1.3"
BT_DRIVER = "btusb"
BT_USB_IFACES = ("1-1.4.3:1.0", "1-1.4.3:1.1", "1-1.4.3:1.2")
CONTROLLER_BT_ADDR = "38:7A:CC:84:A4:14"
AP_ADDR_CIDR = "192.168.77.1/24"
AP_SUBNET = "192.168.77.0/24"
AP_COMMENT = "uconsole-manual-ap"
AP_FIREWALL_RULES = (
    ("nat", "POSTROUTING", "-s 192.168.77.0/24 -o eth1 -m comment --comment uconsole-manual-ap -j MASQUERADE"),
    ("filter", "FORWARD", "-i wlan1 -o eth1 -m comment --comment uconsole-manual-ap -j ACCEPT"),
    ("filter", "FORWARD", "-i eth1 -o wlan1 -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment uconsole-manual-ap -j ACCEPT"),
)


PERMITTED_UNAVAILABLE_FACTS = (
    "hostapd_active",
    "dnsmasq_active",
    "wifi_driver_bound",
    "wlan1_exists",
    "wlan1_managed",
    "wireless_type",
    "ap_addr_present",
    "ap_rules_present",
    "bt_drivers_bound",
    "bt_controllers",
)


@dataclass(frozen=True)
class ObservedState:
    hostapd_active: bool = False
    dnsmasq_active: bool = False
    wifi_driver_bound: bool = False
    wlan1_exists: bool = False
    wlan1_managed: bool | None = None
    wireless_type: str | None = None
    ap_addr_present: bool = False
    ap_rules_present: bool = False
    gerald_stopped_or_blocked: bool = False
    bt_drivers_bound: tuple[bool, bool, bool] = (False, False, False)
    bt_controllers: tuple[tuple[str, str], ...] = ()
    unavailable_facts: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.unavailable_facts, tuple):
            raise ValueError(
                f"unavailable_facts must be a tuple, got {type(self.unavailable_facts).__name__}"
            )
        seen: set[str] = set()
        for fact in self.unavailable_facts:
            if fact not in PERMITTED_UNAVAILABLE_FACTS:
                raise ValueError(
                    f"unavailable_facts contains unknown fact name {fact!r}; "
                    f"permitted names are {PERMITTED_UNAVAILABLE_FACTS!r}"
                )
            if fact in seen:
                raise ValueError(
                    f"unavailable_facts contains duplicate fact name {fact!r}"
                )
            seen.add(fact)


def resolve_bt_controller(
    controllers: tuple[tuple[str, str], ...],
    address: str = CONTROLLER_BT_ADDR,
) -> str | None:
    """Resolve the controller index by address, never by a fixed HCI index."""
    for index, controller_addr in controllers:
        if controller_addr == address:
            return index
    return None


_CLIENT_TRUSTED_FACTS = (
    "hostapd_active",
    "dnsmasq_active",
    "wifi_driver_bound",
    "wlan1_exists",
    "wlan1_managed",
    "wireless_type",
)
_ROUTER_TRUSTED_FACTS = _CLIENT_TRUSTED_FACTS + ("ap_addr_present", "ap_rules_present")
_STABLE_TRUSTED_FACTS = (
    "hostapd_active",
    "dnsmasq_active",
    "wifi_driver_bound",
    "wlan1_exists",
    "bt_drivers_bound",
    "bt_controllers",
)


def classify_mode(observed: ObservedState) -> str:
    unavailable = frozenset(observed.unavailable_facts)
    bt_bound = observed.bt_drivers_bound == (True, True, True)
    bt_registered = resolve_bt_controller(observed.bt_controllers) is not None
    wlan_absent_or_unmanaged = (not observed.wlan1_exists) or (observed.wlan1_managed is False)

    if (
        not any(name in unavailable for name in _CLIENT_TRUSTED_FACTS)
        and not observed.hostapd_active
        and not observed.dnsmasq_active
        and observed.wifi_driver_bound
        and observed.wlan1_exists
        and observed.wlan1_managed is True
        and observed.wireless_type == "managed"
        and observed.gerald_stopped_or_blocked
    ):
        return CLIENT

    if (
        not any(name in unavailable for name in _ROUTER_TRUSTED_FACTS)
        and observed.hostapd_active
        and observed.dnsmasq_active
        and observed.wifi_driver_bound
        and observed.wlan1_exists
        and observed.wlan1_managed is False
        and observed.wireless_type == "AP"
        and observed.ap_addr_present
        and observed.ap_rules_present
        and observed.gerald_stopped_or_blocked
    ):
        return ROUTER_AP

    stable_trusted = not any(name in unavailable for name in _STABLE_TRUSTED_FACTS)
    if observed.wlan1_exists is True:
        stable_trusted = stable_trusted and "wlan1_managed" not in unavailable
    if (
        stable_trusted
        and not observed.hostapd_active
        and not observed.dnsmasq_active
        and wlan_absent_or_unmanaged
        and not observed.wifi_driver_bound
        and bt_bound
        and bt_registered
    ):
        return STABLE_BLUETOOTH

    return UNKNOWN


SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = "/var/lib/uconsole-mt7961-mode/state.json"

STATE_SCHEMA_FIELDS = (
    "schema_version",
    "generation",
    "boot_id",
    "phase",
    "current_mode",
    "source_mode",
    "target_mode",
    "last_completed_step",
    "client_snapshot",
    "updated_at",
)
SNAPSHOT_SCHEMA_FIELDS = ("connection_uuid", "nm_managed", "ip_forward")


@dataclass(frozen=True)
class ClientSnapshot:
    connection_uuid: str | None = None
    nm_managed: bool | None = None
    ip_forward: int | None = None


@dataclass(frozen=True)
class PersistedState:
    schema_version: int = SCHEMA_VERSION
    generation: int = 0
    boot_id: str = ""
    phase: str = SETTLED
    current_mode: str = UNKNOWN
    source_mode: str | None = None
    target_mode: str | None = None
    last_completed_step: str | None = None
    client_snapshot: ClientSnapshot = ClientSnapshot()
    updated_at: str = ""


@dataclass(frozen=True)
class StateValidationError:
    code: str
    detail: str
    field: str | None = None


@dataclass(frozen=True)
class StrictLoadOutcome:
    state: PersistedState
    existed: bool
    errors: tuple[StateValidationError, ...] = ()


@dataclass(frozen=True)
class LenientLoadOutcome:
    state: PersistedState
    existed: bool
    diagnostics: tuple[StateValidationError, ...] = ()


class StateStoreError(Exception):
    """Raised when persisting to an unsafe or invalid state path."""


def _validation_error(code: str, detail: str, field: str | None = None) -> StateValidationError:
    return StateValidationError(code=code, detail=detail, field=field)


def client_snapshot_to_dict(snapshot: ClientSnapshot) -> dict[str, object]:
    return {
        "connection_uuid": snapshot.connection_uuid,
        "nm_managed": snapshot.nm_managed,
        "ip_forward": snapshot.ip_forward,
    }


def persisted_state_to_dict(state: PersistedState) -> dict[str, object]:
    return {
        "schema_version": state.schema_version,
        "generation": state.generation,
        "boot_id": state.boot_id,
        "phase": state.phase,
        "current_mode": state.current_mode,
        "source_mode": state.source_mode,
        "target_mode": state.target_mode,
        "last_completed_step": state.last_completed_step,
        "client_snapshot": client_snapshot_to_dict(state.client_snapshot),
        "updated_at": state.updated_at,
    }


def persisted_state_from_dict(
    mapping: object,
) -> tuple[PersistedState, tuple[StateValidationError, ...]]:
    """Validate and convert a JSON-parsed mapping into a PersistedState.

    Performs every structural check.  The stale-boot-id comparison against
    the injected boot-id provider and the normal-transition non-SETTLED phase
    gate are context-specific and applied by the StateStore.
    """
    if not isinstance(mapping, dict):
        return PersistedState(), (
            _validation_error("not_object", "state data must be a JSON object"),
        )

    errors: list[StateValidationError] = []
    for key in mapping:
        if key not in STATE_SCHEMA_FIELDS:
            errors.append(_validation_error("unknown_field", f"unknown top-level field {key!r}", key))
    for key in STATE_SCHEMA_FIELDS:
        if key not in mapping:
            errors.append(_validation_error("missing_field", f"missing field {key!r}", key))

    schema_version = mapping.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        errors.append(_validation_error("schema_version", f"schema version must be exactly {SCHEMA_VERSION}", "schema_version"))

    generation = mapping.get("generation")
    if type(generation) is not int or generation < 0:
        errors.append(_validation_error("generation", "generation must be a non-negative integer", "generation"))

    boot_id = mapping.get("boot_id")
    if type(boot_id) is not str or boot_id == "":
        errors.append(_validation_error("boot_id", "boot_id must be a non-empty string", "boot_id"))

    phase = mapping.get("phase")
    if phase not in PHASES:
        errors.append(_validation_error("phase", f"phase must be one of {PHASES}", "phase"))

    current_mode = mapping.get("current_mode")
    if current_mode not in MODES:
        errors.append(_validation_error("current_mode", f"current_mode must be one of {MODES}", "current_mode"))

    source_mode = mapping.get("source_mode")
    if source_mode is not None and source_mode not in MODES:
        errors.append(_validation_error("source_mode", f"source_mode must be null or one of {MODES}", "source_mode"))

    target_mode = mapping.get("target_mode")
    if target_mode is not None and target_mode not in MODES:
        errors.append(_validation_error("target_mode", f"target_mode must be null or one of {MODES}", "target_mode"))

    last_completed_step = mapping.get("last_completed_step")
    if last_completed_step is not None and (
        type(last_completed_step) is not str or last_completed_step == ""
    ):
        errors.append(_validation_error("last_completed_step", "last_completed_step must be null or a non-empty string", "last_completed_step"))

    updated_at = mapping.get("updated_at")
    if type(updated_at) is not str or updated_at == "":
        errors.append(_validation_error("updated_at", "updated_at must be a non-empty string", "updated_at"))

    snapshot = mapping.get("client_snapshot")
    if not isinstance(snapshot, dict):
        errors.append(_validation_error("client_snapshot", "client_snapshot must be an object", "client_snapshot"))
        snapshot = {}
    else:
        for key in snapshot:
            if key not in SNAPSHOT_SCHEMA_FIELDS:
                errors.append(_validation_error("unknown_field", f"unknown client_snapshot field {key!r}", f"client_snapshot.{key}"))
        for key in SNAPSHOT_SCHEMA_FIELDS:
            if key not in snapshot:
                errors.append(_validation_error("missing_field", f"missing client_snapshot field {key!r}", f"client_snapshot.{key}"))

    connection_uuid = snapshot.get("connection_uuid")
    if connection_uuid is not None and (type(connection_uuid) is not str or connection_uuid == ""):
        errors.append(_validation_error("connection_uuid", "connection_uuid must be null or a non-empty string", "client_snapshot.connection_uuid"))

    nm_managed = snapshot.get("nm_managed")
    if nm_managed is not None and type(nm_managed) is not bool:
        errors.append(_validation_error("nm_managed", "nm_managed must be null or a boolean", "client_snapshot.nm_managed"))

    ip_forward = snapshot.get("ip_forward")
    if ip_forward is not None and (type(ip_forward) is not int or ip_forward not in (0, 1)):
        errors.append(_validation_error("ip_forward", "ip_forward must be null, 0 or 1", "client_snapshot.ip_forward"))

    state = PersistedState(
        schema_version=schema_version if type(schema_version) is int else SCHEMA_VERSION,
        generation=generation if type(generation) is int and generation >= 0 else 0,
        boot_id=boot_id if type(boot_id) is str else "",
        phase=phase if phase in PHASES else SETTLED,
        current_mode=current_mode if current_mode in MODES else UNKNOWN,
        source_mode=source_mode if source_mode is None or source_mode in MODES else None,
        target_mode=target_mode if target_mode is None or target_mode in MODES else None,
        last_completed_step=(
            last_completed_step
            if last_completed_step is None or (type(last_completed_step) is str and last_completed_step != "")
            else None
        ),
        client_snapshot=ClientSnapshot(
            connection_uuid=(
                connection_uuid
                if connection_uuid is None or (type(connection_uuid) is str and connection_uuid != "")
                else None
            ),
            nm_managed=nm_managed if nm_managed is None or type(nm_managed) is bool else None,
            ip_forward=(
                ip_forward
                if ip_forward is None or (type(ip_forward) is int and ip_forward in (0, 1))
                else None
            ),
        ),
        updated_at=updated_at if type(updated_at) is str and updated_at != "" else "",
    )
    return state, tuple(errors)


class StateStore:
    """Atomic, strictly-validated persisted state store.

    Writes go through a same-directory temporary file and os.replace.  Loads
    are fail-closed in strict mode and best-effort with diagnostics in
    lenient recovery mode.
    """

    DEFAULT_STATE_PATH = DEFAULT_STATE_PATH

    def __init__(
        self,
        state_path: object,
        expected_owner_uid: int,
        boot_id_provider: object,
        clock_provider: object,
    ):
        self.state_path = Path(state_path)
        self.expected_owner_uid = expected_owner_uid
        self._boot_id_provider = boot_id_provider
        self._clock_provider = clock_provider

    def default_state(self) -> PersistedState:
        return PersistedState()

    def _check_file_safety(self, st: os.stat_result) -> tuple[StateValidationError, ...]:
        if stat.S_ISLNK(st.st_mode):
            return (_validation_error("symlink", "state path must not be a symlink"),)
        if not stat.S_ISREG(st.st_mode):
            return (_validation_error("not_regular", "state path must be a regular file"),)
        errors = []
        if st.st_uid != self.expected_owner_uid:
            errors.append(_validation_error("wrong_owner", f"owner uid {st.st_uid} does not match expected uid {self.expected_owner_uid}"))
        if st.st_mode & 0o077:
            errors.append(_validation_error("unsafe_permissions", f"group or other permission bits present: {stat.S_IMODE(st.st_mode):04o}"))
        return tuple(errors)

    def _parse_raw(self, raw: bytes) -> tuple[object, tuple[StateValidationError, ...]]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            return None, (_validation_error("malformed_json", f"state file is not valid JSON: {exc}"),)
        if not isinstance(data, dict):
            return None, (_validation_error("not_object", "state file root must be a JSON object"),)
        return data, ()

    def load_strict(self) -> StrictLoadOutcome:
        try:
            st = os.lstat(self.state_path)
        except FileNotFoundError:
            return StrictLoadOutcome(state=self.default_state(), existed=False, errors=())
        except OSError as exc:
            return StrictLoadOutcome(state=self.default_state(), existed=True, errors=(_validation_error("stat_error", f"cannot stat state path: {exc}"),))

        errors = list(self._check_file_safety(st))
        if errors:
            return StrictLoadOutcome(state=self.default_state(), existed=True, errors=tuple(errors))

        try:
            raw = self.state_path.read_bytes()
        except OSError as exc:
            return StrictLoadOutcome(state=self.default_state(), existed=True, errors=(_validation_error("read_error", f"cannot read state file: {exc}"),))

        data, parse_errors = self._parse_raw(raw)
        if parse_errors:
            return StrictLoadOutcome(state=self.default_state(), existed=True, errors=parse_errors)

        state, structural = persisted_state_from_dict(data)
        errors = list(structural)
        if not errors:
            current_boot = self._boot_id_provider()
            if state.boot_id == "" or state.boot_id != current_boot:
                errors.append(_validation_error("stale_boot_id", f"boot_id {state.boot_id!r} does not match current boot id {current_boot!r}", "boot_id"))
            if state.phase != SETTLED:
                errors.append(_validation_error("non_settled_phase", f"phase must be {SETTLED} for normal-transition loading", "phase"))
        if errors:
            return StrictLoadOutcome(state=self.default_state(), existed=True, errors=tuple(errors))
        return StrictLoadOutcome(state=state, existed=True, errors=())

    def load_lenient(self) -> LenientLoadOutcome:
        try:
            st = os.lstat(self.state_path)
        except FileNotFoundError:
            return LenientLoadOutcome(state=self.default_state(), existed=False, diagnostics=())
        except OSError as exc:
            return LenientLoadOutcome(state=self.default_state(), existed=True, diagnostics=(_validation_error("stat_error", f"cannot stat state path: {exc}"),))

        diagnostics = list(self._check_file_safety(st))
        if diagnostics:
            return LenientLoadOutcome(state=self.default_state(), existed=True, diagnostics=tuple(diagnostics))

        try:
            raw = self.state_path.read_bytes()
        except OSError as exc:
            return LenientLoadOutcome(state=self.default_state(), existed=True, diagnostics=(_validation_error("read_error", f"cannot read state file: {exc}"),))

        data, parse_errors = self._parse_raw(raw)
        if parse_errors:
            return LenientLoadOutcome(state=self.default_state(), existed=True, diagnostics=parse_errors)

        state, structural = persisted_state_from_dict(data)
        if structural:
            return LenientLoadOutcome(state=self.default_state(), existed=True, diagnostics=structural)

        current_boot = self._boot_id_provider()
        if state.boot_id == "" or state.boot_id != current_boot:
            diagnostics.append(_validation_error("stale_boot_id", f"boot_id {state.boot_id!r} does not match current boot id {current_boot!r}", "boot_id"))
        if state.phase != SETTLED:
            diagnostics.append(_validation_error("non_settled_phase", f"phase is {state.phase!r}, not {SETTLED}", "phase"))
        return LenientLoadOutcome(state=state, existed=True, diagnostics=tuple(diagnostics))

    def _ensure_private_parent(self, parent: Path) -> None:
        missing: list[Path] = []
        probe = parent
        while not probe.exists() and probe != probe.parent:
            missing.append(probe)
            probe = probe.parent
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            os.chmod(directory, 0o700)
        if parent.exists():
            os.chmod(parent, 0o700)

    def save(self, state: PersistedState) -> None:
        current_boot = self._boot_id_provider()
        updated_at = self._clock_provider().isoformat()
        to_write = replace(state, boot_id=current_boot, updated_at=updated_at)

        _, validation_errors = persisted_state_from_dict(persisted_state_to_dict(to_write))
        if validation_errors:
            raise StateStoreError("; ".join(f"{e.code}: {e.detail}" for e in validation_errors))

        parent = self.state_path.parent
        try:
            st = os.lstat(self.state_path)
        except FileNotFoundError:
            pass
        else:
            errors = self._check_file_safety(st)
            if errors:
                raise StateStoreError("; ".join(f"{e.code}: {e.detail}" for e in errors))

        self._ensure_private_parent(parent)

        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=f".{self.state_path.name}.", suffix=".tmp", dir=parent)
            tmp_path = Path(tmp_name)
            os.chmod(tmp_path, 0o600)
            content = (
                json.dumps(persisted_state_to_dict(to_write), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                + "\n"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
            tmp_name = None
            parent_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
            raise

        st = os.lstat(self.state_path)
        if not stat.S_ISREG(st.st_mode):
            raise StateStoreError("after write, state path is not a regular file")
        if st.st_uid != self.expected_owner_uid:
            raise StateStoreError(f"after write, owner uid {st.st_uid} does not match expected {self.expected_owner_uid}")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise StateStoreError(f"after write, state file mode is {stat.S_IMODE(st.st_mode):04o}, expected 0600")


MODE_LOCK_PATH = "/run/lock/uconsole-mt7961-mode.lock"
SAFE_MANAGEMENT_IFACE = "eth1"


class ModeLockError(RuntimeError):
    pass


class ModeLockBusy(ModeLockError):
    pass


class ModeLockUnsafe(ModeLockError):
    pass


class ModeLock:
    def __init__(
        self,
        path: str | os.PathLike[str],
        expected_owner_uid: int,
    ) -> None:
        self._path = Path(path)
        self._expected_owner_uid = expected_owner_uid
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise ModeLockError("lock already held")
        if os.geteuid() != self._expected_owner_uid:
            raise ModeLockUnsafe(
                f"current effective uid {os.geteuid()} does not match expected owner uid {self._expected_owner_uid}"
            )

        parent = self._path.parent
        try:
            parent_st = os.lstat(parent)
        except OSError as exc:
            raise ModeLockUnsafe(f"cannot stat lock parent {parent}: {exc}") from exc
        if stat.S_ISLNK(parent_st.st_mode):
            raise ModeLockUnsafe(f"lock parent {parent} is a symlink")
        if not stat.S_ISDIR(parent_st.st_mode):
            raise ModeLockUnsafe(f"lock parent {parent} is not a directory")

        try:
            st = os.lstat(self._path)
        except FileNotFoundError:
            st = None
        except OSError as exc:
            raise ModeLockUnsafe(f"cannot stat lock path {self._path}: {exc}") from exc
        else:
            if stat.S_ISLNK(st.st_mode):
                raise ModeLockUnsafe(f"lock path {self._path} is a symlink")
            if not stat.S_ISREG(st.st_mode):
                raise ModeLockUnsafe(f"lock path {self._path} is not a regular file")
            if st.st_uid != self._expected_owner_uid:
                raise ModeLockUnsafe(
                    f"lock path {self._path} is owned by uid {st.st_uid}, expected {self._expected_owner_uid}"
                )
            if stat.S_IMODE(st.st_mode) != 0o600:
                raise ModeLockUnsafe(
                    f"lock path {self._path} mode is {stat.S_IMODE(st.st_mode):04o}, expected 0600"
                )

        fd: int | None = None
        try:
            if st is None:
                try:
                    fd = os.open(
                        self._path,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o600,
                    )
                except FileExistsError as exc:
                    raise ModeLockUnsafe(
                        f"lock path {self._path} was created concurrently; refusing to acquire"
                    ) from exc
                except OSError as exc:
                    raise ModeLockUnsafe(f"cannot create lock path {self._path}: {exc}") from exc
                os.fchmod(fd, 0o600)
            else:
                try:
                    fd = os.open(
                        self._path,
                        os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                    )
                except OSError as exc:
                    raise ModeLockUnsafe(f"cannot open lock path {self._path}: {exc}") from exc

            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                raise ModeLockUnsafe(f"lock path {self._path} is not a regular file")
            if fst.st_uid != self._expected_owner_uid:
                raise ModeLockUnsafe(
                    f"lock path {self._path} is owned by uid {fst.st_uid}, expected {self._expected_owner_uid}"
                )
            if stat.S_IMODE(fst.st_mode) != 0o600:
                raise ModeLockUnsafe(
                    f"lock path {self._path} mode is {stat.S_IMODE(fst.st_mode):04o}, expected 0600"
                )
            if st is None:
                try:
                    created_st = os.lstat(self._path)
                except FileNotFoundError as exc:
                    raise ModeLockUnsafe(f"lock path {self._path} disappeared after creation") from exc
                if (created_st.st_dev, created_st.st_ino) != (fst.st_dev, fst.st_ino):
                    raise ModeLockUnsafe(f"lock path {self._path} changed after creation")
            elif (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                raise ModeLockUnsafe(f"lock path {self._path} changed during acquisition")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ModeLockBusy(f"lock path {self._path} is already held: {exc}") from exc
                raise ModeLockError(f"flock on lock path {self._path} failed: {exc}") from exc
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ModeLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@dataclass(frozen=True)
class ManagementPathObservation:
    interface_name: str
    interface_exists: bool
    operstate: str | None
    carrier: bool | None
    addresses: tuple[str, ...]
    default_route_interface: str | None
    ssh_connection_present: bool
    ssh_peer_address: str | None
    peer_route_interface: str | None
    local_console: bool


@dataclass(frozen=True)
class ManagementGateError:
    code: str
    detail: str
    field: str | None = None


@dataclass(frozen=True)
class ManagementGateResult:
    allowed: bool
    errors: tuple[ManagementGateError, ...]


def _parse_ip_address(value: str):
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _usable_unicast_addresses(
    addresses: tuple[str, ...],
) -> tuple[list[ManagementGateError], bool]:
    errors: list[ManagementGateError] = []
    usable = False
    for index, address in enumerate(addresses):
        if not isinstance(address, str):
            errors.append(
                ManagementGateError("malformed_address", f"address {index} is not a string", f"addresses[{index}]")
            )
            continue
        try:
            parsed = ipaddress.ip_interface(address)
        except ValueError:
            errors.append(
                ManagementGateError("malformed_address", f"address {address!r} is malformed", f"addresses[{index}]")
            )
            continue
        address_ip = parsed.ip
        if (
            address_ip.is_unspecified
            or address_ip.is_loopback
            or address_ip.is_multicast
            or address_ip.is_link_local
        ):
            continue
        usable = True
    if not usable:
        errors.append(
            ManagementGateError("no_usable_address", "no usable unicast address is present", "addresses")
        )
    return errors, usable


def evaluate_management_path(
    observed: ManagementPathObservation,
    required_interface: str = SAFE_MANAGEMENT_IFACE,
) -> ManagementGateResult:
    errors: list[ManagementGateError] = []

    if not isinstance(required_interface, str) or required_interface == "":
        errors.append(
            ManagementGateError("invalid_required_interface", "required_interface must be a non-empty string", "required_interface")
        )

    if observed.interface_name != required_interface:
        errors.append(
            ManagementGateError("wrong_interface", f"interface is {observed.interface_name!r}, required {required_interface!r}", "interface_name")
        )

    if observed.interface_exists is not True:
        errors.append(
            ManagementGateError("interface_missing", "management interface does not exist", "interface_exists")
        )

    if observed.operstate != "up":
        errors.append(
            ManagementGateError("operstate_not_up", f"operstate is {observed.operstate!r}, required 'up'", "operstate")
        )

    if observed.carrier is not True:
        errors.append(
            ManagementGateError("carrier_unproven", "carrier must be proven True", "carrier")
        )

    address_errors, _usable = _usable_unicast_addresses(observed.addresses)
    errors.extend(address_errors)

    if observed.default_route_interface != required_interface:
        errors.append(
            ManagementGateError("default_route_wrong", f"default route uses {observed.default_route_interface!r}, required {required_interface!r}", "default_route_interface")
        )

    ssh = observed.ssh_connection_present is True
    local = observed.local_console is True
    if ssh and local:
        errors.append(
            ManagementGateError("session_ambiguous", "both SSH session and local console are active", "session")
        )
    elif not ssh and not local:
        errors.append(
            ManagementGateError("management_session_unproven", "no management session is proven", "session")
        )

    if ssh:
        peer = observed.ssh_peer_address
        if not isinstance(peer, str) or peer == "":
            errors.append(
                ManagementGateError("ssh_peer_missing", "SSH peer address is missing", "ssh_peer_address")
            )
        else:
            parsed = _parse_ip_address(peer)
            if parsed is None:
                errors.append(
                    ManagementGateError("ssh_peer_malformed", f"SSH peer address {peer!r} is malformed", "ssh_peer_address")
                )
        if observed.peer_route_interface != required_interface:
            errors.append(
                ManagementGateError("ssh_peer_route_wrong", f"SSH peer route uses {observed.peer_route_interface!r}, required {required_interface!r}", "peer_route_interface")
            )

    if local and (observed.ssh_peer_address is not None or observed.peer_route_interface is not None):
        errors.append(
            ManagementGateError("local_console_has_ssh_facts", "local console must not carry SSH peer facts", "session")
        )

    return ManagementGateResult(allowed=not errors, errors=tuple(errors))


# ---------------------------------------------------------------------------
# Read-only observation core (host-neutral, injected I/O only).
# ---------------------------------------------------------------------------

WIFI_DRIVER_LINK = f"/sys/bus/usb/devices/{WIFI_USB_IFACE}/driver"
BT_DRIVER_LINKS = tuple(
    f"/sys/bus/usb/devices/{interface}/driver" for interface in BT_USB_IFACES
)

NM_MANAGED_TRUE_VALUES = ("yes", "true", "1")
NM_MANAGED_FALSE_VALUES = ("no", "false", "0")

SERVICE_INACTIVE_STATES = (
    "inactive",
    "failed",
    "activating",
    "deactivating",
    "unknown",
    "not-found",
)

OPERSTATE_VALUES = (
    "up",
    "down",
    "unknown",
    "dormant",
    "lowerlayerdown",
    "notpresent",
    "testing",
)


@dataclass(frozen=True)
class ReadOnlyCommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ReadOnlyHostIO:
    run_command: Callable[[tuple[str, ...], float], ReadOnlyCommandResult]
    read_text: Callable[[str], str]
    read_link: Callable[[str], str]
    path_exists: Callable[[str], bool]
    get_environment: Callable[[str], str | None]
    local_console_present: Callable[[], bool]


class ReadOnlyHostIOPolicyError(RuntimeError):
    """The requested host access is outside the read-only adapter policy."""


class ReadOnlyHostIOExecutionError(RuntimeError):
    """An allowed read-only host operation could not be completed safely."""


LIVE_COMMAND_TIMEOUT_MAX = 30.0
LIVE_EXECUTABLE_SEARCH_PATH = (
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
LIVE_COMMAND_ENVIRONMENT = MappingProxyType({
    "PATH": LIVE_EXECUTABLE_SEARCH_PATH,
    "LC_ALL": "C",
    "LANG": "C",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PAGER": "cat",
    "SYSTEMD_PAGER": "cat",
    "SYSTEMD_COLORS": "0",
})

_LIVE_TEXT_PATHS = frozenset(
    {
        f"/sys/class/net/{SAFE_MANAGEMENT_IFACE}/operstate",
        f"/sys/class/net/{SAFE_MANAGEMENT_IFACE}/carrier",
    }
)
_LIVE_LINK_PATHS = frozenset((WIFI_DRIVER_LINK, *BT_DRIVER_LINKS))
_LIVE_SHELL_TOKENS = frozenset((";", "&&", "||", "|", ">", ">>", "<", "$()", "`"))


def _live_static_commands() -> frozenset[tuple[str, ...]]:
    commands = {
        ("systemctl", "is-active", "hostapd.service"),
        ("systemctl", "is-active", "dnsmasq.service"),
        ("ip", "-o", "link", "show", "dev", WIFI_IFACE),
        ("ip", "-o", "link", "show", "dev", SAFE_MANAGEMENT_IFACE),
        ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", WIFI_IFACE),
        ("iw", "dev", WIFI_IFACE, "info"),
        ("ip", "-o", "-4", "addr", "show", "dev", WIFI_IFACE),
        ("ip", "-o", "addr", "show", "dev", SAFE_MANAGEMENT_IFACE),
        ("ip", "route", "show", "default"),
        ("btmgmt", "info"),
    }
    for table, chain, spec in AP_FIREWALL_RULES:
        argv = ["iptables"]
        if table != "filter":
            argv.extend(("-t", table))
        argv.extend(("-C", chain))
        argv.extend(spec.split())
        commands.add(tuple(argv))
    return frozenset(commands)


_LIVE_STATIC_COMMANDS = _live_static_commands()


def _validate_live_timeout(timeout: object) -> float:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0 or timeout > LIVE_COMMAND_TIMEOUT_MAX:
        raise ReadOnlyHostIOPolicyError("invalid command timeout")
    return float(timeout)


def _validate_live_argv(argv: object) -> tuple[str, ...]:
    if type(argv) is not tuple or not argv or any(type(item) is not str or item == "" for item in argv):
        raise ReadOnlyHostIOPolicyError("argv must be a non-empty tuple of non-empty plain strings")
    if any(item in _LIVE_SHELL_TOKENS for item in argv):
        raise ReadOnlyHostIOPolicyError("shell metacharacter is forbidden")
    canonical = tuple(argv)
    if canonical in _LIVE_STATIC_COMMANDS:
        return canonical
    if len(canonical) == 4 and canonical[:3] == ("ip", "route", "get"):
        peer = canonical[3]
        if any(char.isspace() for char in peer) or "/" in peer or "%" in peer:
            raise ReadOnlyHostIOPolicyError("invalid route peer")
        try:
            ipaddress.ip_address(peer)
        except ValueError as exc:
            raise ReadOnlyHostIOPolicyError("route peer must be an IP literal") from exc
        return canonical
    raise ReadOnlyHostIOPolicyError("command is outside the read-only allowlist")


def _resolve_live_executable(executable: str) -> str:
    try:
        resolved = shutil.which(executable, path=LIVE_EXECUTABLE_SEARCH_PATH)
    except OSError as exc:
        raise ReadOnlyHostIOExecutionError("executable resolution failed") from exc
    if not isinstance(resolved, str) or not os.path.isabs(resolved) or os.path.basename(resolved) != executable:
        raise ReadOnlyHostIOExecutionError("authorized executable was not resolved safely")
    return resolved


def _live_run_command(argv: tuple[str, ...], timeout: float) -> ReadOnlyCommandResult:
    canonical = _validate_live_argv(argv)
    validated_timeout = _validate_live_timeout(timeout)
    privileged_firewall = canonical in _LIVE_PRIVILEGED_FIREWALL_COMMANDS
    if privileged_firewall:
        try:
            resolved_sudo = _resolve_live_executable("sudo")
            resolved_iptables = _resolve_live_executable("iptables")
        except ReadOnlyHostIOExecutionError as exc:
            raise ReadOnlyHostIOExecutionError(
                f"authorized firewall command {canonical!r} executable resolution failed"
            ) from exc
        executed_argv = (
            resolved_sudo,
            "-n",
            "--",
            resolved_iptables,
            *canonical[1:],
        )
    else:
        resolved = _resolve_live_executable(canonical[0])
        executed_argv = (resolved, *canonical[1:])
    process_kwargs = {
        "shell": False,
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": validated_timeout,
        "env": dict(LIVE_COMMAND_ENVIRONMENT),
        "cwd": "/",
        "close_fds": True,
    }
    if canonical == ("btmgmt", "info"):
        process_kwargs["input"] = ""
    else:
        process_kwargs["stdin"] = subprocess.DEVNULL
    try:
        completed = subprocess.run(
            executed_argv,
            **process_kwargs,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ReadOnlyHostIOExecutionError(
            f"authorized command {canonical!r} execution failed"
        ) from exc
    if type(completed.returncode) is not int or type(completed.stdout) is not str or type(completed.stderr) is not str:
        raise ReadOnlyHostIOExecutionError("command completion data is malformed")
    return ReadOnlyCommandResult(canonical, completed.returncode, completed.stdout, completed.stderr)


def _live_read_text(path: str) -> str:
    if type(path) is not str or path not in _LIVE_TEXT_PATHS:
        raise ReadOnlyHostIOPolicyError("text path is outside the read-only allowlist")
    try:
        value = Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        raise ReadOnlyHostIOExecutionError("authorized text read failed") from exc
    if type(value) is not str:
        raise ReadOnlyHostIOExecutionError("text read returned malformed data")
    return value


def _live_read_link(path: str) -> str:
    if type(path) is not str or path not in _LIVE_LINK_PATHS:
        raise ReadOnlyHostIOPolicyError("link path is outside the read-only allowlist")
    try:
        value = os.readlink(path)
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ReadOnlyHostIOExecutionError("authorized link read failed") from exc
    if type(value) is not str:
        raise ReadOnlyHostIOExecutionError("link read returned malformed data")
    return value


def _live_path_exists(path: str) -> bool:
    if type(path) is not str or path not in _LIVE_TEXT_PATHS | _LIVE_LINK_PATHS:
        raise ReadOnlyHostIOPolicyError("existence path is outside the read-only allowlist")
    try:
        value = os.path.exists(path)
    except Exception as exc:
        raise ReadOnlyHostIOExecutionError("authorized existence check failed") from exc
    if type(value) is not bool:
        raise ReadOnlyHostIOExecutionError("existence check returned malformed data")
    return value


def _live_get_environment(key: str) -> str | None:
    if type(key) is not str or key != "SSH_CONNECTION":
        raise ReadOnlyHostIOPolicyError("environment key is outside the read-only allowlist")
    try:
        value = os.environ.get("SSH_CONNECTION")
    except Exception as exc:
        raise ReadOnlyHostIOExecutionError("authorized environment read failed") from exc
    if value is not None and type(value) is not str:
        raise ReadOnlyHostIOExecutionError("environment value is malformed")
    return value


_LOCAL_TTY_RE = re.compile(r"^/dev/tty[0-9]+$")


def _live_local_console_present() -> bool:
    if _live_get_environment("SSH_CONNECTION") is not None:
        return False
    try:
        is_tty = sys.stdin.isatty()
    except Exception as exc:
        raise ReadOnlyHostIOExecutionError("stdin TTY inspection failed") from exc
    if type(is_tty) is not bool:
        raise ReadOnlyHostIOExecutionError("isatty returned malformed data")
    if not is_tty:
        return False
    try:
        fd = sys.stdin.fileno()
    except Exception as exc:
        raise ReadOnlyHostIOExecutionError("stdin file descriptor inspection failed") from exc
    if type(fd) is not int or fd < 0:
        raise ReadOnlyHostIOExecutionError("stdin file descriptor is malformed")
    try:
        tty = os.ttyname(fd)
    except Exception as exc:
        raise ReadOnlyHostIOExecutionError("TTY name inspection failed") from exc
    if type(tty) is not str:
        raise ReadOnlyHostIOExecutionError("TTY name is malformed")
    return _LOCAL_TTY_RE.fullmatch(tty) is not None


def build_live_read_only_host_io() -> ReadOnlyHostIO:
    return ReadOnlyHostIO(
        run_command=_live_run_command,
        read_text=_live_read_text,
        read_link=_live_read_link,
        path_exists=_live_path_exists,
        get_environment=_live_get_environment,
        local_console_present=_live_local_console_present,
    )


@dataclass(frozen=True)
class ObservationDiagnostic:
    code: str
    detail: str
    source: str


@dataclass(frozen=True)
class ModeObservationOutcome:
    observed: ObservedState
    diagnostics: tuple[ObservationDiagnostic, ...]


@dataclass(frozen=True)
class ManagementObservationOutcome:
    observed: ManagementPathObservation
    diagnostics: tuple[ObservationDiagnostic, ...]


_INDEX_RE = re.compile(r"^hci(\d+):?(?:\s|$)")
_ZERO_INDEX_LIST_RE = re.compile(r"Index list with 0 items")
_IP_LINE_RE = re.compile(r"^(\d+):\s+(\S+)")


def _basename(path: str) -> str:
    if "/" not in path:
        return path
    return path.rstrip("/").rsplit("/", 1)[-1]


def _parse_ip_interface(line: str) -> str | None:
    """Parse the interface identity from one ``ip -o`` output line.

    The identity is the token following the numeric index token.  A trailing
    colon (``ip -o link``) and a ``@if<N>`` peer suffix are stripped so the
    base interface name is compared.
    """
    match = _IP_LINE_RE.match(line)
    if match is None:
        return None
    name = match.group(2)
    if name.endswith(":"):
        name = name[:-1]
    if "@" in name:
        name = name.split("@", 1)[0]
    if name == "":
        return None
    return name


def _normalize_bt_address(raw: str) -> str | None:
    cleaned = raw.strip()
    if ":" in cleaned:
        parts = cleaned.split(":")
    else:
        if len(cleaned) != 12:
            return None
        parts = [cleaned[i : i + 2] for i in range(0, 12, 2)]
    if len(parts) != 6:
        return None
    normalized: list[str] = []
    for part in parts:
        if len(part) != 2:
            return None
        try:
            value = int(part, 16)
        except ValueError:
            return None
        normalized.append(f"{value:02X}")
    return ":".join(normalized)


def parse_btmgmt_info(text: str) -> tuple[tuple[str, str], ...]:
    """Pure parser for ``btmgmt info`` output.

    Returns controller records as ``("hci<index>", "AA:BB:CC:DD:EE:FF")``
    tuples in deterministic first-seen order.  Malformed addresses are
    rejected and repeated or conflicting indexes keep only their first
    record.  The pure parser never invokes ``btmgmt`` itself.
    """
    records, _ = _parse_btmgmt_records(text)
    return records


def _parse_btmgmt_records(
    text: str,
) -> tuple[tuple[tuple[str, str], ...], list[ObservationDiagnostic]]:
    records: list[tuple[str, str]] = []
    diagnostics: list[ObservationDiagnostic] = []
    seen: dict[str, str] = {}
    index: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _INDEX_RE.match(line)
        if match:
            remainder = line[match.end() :].strip()
            index = (
                f"hci{match.group(1)}"
                if remainder in ("", "Primary controller")
                else None
            )
            continue
        if index is None or not line.startswith("addr"):
            continue
        remainder = line[4:].strip()
        if remainder.startswith(":"):
            remainder = remainder[1:].strip()
        candidate = remainder.split(None, 1)[0] if remainder else ""
        normalized = _normalize_bt_address(candidate)
        if normalized is None:
            diagnostics.append(
                ObservationDiagnostic(
                    "bt_controller_record_invalid",
                    f"controller {index} has malformed address {candidate!r}",
                    "btmgmt info",
                )
            )
            continue
        previous = seen.get(index)
        if previous is not None:
            if previous != normalized:
                diagnostics.append(
                    ObservationDiagnostic(
                        "bt_controller_record_invalid",
                        f"controller {index} reports conflicting addresses {previous!r} and {normalized!r}",
                        "btmgmt info",
                    )
                )
            else:
                diagnostics.append(
                    ObservationDiagnostic(
                        "bt_controller_record_invalid",
                        f"controller {index} is duplicated in btmgmt output",
                        "btmgmt info",
                    )
                )
            continue
        seen[index] = normalized
        records.append((index, normalized))
    return tuple(records), diagnostics


def _run_command(
    io: ReadOnlyHostIO,
    argv: tuple[str, ...],
    timeout: float,
) -> tuple[ReadOnlyCommandResult | None, tuple[ObservationDiagnostic, ...]]:
    source = repr(argv)
    try:
        result = io.run_command(argv, timeout)
    except Exception as exc:
        return None, (
            ObservationDiagnostic(
                "command_exception",
                f"command {argv!r} raised {type(exc).__name__}: {exc}",
                source,
            ),
        )
    if not isinstance(result, ReadOnlyCommandResult):
        return None, (
            ObservationDiagnostic(
                "command_exception",
                f"command {argv!r} returned {type(result).__name__} instead of ReadOnlyCommandResult",
                source,
            ),
        )
    if result.argv != argv:
        return None, (
            ObservationDiagnostic(
                "command_result_argv_mismatch",
                f"command {argv!r} reported argv {result.argv!r}",
                source,
            ),
        )
    if isinstance(result.returncode, bool) or not isinstance(result.returncode, int):
        return None, (
            ObservationDiagnostic(
                "command_returncode_invalid",
                f"command {argv!r} reported non-integer return code {result.returncode!r}",
                source,
            ),
        )
    if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
        return None, (
            ObservationDiagnostic(
                "command_exception",
                f"command {argv!r} returned non-string stdout or stderr",
                source,
            ),
        )
    return result, ()


def _service_active(
    io: ReadOnlyHostIO,
    service: str,
    timeout: float,
) -> tuple[bool, str | None, list[ObservationDiagnostic]]:
    argv = ("systemctl", "is-active", f"{service}.service")
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    unavailable_fact = f"{service}_active"
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "service_status_unavailable",
                f"cannot determine active state of {service}.service",
                repr(argv),
            )
        )
        return False, unavailable_fact, diagnostics
    stdout = result.stdout.strip()
    if result.returncode == 0 and stdout == "active":
        return True, None, diagnostics
    if result.returncode == 0:
        diagnostics.append(
            ObservationDiagnostic(
                "service_status_unavailable",
                f"systemctl is-active for {service}.service returned code 0 with unexpected output {result.stdout!r}",
                repr(argv),
            )
        )
        return False, unavailable_fact, diagnostics
    if stdout in SERVICE_INACTIVE_STATES:
        return False, None, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "service_status_unavailable",
            f"systemctl is-active for {service}.service returned code {result.returncode} with unrecognized output {result.stdout!r}",
            repr(argv),
        )
    )
    return False, unavailable_fact, diagnostics


def _driver_link_bound(
    io: ReadOnlyHostIO,
    link: str,
    expected_driver: str,
    fact_name: str,
) -> tuple[bool, str | None, list[ObservationDiagnostic]]:
    try:
        target = io.read_link(link)
    except FileNotFoundError:
        return False, None, []
    except Exception as exc:
        return False, fact_name, [
            ObservationDiagnostic(
                "driver_link_unavailable",
                f"cannot read driver link {link!r}: {type(exc).__name__}: {exc}",
                link,
            )
        ]
    if not isinstance(target, str):
        return False, fact_name, [
            ObservationDiagnostic(
                "driver_link_unavailable",
                f"driver link {link!r} returned {type(target).__name__} instead of str",
                link,
            )
        ]
    basename = _basename(target)
    if basename == expected_driver:
        return True, None, []
    return False, None, [
        ObservationDiagnostic(
            "driver_mismatch",
            f"driver link {link!r} resolves to {basename!r}, expected {expected_driver!r}",
            link,
        )
    ]


def _interface_exists(
    io: ReadOnlyHostIO,
    interface: str,
    timeout: float,
    fact_name: str | None = None,
) -> tuple[bool, str | None, list[ObservationDiagnostic]]:
    argv = ("ip", "-o", "link", "show", "dev", interface)
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "interface_observation_unavailable",
                f"cannot observe existence of interface {interface!r}",
                repr(argv),
            )
        )
        return False, fact_name, diagnostics
    if result.returncode != 0:
        return False, None, diagnostics
    for line in result.stdout.splitlines():
        if _parse_ip_interface(line) == interface:
            return True, None, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "interface_observation_unavailable",
            f"interface {interface!r} is not proven present in ip link output",
            repr(argv),
        )
    )
    return False, fact_name, diagnostics


def _nm_managed(
    io: ReadOnlyHostIO,
    timeout: float,
) -> tuple[bool | None, str | None, list[ObservationDiagnostic]]:
    argv = ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", WIFI_IFACE)
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "nm_managed_unavailable",
                f"cannot determine NetworkManager managed state of {WIFI_IFACE}",
                repr(argv),
            )
        )
        return None, "wlan1_managed", diagnostics
    if result.returncode != 0:
        diagnostics.append(
            ObservationDiagnostic(
                "nm_managed_unavailable",
                f"nmcli failed with code {result.returncode} for {WIFI_IFACE}",
                repr(argv),
            )
        )
        return None, "wlan1_managed", diagnostics
    value = result.stdout.strip().lower()
    if value in NM_MANAGED_TRUE_VALUES:
        return True, None, diagnostics
    if value in NM_MANAGED_FALSE_VALUES:
        return False, None, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "nm_managed_unavailable",
            f"unrecognized NetworkManager managed output {result.stdout!r} for {WIFI_IFACE}",
            repr(argv),
        )
    )
    return None, "wlan1_managed", diagnostics


def _wireless_type(
    io: ReadOnlyHostIO,
    timeout: float,
) -> tuple[str | None, str | None, list[ObservationDiagnostic]]:
    argv = ("iw", "dev", WIFI_IFACE, "info")
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "wireless_type_unavailable",
                f"cannot determine wireless type of {WIFI_IFACE}",
                repr(argv),
            )
        )
        return None, "wireless_type", diagnostics
    if result.returncode != 0:
        diagnostics.append(
            ObservationDiagnostic(
                "wireless_type_unavailable",
                f"iw failed with code {result.returncode} for {WIFI_IFACE}",
                repr(argv),
            )
        )
        return None, "wireless_type", diagnostics
    types: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("type"):
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                types.append(parts[1].strip())
    if len(types) == 1 and types[0] == "managed":
        return "managed", None, diagnostics
    if len(types) == 1 and types[0] == "AP":
        return "AP", None, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "wireless_type_unavailable",
            f"wireless type of {WIFI_IFACE} is missing, duplicated, or unsupported: {types!r}",
            repr(argv),
        )
    )
    return None, "wireless_type", diagnostics


def _ap_addr_present(
    io: ReadOnlyHostIO,
    timeout: float,
) -> tuple[bool, str | None, list[ObservationDiagnostic]]:
    argv = ("ip", "-o", "-4", "addr", "show", "dev", WIFI_IFACE)
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "ap_address_unavailable",
                f"cannot observe IPv4 address of {WIFI_IFACE}",
                repr(argv),
            )
        )
        return False, "ap_addr_present", diagnostics
    if result.returncode != 0:
        diagnostics.append(
            ObservationDiagnostic(
                "ap_address_unavailable",
                f"address query for {WIFI_IFACE} failed with code {result.returncode}",
                repr(argv),
            )
        )
        return False, "ap_addr_present", diagnostics
    seen_wlan1 = False
    malformed = False
    for line in result.stdout.splitlines():
        if _parse_ip_interface(line) != WIFI_IFACE:
            continue
        seen_wlan1 = True
        tokens = line.split()
        for i, token in enumerate(tokens):
            if token == "inet" and i + 1 < len(tokens):
                if tokens[i + 1] == AP_ADDR_CIDR:
                    return True, None, diagnostics
                try:
                    ipaddress.ip_interface(tokens[i + 1])
                except ValueError:
                    malformed = True
                break
    if not seen_wlan1:
        diagnostics.append(
            ObservationDiagnostic(
                "ap_address_unavailable",
                f"address output for {WIFI_IFACE} proves no {WIFI_IFACE} interface line",
                repr(argv),
            )
        )
        return False, "ap_addr_present", diagnostics
    if malformed:
        diagnostics.append(
            ObservationDiagnostic(
                "ap_address_unavailable",
                f"address output for {WIFI_IFACE} contains a malformed address",
                repr(argv),
            )
        )
        return False, "ap_addr_present", diagnostics
    return False, None, diagnostics


def _iptables_check_argv(table: str, chain: str, spec: str) -> tuple[str, ...]:
    argv = ["iptables"]
    if table != "filter":
        argv += ["-t", table]
    argv += ["-C", chain]
    argv += spec.split()
    return tuple(argv)


_LIVE_PRIVILEGED_FIREWALL_COMMANDS = frozenset(
    _iptables_check_argv(table, chain, spec)
    for table, chain, spec in AP_FIREWALL_RULES
)


def _ap_rules_present(
    io: ReadOnlyHostIO,
    timeout: float,
) -> tuple[bool, str | None, list[ObservationDiagnostic]]:
    present = True
    unavailable = False
    diagnostics: list[ObservationDiagnostic] = []
    for table, chain, spec in AP_FIREWALL_RULES:
        argv = _iptables_check_argv(table, chain, spec)
        result, diags = _run_command(io, argv, timeout)
        diagnostics.extend(diags)
        if result is None:
            present = False
            unavailable = True
            diagnostics.append(
                ObservationDiagnostic(
                    "firewall_rule_unavailable",
                    f"cannot check firewall rule {spec!r}",
                    repr(argv),
                )
            )
            continue
        if result.returncode == 0:
            continue
        if result.returncode == 1:
            present = False
            continue
        present = False
        unavailable = True
        diagnostics.append(
            ObservationDiagnostic(
                "firewall_rule_unavailable",
                f"iptables check for rule {spec!r} returned code {result.returncode}",
                repr(argv),
            )
        )
    return present, ("ap_rules_present" if unavailable else None), diagnostics


def _bt_drivers_bound(
    io: ReadOnlyHostIO,
) -> tuple[tuple[bool, bool, bool], str | None, list[ObservationDiagnostic]]:
    bound: list[bool] = []
    diagnostics: list[ObservationDiagnostic] = []
    unavailable = False
    for link in BT_DRIVER_LINKS:
        value, link_unavailable, diags = _driver_link_bound(
            io, link, BT_DRIVER, "bt_drivers_bound"
        )
        bound.append(value)
        unavailable = unavailable or (link_unavailable is not None)
        diagnostics.extend(diags)
    return (
        (bound[0], bound[1], bound[2]),
        ("bt_drivers_bound" if unavailable else None),
        diagnostics,
    )


def _bt_controllers(
    io: ReadOnlyHostIO,
    timeout: float,
) -> tuple[tuple[tuple[str, str], ...], str | None, list[ObservationDiagnostic]]:
    argv = ("btmgmt", "info")
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "bt_controller_info_unavailable",
                "cannot obtain Bluetooth controller information",
                repr(argv),
            )
        )
        return (), "bt_controllers", diagnostics
    if result.returncode != 0:
        diagnostics.append(
            ObservationDiagnostic(
                "bt_controller_info_unavailable",
                f"btmgmt info failed with code {result.returncode}",
                repr(argv),
            )
        )
        return (), "bt_controllers", diagnostics
    records, record_diags = _parse_btmgmt_records(result.stdout)
    diagnostics.extend(record_diags)
    if record_diags:
        return records, "bt_controllers", diagnostics
    if result.stdout and not records and _ZERO_INDEX_LIST_RE.fullmatch(result.stdout.strip()) is None:
        diagnostics.append(
            ObservationDiagnostic(
                "bt_controller_info_unavailable",
                "successful nonempty output contained no parseable primary-controller records",
                repr(argv),
            )
        )
        return records, "bt_controllers", diagnostics
    return records, None, diagnostics


def observe_mode_state(
    io: ReadOnlyHostIO,
    gerald_stopped_or_blocked: bool,
    command_timeout: float = 5.0,
) -> ModeObservationOutcome:
    diagnostics: list[ObservationDiagnostic] = []
    unavailable: list[str] = []

    hostapd_active, fact, diags = _service_active(io, "hostapd", command_timeout)
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)
    dnsmasq_active, fact, diags = _service_active(io, "dnsmasq", command_timeout)
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    wifi_driver_bound, fact, diags = _driver_link_bound(
        io, WIFI_DRIVER_LINK, WIFI_DRIVER, "wifi_driver_bound"
    )
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    wlan1_exists, fact, diags = _interface_exists(
        io, WIFI_IFACE, command_timeout, "wlan1_exists"
    )
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    wlan1_managed, fact, diags = _nm_managed(io, command_timeout)
    diagnostics.extend(diags)
    if fact is not None and wlan1_exists is not False:
        unavailable.append(fact)

    wireless_type, fact, diags = _wireless_type(io, command_timeout)
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    ap_addr_present, fact, diags = _ap_addr_present(io, command_timeout)
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    ap_rules_present, fact, diags = _ap_rules_present(io, command_timeout)
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    bt_drivers_bound, fact, diags = _bt_drivers_bound(io)
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    bt_controllers, fact, diags = _bt_controllers(io, command_timeout)
    diagnostics.extend(diags)
    if fact is not None:
        unavailable.append(fact)

    observed = ObservedState(
        hostapd_active=hostapd_active,
        dnsmasq_active=dnsmasq_active,
        wifi_driver_bound=wifi_driver_bound,
        wlan1_exists=wlan1_exists,
        wlan1_managed=wlan1_managed,
        wireless_type=wireless_type,
        ap_addr_present=ap_addr_present,
        ap_rules_present=ap_rules_present,
        gerald_stopped_or_blocked=gerald_stopped_or_blocked,
        bt_drivers_bound=bt_drivers_bound,
        bt_controllers=bt_controllers,
        unavailable_facts=tuple(dict.fromkeys(unavailable)),
    )
    return ModeObservationOutcome(
        observed=observed, diagnostics=tuple(diagnostics)
    )


def _read_text(
    io: ReadOnlyHostIO,
    path: str,
) -> tuple[str | None, list[ObservationDiagnostic]]:
    try:
        value = io.read_text(path)
    except Exception as exc:
        return None, [
            ObservationDiagnostic(
                "file_read_unavailable",
                f"cannot read {path!r}: {type(exc).__name__}: {exc}",
                path,
            )
        ]
    if not isinstance(value, str):
        return None, [
            ObservationDiagnostic(
                "file_read_unavailable",
                f"read of {path!r} returned {type(value).__name__} instead of str",
                path,
            )
        ]
    return value, []


def _operstate(
    io: ReadOnlyHostIO,
    interface: str,
) -> tuple[str | None, list[ObservationDiagnostic]]:
    path = f"/sys/class/net/{interface}/operstate"
    value, diagnostics = _read_text(io, path)
    if value is None:
        diagnostics.append(
            ObservationDiagnostic(
                "operstate_invalid",
                f"operstate of {interface!r} is unavailable",
                path,
            )
        )
        return None, diagnostics
    normalized = value.strip().lower()
    if normalized in OPERSTATE_VALUES:
        return normalized, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "operstate_invalid",
            f"unrecognized operstate {value!r} for {interface!r}",
            path,
        )
    )
    return None, diagnostics


def _carrier(
    io: ReadOnlyHostIO,
    interface: str,
) -> tuple[bool | None, list[ObservationDiagnostic]]:
    path = f"/sys/class/net/{interface}/carrier"
    value, diagnostics = _read_text(io, path)
    if value is None:
        diagnostics.append(
            ObservationDiagnostic(
                "carrier_invalid",
                f"carrier of {interface!r} is unavailable",
                path,
            )
        )
        return None, diagnostics
    normalized = value.strip()
    if normalized == "1":
        return True, diagnostics
    if normalized == "0":
        return False, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "carrier_invalid",
            f"unrecognized carrier value {value!r} for {interface!r}",
            path,
        )
    )
    return None, diagnostics


def _route_dev_interfaces(stdout: str) -> tuple[str, ...]:
    interfaces: set[str] = set()
    for line in stdout.splitlines():
        tokens = line.split()
        for i, token in enumerate(tokens):
            if token == "dev" and i + 1 < len(tokens):
                interfaces.add(tokens[i + 1])
                break
    return tuple(sorted(interfaces))


@dataclass(frozen=True)
class _DefaultRouteRecord:
    interface: str
    metric: int | None


_INTERFACE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}", re.ASCII)


def _is_valid_interface_name(value: str) -> bool:
    return (
        type(value) is str
        and value not in {".", ".."}
        and _INTERFACE_NAME_RE.fullmatch(value) is not None
    )


def _parse_default_route_records(
    stdout: str,
) -> tuple[tuple[_DefaultRouteRecord, ...] | None, bool]:
    records: list[_DefaultRouteRecord] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        if not tokens or tokens[0] != "default":
            return None, False
        if tokens.count("dev") != 1:
            return None, False
        dev_index = tokens.index("dev")
        if dev_index + 1 >= len(tokens):
            return None, False
        interface = tokens[dev_index + 1]
        if not _is_valid_interface_name(interface):
            return None, False
        metric_indexes = [index for index, token in enumerate(tokens) if token == "metric"]
        if len(metric_indexes) > 1:
            return None, False
        metric: int | None = None
        if metric_indexes:
            metric_index = metric_indexes[0]
            if metric_index + 1 >= len(tokens) or not re.fullmatch(r"[0-9]+", tokens[metric_index + 1], re.ASCII):
                return None, False
            metric = int(tokens[metric_index + 1], 10)
        index = 1
        while index < len(tokens):
            key = tokens[index]
            if key not in {"via", "dev", "proto", "src", "metric", "pref", "onlink"}:
                return None, False
            if key == "onlink":
                index += 1
                continue
            if index + 1 >= len(tokens):
                return None, False
            index += 2
        records.append(_DefaultRouteRecord(interface, metric))
    return tuple(records), True


def _interface_addresses(
    io: ReadOnlyHostIO,
    interface: str,
    timeout: float,
) -> tuple[tuple[str, ...], list[ObservationDiagnostic]]:
    argv = ("ip", "-o", "addr", "show", "dev", interface)
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "address_output_invalid",
                f"cannot observe addresses of interface {interface!r}",
                repr(argv),
            )
        )
        return (), diagnostics
    if result.returncode != 0:
        diagnostics.append(
            ObservationDiagnostic(
                "address_output_invalid",
                f"address query for {interface!r} failed with code {result.returncode}",
                repr(argv),
            )
        )
        return (), diagnostics
    addresses: list[str] = []
    for line in result.stdout.splitlines():
        if _parse_ip_interface(line) != interface:
            continue
        tokens = line.split()
        for i, token in enumerate(tokens):
            if token not in ("inet", "inet6") or i + 1 >= len(tokens):
                continue
            candidate = tokens[i + 1]
            try:
                ipaddress.ip_interface(candidate)
            except ValueError:
                diagnostics.append(
                    ObservationDiagnostic(
                        "address_output_invalid",
                        f"malformed address candidate {candidate!r} in {line!r}",
                        repr(argv),
                    )
                )
                continue
            addresses.append(candidate)
            break
    return tuple(addresses), diagnostics


def _default_route_interface(
    io: ReadOnlyHostIO,
    timeout: float,
) -> tuple[str | None, list[ObservationDiagnostic]]:
    argv = ("ip", "route", "show", "default")
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "default_route_unavailable",
                "cannot observe the default route",
                repr(argv),
            )
        )
        return None, diagnostics
    if result.returncode != 0:
        diagnostics.append(
            ObservationDiagnostic(
                "default_route_unavailable",
                f"default route query failed with code {result.returncode}",
                repr(argv),
            )
        )
        return None, diagnostics
    records, parseable = _parse_default_route_records(result.stdout)
    if not parseable:
        diagnostics.append(
            ObservationDiagnostic(
                "default_route_ambiguous",
                "successful default route output contains an unparseable route record",
                repr(argv),
            )
        )
        return None, diagnostics
    if not records:
        diagnostics.append(
            ObservationDiagnostic(
                "default_route_unavailable",
                "default route output contains no dev interface fact",
                repr(argv),
            )
        )
        return None, diagnostics
    if len(records) == 1:
        return records[0].interface, diagnostics
    if any(record.metric is None for record in records):
        diagnostics.append(
            ObservationDiagnostic(
                "default_route_ambiguous",
                "competing default route output omits a metric",
                repr(argv),
            )
        )
        return None, diagnostics
    lowest_metric = min(record.metric for record in records)
    lowest = [record for record in records if record.metric == lowest_metric]
    if len(lowest) == 1:
        return lowest[0].interface, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "default_route_ambiguous",
            f"default route output has {len(lowest)} lowest-metric records",
            repr(argv),
        )
    )
    return None, diagnostics


def _parse_port(value: str) -> int | None:
    try:
        port = int(value, 10)
    except ValueError:
        return None
    if str(port) != value:
        return None
    if port < 1 or port > 65535:
        return None
    return port


def _ssh_connection(
    io: ReadOnlyHostIO,
) -> tuple[bool, str | None, list[ObservationDiagnostic]]:
    source = "SSH_CONNECTION"
    try:
        raw = io.get_environment("SSH_CONNECTION")
    except Exception as exc:
        return True, None, [
            ObservationDiagnostic(
                "ssh_connection_invalid",
                f"environment access for SSH_CONNECTION raised {type(exc).__name__}: {exc}",
                source,
            )
        ]
    if raw is None:
        return False, None, []
    if not isinstance(raw, str):
        return True, None, [
            ObservationDiagnostic(
                "ssh_connection_invalid",
                f"SSH_CONNECTION returned {type(raw).__name__} instead of str",
                source,
            )
        ]
    fields = raw.split()
    if len(fields) != 4:
        return True, None, [
            ObservationDiagnostic(
                "ssh_connection_invalid",
                f"SSH_CONNECTION {raw!r} does not have exactly four whitespace-separated fields",
                source,
            )
        ]
    remote_address, remote_port, local_address, local_port = fields
    if (
        _parse_ip_address(remote_address) is None
        or _parse_ip_address(local_address) is None
        or _parse_port(remote_port) is None
        or _parse_port(local_port) is None
    ):
        return True, None, [
            ObservationDiagnostic(
                "ssh_connection_invalid",
                f"SSH_CONNECTION {raw!r} contains invalid address or port fields",
                source,
            )
        ]
    return True, remote_address, []


def _peer_route_interface(
    io: ReadOnlyHostIO,
    peer_address: str | None,
    timeout: float,
) -> tuple[str | None, list[ObservationDiagnostic]]:
    if peer_address is None:
        return None, []
    argv = ("ip", "route", "get", peer_address)
    result, diags = _run_command(io, argv, timeout)
    diagnostics = list(diags)
    if result is None:
        diagnostics.append(
            ObservationDiagnostic(
                "peer_route_unavailable",
                f"cannot observe route to peer {peer_address!r}",
                repr(argv),
            )
        )
        return None, diagnostics
    if result.returncode != 0:
        diagnostics.append(
            ObservationDiagnostic(
                "peer_route_unavailable",
                f"peer route query failed with code {result.returncode}",
                repr(argv),
            )
        )
        return None, diagnostics
    interfaces = _route_dev_interfaces(result.stdout)
    if len(interfaces) == 1:
        return interfaces[0], diagnostics
    if not interfaces:
        diagnostics.append(
            ObservationDiagnostic(
                "peer_route_unavailable",
                f"peer route output for {peer_address!r} contains no dev interface fact",
                repr(argv),
            )
        )
        return None, diagnostics
    diagnostics.append(
        ObservationDiagnostic(
            "peer_route_ambiguous",
            f"peer route output for {peer_address!r} names conflicting interfaces {interfaces!r}",
            repr(argv),
        )
    )
    return None, diagnostics


def _local_console_present(
    io: ReadOnlyHostIO,
) -> tuple[bool, list[ObservationDiagnostic]]:
    source = "local_console_present"
    try:
        value = io.local_console_present()
    except Exception as exc:
        return False, [
            ObservationDiagnostic(
                "local_console_unavailable",
                f"local console provider raised {type(exc).__name__}: {exc}",
                source,
            )
        ]
    if not isinstance(value, bool):
        return False, [
            ObservationDiagnostic(
                "local_console_unavailable",
                f"local console provider returned {type(value).__name__} instead of bool",
                source,
            )
        ]
    return value, []


def observe_management_path(
    io: ReadOnlyHostIO,
    required_interface: str = SAFE_MANAGEMENT_IFACE,
    command_timeout: float = 5.0,
) -> ManagementObservationOutcome:
    diagnostics: list[ObservationDiagnostic] = []

    interface_exists, _interface_fact, diags = _interface_exists(
        io, required_interface, command_timeout
    )
    diagnostics.extend(diags)

    operstate, diags = _operstate(io, required_interface)
    diagnostics.extend(diags)

    carrier, diags = _carrier(io, required_interface)
    diagnostics.extend(diags)

    addresses, diags = _interface_addresses(io, required_interface, command_timeout)
    diagnostics.extend(diags)

    default_route_interface, diags = _default_route_interface(io, command_timeout)
    diagnostics.extend(diags)

    ssh_connection_present, ssh_peer_address, diags = _ssh_connection(io)
    diagnostics.extend(diags)

    peer_route_interface, diags = _peer_route_interface(
        io, ssh_peer_address, command_timeout
    )
    diagnostics.extend(diags)

    local_console, diags = _local_console_present(io)
    diagnostics.extend(diags)

    observed = ManagementPathObservation(
        interface_name=required_interface,
        interface_exists=interface_exists,
        operstate=operstate,
        carrier=carrier,
        addresses=addresses,
        default_route_interface=default_route_interface,
        ssh_connection_present=ssh_connection_present,
        ssh_peer_address=ssh_peer_address,
        peer_route_interface=peer_route_interface,
        local_console=local_console,
    )
    return ManagementObservationOutcome(
        observed=observed, diagnostics=tuple(diagnostics)
    )


def _write_management_gate_failure(
    status: str,
    code: str,
    returncode: int,
    stderr: TextIO,
) -> int:
    stderr.write(f"UCONSOLE_MANAGEMENT_GATE status={status} code={code}\n")
    return returncode


def _run_management_gate_cli(
    argv: Sequence[str],
    *,
    io_factory: Callable[[], ReadOnlyHostIO],
    observer: Callable[[ReadOnlyHostIO], ManagementObservationOutcome],
    evaluator: Callable[[ManagementPathObservation], ManagementGateResult],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if tuple(argv) != (_MANAGEMENT_GATE_COMMAND,):
        return _write_management_gate_failure(
            "USAGE_ERROR", "usage", MANAGEMENT_GATE_USAGE_ERROR, stderr
        )

    try:
        io = io_factory()
        outcome = observer(io)
        if not isinstance(outcome, ManagementObservationOutcome):
            raise TypeError("management observation outcome has invalid shape")
        if type(outcome.diagnostics) is not tuple:
            raise TypeError("management diagnostics have invalid shape")
        for diagnostic in outcome.diagnostics:
            if not isinstance(diagnostic, ObservationDiagnostic):
                raise TypeError("management diagnostic has invalid shape")
            if _MANAGEMENT_GATE_CODE_RE.fullmatch(diagnostic.code) is None:
                raise ValueError("management diagnostic code is invalid")
        if outcome.diagnostics:
            return _write_management_gate_failure(
                "OBSERVATION_UNAVAILABLE",
                outcome.diagnostics[0].code,
                MANAGEMENT_GATE_OBSERVATION_UNAVAILABLE,
                stderr,
            )
        gate = evaluator(outcome.observed)
        if not isinstance(gate, ManagementGateResult):
            raise TypeError("management gate result has invalid shape")
        if type(gate.allowed) is not bool or type(gate.errors) is not tuple:
            raise TypeError("management gate result has invalid shape")
        for error in gate.errors:
            if not isinstance(error, ManagementGateError):
                raise TypeError("management gate error has invalid shape")
            if _MANAGEMENT_GATE_CODE_RE.fullmatch(error.code) is None:
                raise ValueError("management gate error code is invalid")
        if gate.allowed and gate.errors:
            raise ValueError("allowed management gate contains errors")
        if not gate.allowed and not gate.errors:
            raise ValueError("denied management gate contains no errors")
        if not gate.allowed:
            return _write_management_gate_failure(
                "DENIED_MANAGEMENT_GATE",
                gate.errors[0].code,
                MANAGEMENT_GATE_DENIED,
                stderr,
            )
    except Exception:
        return _write_management_gate_failure(
            "INTERNAL_CONTRACT_ERROR", "internal_error", MANAGEMENT_GATE_INTERNAL_ERROR, stderr
        )

    stdout.write("UCONSOLE_MANAGEMENT_GATE status=ALLOWED code=none\n")
    return MANAGEMENT_GATE_ALLOWED


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return _run_management_gate_cli(
        argv,
        io_factory=build_live_read_only_host_io,
        observer=observe_management_path,
        evaluator=evaluate_management_path,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())


# Gerald lifecycle primitive (Slice 7D1).  This block is deliberately
# independent of the mode/state and live-host boundaries above.
class GeraldLifecycleBusy(RuntimeError):
    """Another compliant holder owns the lifecycle domain."""


class GeraldLifecycleUnsafe(RuntimeError):
    """The lifecycle path, inode, ownership, mode, or lock state is unsafe."""


class GeraldLifecycleWitnessInvalid(RuntimeError):
    """A lifecycle capability is forged, stale, released, or role-incompatible."""


def default_gerald_lifecycle_lock_path(expected_uid: int) -> Path:
    if type(expected_uid) is not int:
        raise TypeError("expected uid must be an integer")
    if expected_uid < 0:
        raise ValueError("expected uid must be non-negative")
    return Path(f"/run/user/{expected_uid}/uconsole-mt7961-gerald.lifecycle.lock")


@dataclass(frozen=True, init=False)
class GeraldRuntimeLease:
    path: Path
    device: int
    inode: int
    owner_uid: int

    def __new__(cls, *args, **kwargs):
        raise TypeError("lifecycle capabilities are issued by the lock")


@dataclass(frozen=True, init=False)
class GeraldExclusionWitness:
    path: Path
    device: int
    inode: int
    owner_uid: int

    def __new__(cls, *args, **kwargs):
        raise TypeError("lifecycle capabilities are issued by the lock")


@dataclass(frozen=True)
class _GeraldLifecycleOps:
    open: Callable[..., int]
    close: Callable[[int], None]
    fstat: Callable[[int], os.stat_result]
    stat: Callable[..., os.stat_result]
    flock: Callable[[int, int], None]


_GERALD_BUSY_ERRNOS = frozenset((errno.EACCES, errno.EAGAIN))
_GERALD_LOCK_MODE = 0o600
_GERALD_PARENT_MODE = 0o700


def _gerald_unsafe() -> GeraldLifecycleUnsafe:
    return GeraldLifecycleUnsafe("lifecycle lock is unsafe")


def _gerald_invalid() -> GeraldLifecycleWitnessInvalid:
    return GeraldLifecycleWitnessInvalid("lifecycle witness is invalid")


class GeraldLifecycleLock:
    def __init__(self, expected_uid: int, path: Path | str | None = None, *, ops=None):
        if type(expected_uid) is not int:
            raise TypeError("expected uid must be an integer")
        if expected_uid < 0:
            raise ValueError("expected uid must be non-negative")
        self._expected_uid = expected_uid
        try:
            self._path = Path(path) if path is not None else default_gerald_lifecycle_lock_path(expected_uid)
        except (TypeError, ValueError) as exc:
            raise GeraldLifecycleUnsafe("lifecycle lock is unsafe") from exc
        self._ops = ops if ops is not None else _GeraldLifecycleOps(
            os.open, os.close, os.fstat, os.stat, fcntl.flock
        )
        self._held = False
        self._fd = None
        self._parent_fd = None
        self._generation = 0
        self._capability = None
        self._token = None
        self._binding = object()
        self._parent_identity = None

    def _flags(self, *names: str) -> int:
        try:
            value = 0
            for name in names:
                value |= getattr(os, name)
            return value
        except AttributeError as exc:
            raise _gerald_unsafe() from exc

    def _path_contract(self) -> tuple[Path, str]:
        path = self._path
        if not path.is_absolute() or not path.name or path.name in {".", ".."}:
            raise _gerald_unsafe()
        parent = path.parent
        if parent == path or not parent.is_absolute():
            raise _gerald_unsafe()
        try:
            if stat.S_ISLNK(self._ops.stat(parent, follow_symlinks=False).st_mode):
                raise _gerald_unsafe()
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                raise _gerald_unsafe() from exc
            raise _gerald_unsafe() from exc
        return parent, path.name

    def _parent_flags(self) -> int:
        return self._flags("O_RDONLY", "O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW")

    def _lock_flags(self) -> int:
        return self._flags("O_RDWR", "O_CREAT", "O_CLOEXEC", "O_NOFOLLOW")

    def _check_parent(self, parent: Path, parent_fd: int) -> os.stat_result:
        try:
            path_info = self._ops.stat(parent, follow_symlinks=False)
            fd_info = self._ops.fstat(parent_fd)
        except OSError as exc:
            raise _gerald_unsafe() from exc
        if (
            not stat.S_ISDIR(fd_info.st_mode)
            or fd_info.st_uid != self._expected_uid
            or stat.S_IMODE(fd_info.st_mode) != _GERALD_PARENT_MODE
            or (path_info.st_dev, path_info.st_ino) != (fd_info.st_dev, fd_info.st_ino)
        ):
            raise _gerald_unsafe()
        return fd_info

    def _check_lock(self, parent_fd: int, fd: int, name: str) -> os.stat_result:
        try:
            fd_info = self._ops.fstat(fd)
            path_info = self._ops.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise _gerald_unsafe() from exc
        if (
            not stat.S_ISREG(fd_info.st_mode)
            or fd_info.st_uid != self._expected_uid
            or stat.S_IMODE(fd_info.st_mode) != _GERALD_LOCK_MODE
            or fd_info.st_nlink != 1
            or (path_info.st_dev, path_info.st_ino) != (fd_info.st_dev, fd_info.st_ino)
        ):
            raise _gerald_unsafe()
        return fd_info

    def _close_failed(self, fd: int | None, parent_fd: int | None, *, unlock: bool) -> None:
        first = None
        if unlock and fd is not None:
            try:
                self._ops.flock(fd, fcntl.LOCK_UN)
            except BaseException as exc:
                first = exc
        if fd is not None:
            try:
                self._ops.close(fd)
            except BaseException as exc:
                if first is None:
                    first = exc
        if parent_fd is not None:
            try:
                self._ops.close(parent_fd)
            except BaseException as exc:
                if first is None:
                    first = exc
        if first is not None:
            raise _gerald_unsafe() from first

    def _acquire(self, role: str):
        if self._held:
            raise _gerald_unsafe()
        parent, name = self._path_contract()
        parent_fd = None
        fd = None
        locked = False
        try:
            parent_fd = self._ops.open(parent, self._parent_flags())
            parent_info = self._check_parent(parent, parent_fd)
            try:
                fd = self._ops.open(
                    name,
                    self._lock_flags(),
                    _GERALD_LOCK_MODE,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise _gerald_unsafe() from exc
            info = self._check_lock(parent_fd, fd, name)
            try:
                self._ops.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in _GERALD_BUSY_ERRNOS:
                    raise GeraldLifecycleBusy("lifecycle lock is busy") from None
                raise _gerald_unsafe() from exc
            locked = True
            info = self._check_lock(parent_fd, fd, name)
            self._generation += 1
            token = object()
            capability_type = GeraldRuntimeLease if role == "runtime" else GeraldExclusionWitness
            capability = object.__new__(capability_type)
            object.__setattr__(capability, "path", self._path)
            object.__setattr__(capability, "device", info.st_dev)
            object.__setattr__(capability, "inode", info.st_ino)
            object.__setattr__(capability, "owner_uid", info.st_uid)
            object.__setattr__(capability, "_issuer", self)
            object.__setattr__(capability, "_token", token)
            object.__setattr__(capability, "_issuer_binding", self._binding)
            object.__setattr__(capability, "_generation", self._generation)
            object.__setattr__(capability, "_role", role)
            object.__setattr__(capability, "_fd", fd)
            object.__setattr__(capability, "_parent_fd", parent_fd)
            self._fd = fd
            self._parent_fd = parent_fd
            self._parent_identity = (parent_info.st_dev, parent_info.st_ino)
            self._capability = capability
            self._token = token
            self._held = True
            return capability
        except GeraldLifecycleBusy:
            self._close_failed(fd, parent_fd, unlock=locked)
            raise
        except GeraldLifecycleUnsafe:
            self._close_failed(fd, parent_fd, unlock=locked)
            raise
        except BaseException as exc:
            self._close_failed(fd, parent_fd, unlock=locked)
            raise _gerald_unsafe() from exc

    def acquire_runtime(self) -> GeraldRuntimeLease:
        return self._acquire("runtime")

    def acquire_exclusion(self) -> GeraldExclusionWitness:
        return self._acquire("exclusion")

    def validate_runtime(self, lease: GeraldRuntimeLease) -> None:
        self._validate(lease, "runtime")

    def validate_exclusion(self, witness: GeraldExclusionWitness) -> None:
        self._validate(witness, "exclusion")

    def release(self) -> None:
        capability = self._capability
        fd = self._fd
        parent_fd = self._parent_fd
        self._capability = None
        self._token = None
        self._held = False
        self._fd = None
        self._parent_fd = None
        self._parent_identity = None
        if fd is None and parent_fd is None:
            return None
        first = None
        if fd is not None:
            try:
                self._ops.flock(fd, fcntl.LOCK_UN)
            except BaseException as exc:
                first = exc
            try:
                self._ops.close(fd)
            except BaseException as exc:
                if first is None:
                    first = exc
        if parent_fd is not None:
            try:
                self._ops.close(parent_fd)
            except BaseException as exc:
                if first is None:
                    first = exc
        if first is not None:
            raise _gerald_unsafe() from first
        return None

    def _validate(self, capability, role: str) -> None:
        expected = GeraldRuntimeLease if role == "runtime" else GeraldExclusionWitness
        if type(capability) is not expected or not self._held:
            raise _gerald_invalid()
        if (
            getattr(capability, "_issuer", None) is not self
            or getattr(capability, "_issuer_binding", None) is not self._binding
            or getattr(capability, "_role", None) != role
            or getattr(capability, "_token", None) is not self._token
            or getattr(capability, "_generation", None) != self._generation
            or capability is not self._capability
        ):
            raise _gerald_invalid()
        parent, name = self._path_contract()
        try:
            parent_info = self._check_parent(parent, self._parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != self._parent_identity:
                raise _gerald_invalid()
            info = self._check_lock(self._parent_fd, self._fd, name)
        except (GeraldLifecycleUnsafe, TypeError, ValueError, OSError) as exc:
            raise _gerald_invalid() from exc
        if (info.st_dev, info.st_ino) != (capability.device, capability.inode):
            raise _gerald_invalid()
        if capability.path != self._path or capability.owner_uid != self._expected_uid:
            raise _gerald_invalid()

    def runtime(self):
        return _GeraldRoleContext(self, "runtime")

    def exclusion(self):
        return _GeraldRoleContext(self, "exclusion")


class _GeraldRoleContext:
    def __init__(self, lock: GeraldLifecycleLock, role: str):
        self._lock = lock
        self._role = role

    def __enter__(self):
        if self._role == "runtime":
            return self._lock.acquire_runtime()
        return self._lock.acquire_exclusion()

    def __exit__(self, exc_type, exc_value, traceback):
        self._lock.release()
        return False


def validate_gerald_runtime_lease(lease: GeraldRuntimeLease) -> None:
    issuer = getattr(lease, "_issuer", None)
    if type(lease) is not GeraldRuntimeLease or not isinstance(issuer, GeraldLifecycleLock):
        raise _gerald_invalid()
    issuer._validate(lease, "runtime")


def validate_gerald_exclusion_witness(witness: GeraldExclusionWitness) -> None:
    issuer = getattr(witness, "_issuer", None)
    if type(witness) is not GeraldExclusionWitness or not isinstance(issuer, GeraldLifecycleLock):
        raise _gerald_invalid()
    issuer._validate(witness, "exclusion")
