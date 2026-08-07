"""Pure classification and state-persistence tests for the uConsole MT7961
three-mode interlock."""

import ast
import copy
import errno
from io import StringIO
import json
import multiprocessing
import os
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import uconsole_mode as um


def test_gerald_lifecycle_public_surface_and_default_path():
    assert um.default_gerald_lifecycle_lock_path(1000) == Path(
        "/run/user/1000/uconsole-mt7961-gerald.lifecycle.lock"
    )
    for value in (True, False, -1):
        with pytest.raises((TypeError, ValueError)):
            um.default_gerald_lifecycle_lock_path(value)
    assert issubclass(um.GeraldLifecycleBusy, RuntimeError)
    assert issubclass(um.GeraldLifecycleUnsafe, RuntimeError)
    assert issubclass(um.GeraldLifecycleWitnessInvalid, RuntimeError)
    assert um.GeraldRuntimeLease.__dataclass_params__.frozen
    assert um.GeraldExclusionWitness.__dataclass_params__.frozen
    with pytest.raises(TypeError):
        um.GeraldRuntimeLease()
    with pytest.raises(TypeError):
        um.GeraldExclusionWitness()
    lock = um.GeraldLifecycleLock(1000, Path("/tmp/gerald-lifecycle-test.lock"))
    assert callable(lock.acquire_runtime)
    assert callable(lock.acquire_exclusion)
    assert callable(lock.runtime)
    assert callable(lock.exclusion)
    assert not hasattr(lock, "__enter__")


def test_gerald_runtime_acquisition_creates_safe_empty_lock(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    lock = um.GeraldLifecycleLock(os.getuid(), path)
    lease = lock.acquire_runtime()
    try:
        info = path.stat()
        assert type(lease) is um.GeraldRuntimeLease
        assert lease.path == path
        assert lease.device == info.st_dev
        assert lease.inode == info.st_ino
        assert lease.owner_uid == os.getuid()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.getuid()
        assert info.st_nlink == 1
        assert path.read_bytes() == b""
    finally:
        lock.release()
    assert path.exists()


def test_gerald_lifecycle_roles_validation_and_reacquisition(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    lock = um.GeraldLifecycleLock(os.getuid(), path)
    lease = lock.acquire_runtime()
    um.validate_gerald_runtime_lease(lease)
    with pytest.raises(um.GeraldLifecycleWitnessInvalid):
        um.validate_gerald_exclusion_witness(lease)
    with pytest.raises(um.GeraldLifecycleUnsafe):
        lock.acquire_exclusion()
    lock.release()
    with pytest.raises(um.GeraldLifecycleWitnessInvalid):
        um.validate_gerald_runtime_lease(lease)
    with lock.exclusion() as witness:
        um.validate_gerald_exclusion_witness(witness)
    with pytest.raises(um.GeraldLifecycleWitnessInvalid):
        um.validate_gerald_exclusion_witness(witness)
    fresh = lock.acquire_runtime()
    try:
        assert getattr(fresh, "_generation") != getattr(lease, "_generation")
        assert getattr(fresh, "_token") is not getattr(lease, "_token")
        um.validate_gerald_runtime_lease(fresh)
    finally:
        lock.release()


def test_gerald_lifecycle_contention_is_nonblocking_and_role_neutral(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    first = um.GeraldLifecycleLock(os.getuid(), path)
    second = um.GeraldLifecycleLock(os.getuid(), path)
    lease = first.acquire_runtime()
    try:
        started = time.monotonic()
        with pytest.raises(um.GeraldLifecycleBusy) as caught:
            second.acquire_exclusion()
        assert time.monotonic() - started < 1.0
        assert str(caught.value) == "lifecycle lock is busy"
        um.validate_gerald_runtime_lease(lease)
    finally:
        first.release()
        second.release()


def test_gerald_lifecycle_context_propagates_body_exception_and_releases(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    lock = um.GeraldLifecycleLock(os.getuid(), path)
    with pytest.raises(KeyboardInterrupt):
        with lock.runtime():
            raise KeyboardInterrupt
    contender = um.GeraldLifecycleLock(os.getuid(), path)
    try:
        contender.acquire_runtime()
    finally:
        contender.release()


@pytest.mark.parametrize("kind", ["relative", "absent-parent", "parent-file", "parent-mode"])
def test_gerald_lifecycle_rejects_unsafe_parent(kind, tmp_path):
    uid = os.getuid()
    if kind == "relative":
        path = Path("relative-gerald.lifecycle.lock")
    elif kind == "absent-parent":
        path = tmp_path / "missing" / "gerald.lifecycle.lock"
    elif kind == "parent-file":
        parent = tmp_path / "parent"
        parent.write_bytes(b"")
        path = parent / "gerald.lifecycle.lock"
    else:
        parent = tmp_path / "parent"
        parent.mkdir(mode=0o755)
        os.chmod(parent, 0o755)
        path = parent / "gerald.lifecycle.lock"
    with pytest.raises(um.GeraldLifecycleUnsafe):
        um.GeraldLifecycleLock(uid, path).acquire_runtime()
    assert not path.exists()


def test_gerald_lifecycle_rejects_unsafe_inode_shapes(tmp_path):
    uid = os.getuid()
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    target = parent / "target"
    target.write_bytes(b"")
    path.symlink_to(target)
    with pytest.raises(um.GeraldLifecycleUnsafe):
        um.GeraldLifecycleLock(uid, path).acquire_runtime()
    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(um.GeraldLifecycleUnsafe):
        um.GeraldLifecycleLock(uid, path).acquire_runtime()
    path.rmdir()
    os.mkfifo(path, 0o600)
    with pytest.raises(um.GeraldLifecycleUnsafe):
        um.GeraldLifecycleLock(uid, path).acquire_runtime()
    path.unlink()
    path.write_bytes(b"")
    os.chmod(path, 0o644)
    with pytest.raises(um.GeraldLifecycleUnsafe):
        um.GeraldLifecycleLock(uid, path).acquire_runtime()


def test_gerald_lifecycle_rejects_hardlink_and_live_mutations(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    hardlink = parent / "second-link"
    path.write_bytes(b"")
    os.link(path, hardlink)
    with pytest.raises(um.GeraldLifecycleUnsafe):
        um.GeraldLifecycleLock(os.getuid(), path).acquire_runtime()
    hardlink.unlink()
    os.chmod(path, 0o600)
    lock = um.GeraldLifecycleLock(os.getuid(), path)
    lease = lock.acquire_runtime()
    try:
        os.chmod(path, 0o644)
        with pytest.raises(um.GeraldLifecycleWitnessInvalid):
            lock.validate_runtime(lease)
    finally:
        os.chmod(path, 0o600)
        lock.release()
def test_gerald_lifecycle_validation_is_live_and_antiforgery(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    lock = um.GeraldLifecycleLock(os.getuid(), path)
    lease = lock.acquire_runtime()
    try:
        um.validate_gerald_runtime_lease(lease)
        try:
            shallow = copy.copy(lease)
        except Exception:
            shallow = None
        if shallow is not None:
            if shallow is not lease:
                with pytest.raises(um.GeraldLifecycleWitnessInvalid):
                    lock.validate_runtime(shallow)
            else:
                lock.validate_runtime(shallow)
        try:
            deep = copy.deepcopy(lease)
        except Exception:
            deep = None
        if deep is not None:
            with pytest.raises(um.GeraldLifecycleWitnessInvalid):
                lock.validate_runtime(deep)
        object.__setattr__(lease, "_token", object())
        with pytest.raises(um.GeraldLifecycleWitnessInvalid):
            lock.validate_runtime(lease)
    finally:
        lock.release()


def test_gerald_lifecycle_kernel_lock_released_after_owned_child_sigkill(tmp_path):
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "gerald.lifecycle.lock"
    ready = tmp_path / "ready"
    worktree = Path(__file__).resolve().parents[1]
    code = (
        "import os, pathlib, time; "
        f"import sys; sys.path.insert(0, {str(worktree)!r}); "
        "import uconsole_mode as um; "
        f"lock=um.GeraldLifecycleLock(os.getuid(), pathlib.Path({str(path)!r})); "
        "lock.acquire_runtime(); "
        f"pathlib.Path({str(ready)!r}).write_bytes(b'1'); "
        "time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        contender = um.GeraldLifecycleLock(os.getuid(), path)
        with pytest.raises(um.GeraldLifecycleBusy):
            contender.acquire_runtime()
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        lease = contender.acquire_runtime()
        try:
            um.validate_gerald_runtime_lease(lease)
        finally:
            contender.release()
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


def test_management_gate_cli_usage_contract():
    for argv in ([], ["unknown"], ["management-gate", "extra"], ["--help"]):
        stdout = StringIO()
        stderr = StringIO()
        rc = um._run_management_gate_cli(
            argv,
            io_factory=lambda: object(),
            observer=lambda io: object(),
            evaluator=lambda observed: object(),
            stdout=stdout,
            stderr=stderr,
        )
        assert rc == um.MANAGEMENT_GATE_USAGE_ERROR
        assert stdout.getvalue() == ""
        assert stderr.getvalue() == (
            "UCONSOLE_MANAGEMENT_GATE status=USAGE_ERROR code=usage\n"
        )


def test_management_gate_cli_accepts_management_gate_command():
    stdout = StringIO()
    stderr = StringIO()
    rc = um._run_management_gate_cli(
        ["management-gate"],
        io_factory=lambda: object(),
        observer=lambda io: object(),
        evaluator=lambda observed: object(),
        stdout=stdout,
        stderr=stderr,
    )
    assert rc == um.MANAGEMENT_GATE_INTERNAL_ERROR


def test_management_gate_cli_allows_clean_management_path():
    calls = []
    io = object()
    observed = local_console_observation()
    outcome = um.ManagementObservationOutcome(observed=observed, diagnostics=())

    def io_factory():
        calls.append("io")
        return io

    def observer(actual_io):
        calls.append(("observer", actual_io))
        return outcome

    def evaluator(actual_observed):
        calls.append(("evaluator", actual_observed))
        return um.ManagementGateResult(allowed=True, errors=())

    stdout = StringIO()
    stderr = StringIO()
    rc = um._run_management_gate_cli(
        ["management-gate"],
        io_factory=io_factory,
        observer=observer,
        evaluator=evaluator,
        stdout=stdout,
        stderr=stderr,
    )

    assert rc == um.MANAGEMENT_GATE_ALLOWED
    assert calls == ["io", ("observer", io), ("evaluator", observed)]
    assert stdout.getvalue() == "UCONSOLE_MANAGEMENT_GATE status=ALLOWED code=none\n"
    assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    "diagnostic_code",
    ["default_route_ambiguous", "default_route_unavailable", "interface_observation_unavailable"],
)
def test_management_gate_cli_observation_diagnostic_precedes_evaluation(diagnostic_code):
    calls = []
    outcome = um.ManagementObservationOutcome(
        observed=local_console_observation(),
        diagnostics=(um.ObservationDiagnostic(diagnostic_code, "detail", "source"),),
    )

    def evaluator(observed):
        calls.append(observed)
        return um.ManagementGateResult(allowed=True, errors=())

    stdout = StringIO()
    stderr = StringIO()
    rc = um._run_management_gate_cli(
        ["management-gate"],
        io_factory=lambda: object(),
        observer=lambda io: outcome,
        evaluator=evaluator,
        stdout=stdout,
        stderr=stderr,
    )

    assert rc == um.MANAGEMENT_GATE_OBSERVATION_UNAVAILABLE
    assert calls == []
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        f"UCONSOLE_MANAGEMENT_GATE status=OBSERVATION_UNAVAILABLE code={diagnostic_code}\n"
    )


@pytest.mark.parametrize(
    "error_code",
    ["default_route_wrong", "ssh_peer_route_wrong", "no_usable_address"],
)
def test_management_gate_cli_reports_gate_denial(error_code):
    outcome = um.ManagementObservationOutcome(
        observed=local_console_observation(), diagnostics=()
    )
    gate = um.ManagementGateResult(
        allowed=False,
        errors=(um.ManagementGateError(error_code, "detail", "field"),),
    )
    stdout = StringIO()
    stderr = StringIO()
    rc = um._run_management_gate_cli(
        ["management-gate"],
        io_factory=lambda: object(),
        observer=lambda io: outcome,
        evaluator=lambda observed: gate,
        stdout=stdout,
        stderr=stderr,
    )

    assert rc == um.MANAGEMENT_GATE_DENIED
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        f"UCONSOLE_MANAGEMENT_GATE status=DENIED_MANAGEMENT_GATE code={error_code}\n"
    )


@pytest.mark.parametrize("failure_stage", ["io", "observer", "evaluator"])
def test_management_gate_cli_normalizes_dependency_exceptions(failure_stage):
    outcome = um.ManagementObservationOutcome(
        observed=local_console_observation(), diagnostics=()
    )

    def io_factory():
        if failure_stage == "io":
            raise RuntimeError("secret exception")
        return object()

    def observer(io):
        if failure_stage == "observer":
            raise RuntimeError("secret exception")
        return outcome

    def evaluator(observed):
        if failure_stage == "evaluator":
            raise RuntimeError("secret exception")
        return um.ManagementGateResult(allowed=True, errors=())

    stdout = StringIO()
    stderr = StringIO()
    rc = um._run_management_gate_cli(
        ["management-gate"],
        io_factory=io_factory,
        observer=observer,
        evaluator=evaluator,
        stdout=stdout,
        stderr=stderr,
    )

    assert rc == um.MANAGEMENT_GATE_INTERNAL_ERROR
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "UCONSOLE_MANAGEMENT_GATE status=INTERNAL_CONTRACT_ERROR code=internal_error\n"
    )


@pytest.mark.parametrize(
    "case",
    [
        "bad_diagnostic_code",
        "allowed_with_errors",
        "denied_without_errors",
    ],
)
def test_management_gate_cli_rejects_malformed_contracts(case):
    if case == "bad_diagnostic_code":
        outcome = um.ManagementObservationOutcome(
            observed=local_console_observation(),
            diagnostics=(um.ObservationDiagnostic("BAD-CODE", "detail", "source"),),
        )
        gate = um.ManagementGateResult(allowed=True, errors=())
    elif case == "allowed_with_errors":
        outcome = um.ManagementObservationOutcome(
            observed=local_console_observation(), diagnostics=()
        )
        gate = um.ManagementGateResult(
            allowed=True,
            errors=(um.ManagementGateError("wrong_interface", "detail", "field"),),
        )
    else:
        outcome = um.ManagementObservationOutcome(
            observed=local_console_observation(), diagnostics=()
        )
        gate = um.ManagementGateResult(allowed=False, errors=())
    stdout = StringIO()
    stderr = StringIO()
    rc = um._run_management_gate_cli(
        ["management-gate"],
        io_factory=lambda: object(),
        observer=lambda io: outcome,
        evaluator=lambda observed: gate,
        stdout=stdout,
        stderr=stderr,
    )

    assert rc == um.MANAGEMENT_GATE_INTERNAL_ERROR
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "UCONSOLE_MANAGEMENT_GATE status=INTERNAL_CONTRACT_ERROR code=internal_error\n"
    )


def test_management_gate_main_binds_live_dependencies_once(monkeypatch):
    calls = []
    io = object()
    observed = local_console_observation()
    outcome = um.ManagementObservationOutcome(observed=observed, diagnostics=())
    stdout = StringIO()
    stderr = StringIO()

    def io_factory():
        calls.append("io")
        return io

    def observer(actual_io):
        calls.append(("observer", actual_io))
        return outcome

    def evaluator(actual_observed):
        calls.append(("evaluator", actual_observed))
        return um.ManagementGateResult(allowed=True, errors=())

    monkeypatch.setattr(um, "build_live_read_only_host_io", io_factory)
    monkeypatch.setattr(um, "observe_management_path", observer)
    monkeypatch.setattr(um, "evaluate_management_path", evaluator)
    monkeypatch.setattr(um.sys, "stdout", stdout)
    monkeypatch.setattr(um.sys, "stderr", stderr)

    rc = um.main(["management-gate"])

    assert rc == um.MANAGEMENT_GATE_ALLOWED
    assert calls == ["io", ("observer", io), ("evaluator", observed)]
    assert stdout.getvalue() == "UCONSOLE_MANAGEMENT_GATE status=ALLOWED code=none\n"
    assert stderr.getvalue() == ""


def _write_fake_executable(path, body):
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o700)


def _temporary_launch_deployment(tmp_path):
    deployment = tmp_path / "device"
    deployment.mkdir()
    (deployment / "uconsole_mode.py").write_text("# fake mode module\n")
    (deployment / "run-debug.sh").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' run-debug >> \"$EVENT_LOG\"\n"
    )
    (deployment / "run-debug.sh").chmod(0o700)
    venv = deployment / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "activate").write_text("")
    _write_fake_executable(
        venv / "python",
        "printf '%s\\n' gate >> \"$EVENT_LOG\"\n"
        "rc=\"${FAKE_GATE_RC:-0}\"\n"
        "if test \"$rc\" -eq 0; then\n"
        "  printf '%s\\n' 'UCONSOLE_MANAGEMENT_GATE status=ALLOWED code=none'\n"
        "else\n"
        "  printf '%s\\n' \"UCONSOLE_MANAGEMENT_GATE status=DENIED_MANAGEMENT_GATE code=fake_gate\" >&2\n"
        "fi\n"
        "exit \"$rc\"",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_fake_executable(fake_bin / "id", "printf '1000\\n'")
    _write_fake_executable(fake_bin / "dirname", "printf '%s\\n' \"$FAKE_SCRIPT_DIR\"")
    _write_fake_executable(fake_bin / "pwd", "printf '%s\\n' \"$FAKE_SCRIPT_DIR\"")
    for name in ("pkill", "sleep", "setsid", "lxterminal", "sudo", "systemctl", "python"):
        _write_fake_executable(
            fake_bin / name,
            f"printf '%s\\n' {name} >> \"$EVENT_LOG\"\nexit 0",
        )
    return deployment, fake_bin


def _run_temporary_shell(script, deployment, fake_bin, tmp_path, gate_rc):
    events = tmp_path / "events.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "EVENT_LOG": str(events),
            "FAKE_SCRIPT_DIR": str(deployment),
            "FAKE_GATE_RC": str(gate_rc),
            "HOME": str(tmp_path / "home"),
            "PYTHONPATH": "unsafe-test-path",
            "PYTHONHOME": "unsafe-test-home",
        }
    )
    result = subprocess.run(
        ["bash", str(deployment / script)],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return result, events.read_text().splitlines() if events.exists() else []


@pytest.mark.parametrize("gate_rc", [20, 21, 22, 64])
def test_launch_display_gate_denial_prevents_all_start_side_effects(tmp_path, gate_rc):
    deployment, fake_bin = _temporary_launch_deployment(tmp_path)
    (deployment / "launch-display.sh").write_text(
        Path("device/launch-display.sh").read_text()
    )
    (deployment / "launch-display.sh").chmod(0o700)

    result, events = _run_temporary_shell(
        "launch-display.sh", deployment, fake_bin, tmp_path, gate_rc=gate_rc
    )

    assert result.returncode == gate_rc
    assert result.stdout == ""
    assert "UCONSOLE_MANAGEMENT_GATE" in result.stderr
    assert not {"pkill", "sleep", "setsid", "lxterminal", "run-debug"} & set(events)


@pytest.mark.parametrize("gate_rc", [20, 21, 22, 64])
def test_run_debug_gate_denial_prevents_bluetooth_and_gerald(tmp_path, gate_rc):
    deployment, fake_bin = _temporary_launch_deployment(tmp_path)
    (deployment / "run-debug.sh").write_text(
        Path("device/run-debug.sh").read_text()
    )
    (deployment / "run-debug.sh").chmod(0o700)

    result, events = _run_temporary_shell(
        "run-debug.sh", deployment, fake_bin, tmp_path, gate_rc=gate_rc
    )

    assert result.returncode == gate_rc
    assert "UCONSOLE_MANAGEMENT_GATE" in result.stderr
    assert not {"sudo", "systemctl", "sleep", "python", "gerald"} & set(events)


def test_run_debug_success_preserves_gate_restart_and_gerald_order(tmp_path):
    deployment, fake_bin = _temporary_launch_deployment(tmp_path)
    (deployment / "run-debug.sh").write_text(
        Path("device/run-debug.sh").read_text()
    )
    (deployment / "run-debug.sh").chmod(0o700)

    result, events = _run_temporary_shell(
        "run-debug.sh", deployment, fake_bin, tmp_path, gate_rc=0
    )

    assert result.returncode == 0
    assert events == ["gate", "sudo", "sleep", "python"]
    assert result.stdout == (
        "UCONSOLE_MANAGEMENT_GATE status=ALLOWED code=none\n"
    )


def test_launch_display_success_preserves_cleanup_and_terminal_order(tmp_path):
    deployment, fake_bin = _temporary_launch_deployment(tmp_path)
    launch_source = Path("device/launch-display.sh").read_text()
    launch_source = launch_source.replace(
        "/tmp/lxterm-launch.log", str(tmp_path / "lxterm-launch.log")
    )
    (deployment / "launch-display.sh").write_text(launch_source)
    (deployment / "launch-display.sh").chmod(0o700)
    _write_fake_executable(
        fake_bin / "setsid",
        "printf '%s\\n' setsid >> \"$EVENT_LOG\"\n"
        "\"$@\"",
    )
    _write_fake_executable(
        fake_bin / "lxterminal",
        "printf '%s\\n' lxterminal >> \"$EVENT_LOG\"\nexit 0",
    )

    result, events = _run_temporary_shell(
        "launch-display.sh", deployment, fake_bin, tmp_path, gate_rc=0
    )
    for _ in range(100):
        if events[-2:] == ["setsid", "lxterminal"]:
            break
        time.sleep(0.01)
        events = (
            (tmp_path / "events.log").read_text().splitlines()
            if (tmp_path / "events.log").exists()
            else []
        )

    assert result.returncode == 0
    assert events == [
        "gate", "pkill", "pkill", "pkill", "sleep", "setsid", "lxterminal"
    ]
    assert (tmp_path / "lxterm-launch.log").exists()


def test_run_debug_rechecks_gate_before_retry(tmp_path):
    deployment, fake_bin = _temporary_launch_deployment(tmp_path)
    (deployment / "run-debug.sh").write_text(
        Path("device/run-debug.sh").read_text()
    )
    (deployment / "run-debug.sh").chmod(0o700)
    count_file = tmp_path / "gerald-count"
    _write_fake_executable(
        fake_bin / "python",
        "count=0\n"
        "if test -f \"$COUNT_FILE\"; then read -r count < \"$COUNT_FILE\"; fi\n"
        "count=$((count + 1))\n"
        "printf '%s\\n' \"$count\" > \"$COUNT_FILE\"\n"
        "printf '%s\\n' python >> \"$EVENT_LOG\"\n"
        "if test \"$count\" -eq 1; then exit 7; fi\n"
        "exit 0",
    )
    env_count = os.environ.get("COUNT_FILE")
    os.environ["COUNT_FILE"] = str(count_file)
    try:
        result, events = _run_temporary_shell(
            "run-debug.sh", deployment, fake_bin, tmp_path, gate_rc=0
        )
    finally:
        if env_count is None:
            os.environ.pop("COUNT_FILE", None)
        else:
            os.environ["COUNT_FILE"] = env_count

    assert result.returncode == 0
    assert events == [
        "gate", "sudo", "sleep", "python", "sleep",
        "gate", "sudo", "sleep", "python",
    ]
    assert events.index("gate", 5) < events.index("sudo", 5)


def test_shell_gate_boundaries_are_identical_and_precede_mutations():
    launch = Path("device/launch-display.sh").read_text()
    debug = Path("device/run-debug.sh").read_text()
    helper_start = "run_uconsole_management_gate() {"
    helper_end = "\n}\n"
    launch_helper = launch[launch.index(helper_start): launch.index(helper_end, launch.index(helper_start)) + len(helper_end)]
    debug_helper = debug[debug.index(helper_start): debug.index(helper_end, debug.index(helper_start)) + len(helper_end)]

    assert launch_helper == debug_helper
    assert 'MODE_MODULE="$SCRIPT_DIR/uconsole_mode.py"' in launch
    assert 'MODE_PYTHON="$SCRIPT_DIR/.venv/bin/python"' in launch
    assert '"$MODE_PYTHON" \\\n      -I \\\n      -B \\\n      "$MODE_MODULE" \\\n      management-gate' in launch
    assert '"$HOME/Documents/web/uconsole-companion/run-debug.sh"' not in launch
    assert "PYTHONPATH" in launch and "PYTHONHOME" in launch
    assert launch.index("run_uconsole_management_gate\n") < launch.index("pkill")
    assert debug.index("while true; do") < debug.index("  run_uconsole_management_gate")
    assert debug.index("  run_uconsole_management_gate") < debug.index("sudo systemctl")

BOOT_ID = "boot-2026-08-01-001"
CLOCK_ISO = "2026-08-01T09:00:00+00:00"


def make_store(tmp_path, expected_uid=None, boot_id=BOOT_ID, clock=CLOCK_ISO):
    return um.StateStore(
        state_path=tmp_path / "state.json",
        expected_owner_uid=expected_uid if expected_uid is not None else os.geteuid(),
        boot_id_provider=lambda: boot_id,
        clock_provider=lambda: datetime.fromisoformat(clock),
    )


def sample_state(**overrides):
    data = {
        "schema_version": 1,
        "generation": 3,
        "boot_id": BOOT_ID,
        "phase": "SETTLED",
        "current_mode": "CLIENT",
        "source_mode": None,
        "target_mode": "ROUTER_AP",
        "last_completed_step": "start_hostapd",
        "client_snapshot": {
            "connection_uuid": "nm-uuid-1",
            "nm_managed": True,
            "ip_forward": 1,
        },
        "updated_at": "2026-08-01T08:59:00+00:00",
    }
    data.update(overrides)
    return data


def write_state(tmp_path, data, mode=0o600):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data))
    os.chmod(path, mode)
    return path


def obs(**kw):
    defaults = dict(
        hostapd_active=False,
        dnsmasq_active=False,
        wifi_driver_bound=False,
        wlan1_exists=False,
        wlan1_managed=None,
        wireless_type=None,
        ap_addr_present=False,
        ap_rules_present=False,
        gerald_stopped_or_blocked=False,
        bt_drivers_bound=(False, False, False),
        bt_controllers=(),
    )
    defaults.update(kw)
    return um.ObservedState(**defaults)


def client_state(**kw):
    base = dict(
        hostapd_active=False,
        dnsmasq_active=False,
        wifi_driver_bound=True,
        wlan1_exists=True,
        wlan1_managed=True,
        wireless_type="managed",
        ap_addr_present=False,
        ap_rules_present=False,
        gerald_stopped_or_blocked=True,
        bt_drivers_bound=(False, False, False),
        bt_controllers=(),
    )
    base.update(kw)
    return um.ObservedState(**base)


def router_state(**kw):
    base = dict(
        hostapd_active=True,
        dnsmasq_active=True,
        wifi_driver_bound=True,
        wlan1_exists=True,
        wlan1_managed=False,
        wireless_type="AP",
        ap_addr_present=True,
        ap_rules_present=True,
        gerald_stopped_or_blocked=True,
        bt_drivers_bound=(False, False, False),
        bt_controllers=(),
    )
    base.update(kw)
    return um.ObservedState(**base)


def bt_state(**kw):
    base = dict(
        hostapd_active=False,
        dnsmasq_active=False,
        wifi_driver_bound=False,
        wlan1_exists=True,
        wlan1_managed=False,
        wireless_type=None,
        ap_addr_present=False,
        ap_rules_present=False,
        gerald_stopped_or_blocked=False,
        bt_drivers_bound=(True, True, True),
        bt_controllers=(("hci0", um.CONTROLLER_BT_ADDR),),
    )
    base.update(kw)
    return um.ObservedState(**base)


def test_mode_constants():
    assert um.CLIENT == "CLIENT"
    assert um.ROUTER_AP == "ROUTER_AP"
    assert um.STABLE_BLUETOOTH == "STABLE_BLUETOOTH"
    assert um.UNKNOWN == "UNKNOWN"


def test_phase_constants():
    assert um.SETTLED == "SETTLED"
    assert um.TRANSITIONING_TO_CLIENT == "TRANSITIONING_TO_CLIENT"
    assert um.TRANSITIONING_TO_ROUTER_AP == "TRANSITIONING_TO_ROUTER_AP"
    assert um.TRANSITIONING_TO_STABLE_BLUETOOTH == "TRANSITIONING_TO_STABLE_BLUETOOTH"
    assert um.RECOVERING_TO_CLIENT == "RECOVERING_TO_CLIENT"


def test_client_classification():
    assert um.classify_mode(client_state()) == um.CLIENT


def test_router_ap_classification():
    assert um.classify_mode(router_state()) == um.ROUTER_AP


def test_stable_bluetooth_classification():
    assert um.classify_mode(bt_state()) == um.STABLE_BLUETOOTH


@pytest.mark.parametrize(
    "overrides",
    [
        {"hostapd_active": True},
        {"dnsmasq_active": True},
        {"wifi_driver_bound": False},
        {"wlan1_exists": False},
        {"wlan1_managed": False},
        {"wireless_type": "AP"},
        {"gerald_stopped_or_blocked": False},
    ],
)
def test_client_each_requirement_enforced(overrides):
    assert um.classify_mode(client_state(**overrides)) == um.UNKNOWN


@pytest.mark.parametrize(
    "overrides",
    [
        {"hostapd_active": False},
        {"dnsmasq_active": False},
        {"wifi_driver_bound": False},
        {"wlan1_exists": False},
        {"wlan1_managed": True},
        {"wireless_type": "managed"},
        {"ap_addr_present": False},
        {"ap_rules_present": False},
        {"gerald_stopped_or_blocked": False},
    ],
)
def test_router_ap_each_requirement_enforced(overrides):
    assert um.classify_mode(router_state(**overrides)) == um.UNKNOWN


@pytest.mark.parametrize(
    "overrides",
    [
        {"hostapd_active": True},
        {"dnsmasq_active": True},
        {"wifi_driver_bound": True},
        {"bt_drivers_bound": (False, True, True)},
        {"bt_controllers": ()},
        {"bt_controllers": (("hci0", "AA:BB:CC:DD:EE:FF"),)},
    ],
)
def test_stable_bluetooth_each_requirement_enforced(overrides):
    assert um.classify_mode(bt_state(**overrides)) == um.UNKNOWN


def test_mixed_state_becomes_unknown():
    mixed = obs(
        hostapd_active=True,
        wifi_driver_bound=True,
        wlan1_exists=True,
        wlan1_managed=False,
        wireless_type="AP",
    )
    assert um.classify_mode(mixed) == um.UNKNOWN


def test_stable_bluetooth_allows_wlan1_absent():
    assert um.classify_mode(bt_state(wlan1_exists=False, wlan1_managed=None)) == um.STABLE_BLUETOOTH


def test_stable_bluetooth_allows_wlan1_unmanaged():
    assert um.classify_mode(bt_state(wlan1_exists=True, wlan1_managed=False)) == um.STABLE_BLUETOOTH


def test_stable_bluetooth_gerald_status_irrelevant():
    assert um.classify_mode(bt_state(gerald_stopped_or_blocked=True)) == um.STABLE_BLUETOOTH
    assert um.classify_mode(bt_state(gerald_stopped_or_blocked=False)) == um.STABLE_BLUETOOTH


def test_resolve_controller_by_address():
    controllers = (("hci0", "38:7A:CC:84:A4:14"),)
    assert um.resolve_bt_controller(controllers) == "hci0"


def test_resolve_controller_ignores_index():
    for index in ("hci0", "hci1", "hci2", "hci7"):
        controllers = ((index, "38:7A:CC:84:A4:14"),)
        assert um.resolve_bt_controller(controllers, um.CONTROLLER_BT_ADDR) == index


def test_resolve_controller_missing_returns_none():
    assert um.resolve_bt_controller(()) is None
    assert um.resolve_bt_controller((("hci0", "AA:BB:CC:DD:EE:FF"),)) is None


def test_classify_uses_address_not_index():
    assert um.classify_mode(bt_state(bt_controllers=(("hci3", um.CONTROLLER_BT_ADDR),))) == um.STABLE_BLUETOOTH


def test_missing_target_controller_prevents_stable_bluetooth():
    assert um.classify_mode(bt_state(bt_controllers=())) == um.UNKNOWN


def test_observed_state_is_immutable():
    state = client_state()
    with pytest.raises(Exception):
        state.hostapd_active = True


def test_default_state_shape(tmp_path):
    store = make_store(tmp_path)
    state = store.default_state()
    assert state.schema_version == 1
    assert state.generation == 0
    assert state.boot_id == ""
    assert state.phase == um.SETTLED
    assert state.current_mode == um.UNKNOWN
    assert state.source_mode is None
    assert state.target_mode is None
    assert state.last_completed_step is None
    assert state.updated_at == ""
    assert state.client_snapshot == um.ClientSnapshot()
    assert state.client_snapshot.connection_uuid is None
    assert state.client_snapshot.nm_managed is None
    assert state.client_snapshot.ip_forward is None
    assert um.persisted_state_to_dict(state) == {
        "schema_version": 1,
        "generation": 0,
        "boot_id": "",
        "phase": "SETTLED",
        "current_mode": "UNKNOWN",
        "source_mode": None,
        "target_mode": None,
        "last_completed_step": None,
        "client_snapshot": {
            "connection_uuid": None,
            "nm_managed": None,
            "ip_forward": None,
        },
        "updated_at": "",
    }


def test_round_trip_persistence(tmp_path):
    store = make_store(tmp_path)
    state = um.PersistedState(
        generation=5,
        current_mode=um.CLIENT,
        target_mode=um.ROUTER_AP,
        last_completed_step="start_hostapd",
        client_snapshot=um.ClientSnapshot(connection_uuid="nm-uuid-1", nm_managed=True, ip_forward=1),
    )
    store.save(state)
    outcome = store.load_strict()
    assert outcome.existed is True
    assert outcome.errors == ()
    assert outcome.state == um.PersistedState(
        generation=5,
        boot_id=BOOT_ID,
        phase=um.SETTLED,
        current_mode=um.CLIENT,
        target_mode=um.ROUTER_AP,
        last_completed_step="start_hostapd",
        client_snapshot=um.ClientSnapshot(connection_uuid="nm-uuid-1", nm_managed=True, ip_forward=1),
        updated_at=CLOCK_ISO,
    )


def test_client_snapshot_immutable():
    snapshot = um.ClientSnapshot(connection_uuid="u", nm_managed=True, ip_forward=0)
    with pytest.raises(Exception):
        snapshot.connection_uuid = "x"
    with pytest.raises(Exception):
        snapshot.nm_managed = False
    with pytest.raises(Exception):
        snapshot.ip_forward = 1


def test_persisted_state_immutable():
    state = um.PersistedState(generation=1)
    with pytest.raises(Exception):
        state.generation = 2
    with pytest.raises(Exception):
        state.client_snapshot = um.ClientSnapshot()


def test_deterministic_dict_conversion():
    snapshot = um.ClientSnapshot(connection_uuid="nm-uuid-1", nm_managed=True, ip_forward=1)
    state = um.PersistedState(
        generation=7,
        boot_id=BOOT_ID,
        phase=um.TRANSITIONING_TO_CLIENT,
        current_mode=um.UNKNOWN,
        source_mode=um.STABLE_BLUETOOTH,
        target_mode=um.CLIENT,
        last_completed_step="stop_bt_services",
        client_snapshot=snapshot,
        updated_at="2026-08-01T08:58:00+00:00",
    )
    expected = {
        "schema_version": 1,
        "generation": 7,
        "boot_id": BOOT_ID,
        "phase": "TRANSITIONING_TO_CLIENT",
        "current_mode": "UNKNOWN",
        "source_mode": "STABLE_BLUETOOTH",
        "target_mode": "CLIENT",
        "last_completed_step": "stop_bt_services",
        "client_snapshot": {
            "connection_uuid": "nm-uuid-1",
            "nm_managed": True,
            "ip_forward": 1,
        },
        "updated_at": "2026-08-01T08:58:00+00:00",
    }
    assert um.client_snapshot_to_dict(snapshot) == expected["client_snapshot"]
    assert um.persisted_state_to_dict(state) == expected
    roundtrip, errors = um.persisted_state_from_dict(expected)
    assert errors == ()
    assert roundtrip == state


def test_written_file_mode_is_exactly_0600(tmp_path):
    store = make_store(tmp_path)
    store.save(um.PersistedState(generation=1))
    assert stat.S_IMODE(os.stat(tmp_path / "state.json").st_mode) == 0o600


def test_parent_directory_permissions(tmp_path):
    store = make_store(tmp_path / "nested" / "deeper")
    store.save(um.PersistedState())
    for directory in (tmp_path / "nested" / "deeper", tmp_path / "nested"):
        mode = stat.S_IMODE(os.stat(directory).st_mode)
        assert mode & 0o077 == 0
        assert mode & 0o700 == 0o700


def test_save_rejects_invalid_state_before_mutation(tmp_path):
    parent = tmp_path / "absent-parent"
    store = make_store(parent / "state.json")
    with pytest.raises(um.StateStoreError):
        store.save(um.PersistedState(generation=-1))
    assert not parent.exists()
    assert not (parent / "state.json").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "state",
    [
        um.PersistedState(schema_version=2),
        um.PersistedState(generation=-1),
        um.PersistedState(phase="INVALID"),
        um.PersistedState(current_mode="INVALID"),
        um.PersistedState(source_mode="INVALID"),
        um.PersistedState(target_mode="INVALID"),
        um.PersistedState(last_completed_step=""),
        um.PersistedState(client_snapshot=um.ClientSnapshot(connection_uuid="")),
        um.PersistedState(client_snapshot=um.ClientSnapshot(nm_managed="yes")),
        um.PersistedState(client_snapshot=um.ClientSnapshot(ip_forward=2)),
    ],
)
def test_save_rejects_invalid_states(tmp_path, state):
    parent = tmp_path / "nested"
    store = make_store(parent / "state.json")
    with pytest.raises(um.StateStoreError):
        store.save(state)
    assert not parent.exists()
    assert not (parent / "state.json").exists()
    assert list(tmp_path.iterdir()) == []


def test_no_temp_file_after_success(tmp_path):
    store = make_store(tmp_path)
    store.save(um.PersistedState())
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_no_temp_file_after_injected_write_failure(tmp_path, monkeypatch):
    store = make_store(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(um.os, "replace", boom)
    with pytest.raises(OSError):
        store.save(um.PersistedState())
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == []


def test_rejected_save_leaves_parent_unchanged(tmp_path):
    parent = tmp_path / "state-dir"
    parent.mkdir()
    os.chmod(parent, 0o755)
    target = tmp_path / "target.json"
    target.write_text(json.dumps(sample_state()))
    os.chmod(target, 0o600)
    target_bytes = target.read_bytes()
    link = parent / "state.json"
    link.symlink_to(target)
    store = um.StateStore(
        state_path=link,
        expected_owner_uid=os.geteuid(),
        boot_id_provider=lambda: BOOT_ID,
        clock_provider=lambda: datetime.fromisoformat(CLOCK_ISO),
    )
    with pytest.raises(um.StateStoreError):
        store.save(um.PersistedState())
    assert stat.S_IMODE(os.stat(parent).st_mode) == 0o755
    assert link.is_symlink()
    assert target.read_bytes() == target_bytes
    assert [p.name for p in parent.iterdir()] == ["state.json"]


def test_transition_journal_saves_and_loads_contextually(tmp_path):
    store = make_store(tmp_path)
    journal = um.PersistedState(
        generation=2,
        phase=um.TRANSITIONING_TO_CLIENT,
        current_mode=um.STABLE_BLUETOOTH,
        source_mode=um.STABLE_BLUETOOTH,
        target_mode=um.CLIENT,
        last_completed_step="gerald_blocked",
        client_snapshot=um.ClientSnapshot(connection_uuid=None, nm_managed=True, ip_forward=0),
    )
    store.save(journal)
    strict = store.load_strict()
    assert any(e.code == "non_settled_phase" for e in strict.errors)
    assert strict.state == store.default_state()
    lenient = store.load_lenient()
    assert any(d.code == "non_settled_phase" for d in lenient.diagnostics)
    assert lenient.state.phase == um.TRANSITIONING_TO_CLIENT
    assert lenient.state.generation == 2
    assert lenient.state.current_mode == um.STABLE_BLUETOOTH


def test_missing_file_returns_default_without_error(tmp_path):
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert outcome.existed is False
    assert outcome.errors == ()
    assert outcome.state == store.default_state()


def test_malformed_json_rejected_by_strict(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    os.chmod(tmp_path / "state.json", 0o600)
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert outcome.existed is True
    assert any(e.code == "malformed_json" for e in outcome.errors)
    assert outcome.state == store.default_state()


def test_malformed_json_available_to_lenient(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    os.chmod(tmp_path / "state.json", 0o600)
    store = make_store(tmp_path)
    outcome = store.load_lenient()
    assert outcome.existed is True
    assert any(d.code == "malformed_json" for d in outcome.diagnostics)
    assert outcome.state == store.default_state()


def test_symlink_rejected_without_following_or_replacing_target(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(sample_state()))
    os.chmod(target, 0o600)
    original = target.read_bytes()
    link = tmp_path / "state.json"
    link.symlink_to(target)
    store = make_store(tmp_path)
    strict = store.load_strict()
    assert any(e.code == "symlink" for e in strict.errors)
    assert strict.state == store.default_state()
    lenient = store.load_lenient()
    assert any(d.code == "symlink" for d in lenient.diagnostics)
    assert lenient.state == store.default_state()
    assert link.is_symlink()
    assert target.read_bytes() == original
    with pytest.raises(um.StateStoreError):
        store.save(um.PersistedState())
    assert link.is_symlink()
    assert target.read_bytes() == original


def test_non_regular_state_path_rejected(tmp_path):
    (tmp_path / "state.json").mkdir()
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "not_regular" for e in outcome.errors)
    assert outcome.state == store.default_state()
    lenient = store.load_lenient()
    assert any(d.code == "not_regular" for d in lenient.diagnostics)
    assert lenient.state == store.default_state()


def test_unsafe_0644_permissions_rejected(tmp_path):
    write_state(tmp_path, sample_state(), mode=0o644)
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "unsafe_permissions" for e in outcome.errors)
    assert outcome.state == store.default_state()
    with pytest.raises(um.StateStoreError):
        store.save(um.PersistedState())
    assert stat.S_IMODE(os.stat(tmp_path / "state.json").st_mode) == 0o644


def test_wrong_owner_rejected(tmp_path):
    write_state(tmp_path, sample_state())
    store = make_store(tmp_path, expected_uid=os.geteuid() + 999999)
    outcome = store.load_strict()
    assert any(e.code == "wrong_owner" for e in outcome.errors)
    assert outcome.state == store.default_state()


def test_regular_file_aggregates_safety_errors(tmp_path):
    write_state(tmp_path, sample_state(), mode=0o644)
    store = make_store(tmp_path, expected_uid=os.geteuid() + 999999)
    strict = store.load_strict()
    codes = [e.code for e in strict.errors]
    assert "wrong_owner" in codes
    assert "unsafe_permissions" in codes
    assert strict.state == store.default_state()
    lenient = store.load_lenient()
    diag_codes = [d.code for d in lenient.diagnostics]
    assert "wrong_owner" in diag_codes
    assert "unsafe_permissions" in diag_codes
    assert lenient.state == store.default_state()


def test_unknown_schema_version_rejected(tmp_path):
    write_state(tmp_path, sample_state(schema_version=2))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "schema_version" for e in outcome.errors)
    assert outcome.state == store.default_state()


def test_stale_boot_id_rejected_by_strict(tmp_path):
    write_state(tmp_path, sample_state(boot_id="stale-boot-1"))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "stale_boot_id" for e in outcome.errors)
    assert outcome.state == store.default_state()


def test_stale_boot_id_tolerated_by_lenient(tmp_path):
    write_state(tmp_path, sample_state(boot_id="stale-boot-1"))
    store = make_store(tmp_path)
    outcome = store.load_lenient()
    assert any(d.code == "stale_boot_id" for d in outcome.diagnostics)
    assert outcome.state.generation == 3
    assert outcome.state.boot_id == "stale-boot-1"


def test_non_settled_phase_rejected_by_strict(tmp_path):
    write_state(tmp_path, sample_state(phase=um.TRANSITIONING_TO_CLIENT))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "non_settled_phase" for e in outcome.errors)
    assert outcome.state == store.default_state()


def test_non_settled_phase_tolerated_by_lenient(tmp_path):
    write_state(tmp_path, sample_state(phase=um.TRANSITIONING_TO_CLIENT))
    store = make_store(tmp_path)
    outcome = store.load_lenient()
    assert any(d.code == "non_settled_phase" for d in outcome.diagnostics)
    assert outcome.state.phase == um.TRANSITIONING_TO_CLIENT


def test_negative_generation_rejected(tmp_path):
    write_state(tmp_path, sample_state(generation=-1))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "generation" for e in outcome.errors)
    assert outcome.state == store.default_state()


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_mode", "WARP"),
        ("source_mode", "WARP"),
        ("target_mode", 7),
        ("current_mode", True),
        ("source_mode", ""),
    ],
)
def test_invalid_modes_rejected(tmp_path, field, value):
    write_state(tmp_path, sample_state(**{field: value}))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert outcome.errors
    assert outcome.state == store.default_state()


@pytest.mark.parametrize(
    "path,value",
    [
        ("connection_uuid", ""),
        ("nm_managed", "yes"),
        ("ip_forward", 2),
        ("ip_forward", True),
    ],
)
def test_invalid_client_snapshot_values_rejected(tmp_path, path, value):
    data = sample_state()
    data["client_snapshot"][path] = value
    write_state(tmp_path, data)
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert outcome.errors
    assert outcome.state == store.default_state()


@pytest.mark.parametrize(
    "value",
    ["", 123, None],
)
def test_empty_or_invalid_updated_at_rejected(tmp_path, value):
    write_state(tmp_path, sample_state(updated_at=value))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "updated_at" for e in outcome.errors)
    assert outcome.state == store.default_state()


def test_unknown_top_level_fields_rejected(tmp_path):
    write_state(tmp_path, sample_state(extra="nope"))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "unknown_field" and e.field == "extra" for e in outcome.errors)
    assert outcome.state == store.default_state()
    lenient = store.load_lenient()
    assert lenient.state == store.default_state()


@pytest.mark.parametrize(
    "secret",
    ["ssid", "psk", "password", "secret", "api_key", "environment", "process_environment"],
)
def test_secret_like_fields_never_accepted(tmp_path, secret):
    write_state(tmp_path, sample_state(**{secret: "supersecret"}))
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert any(e.code == "unknown_field" and e.field == secret for e in outcome.errors)
    assert outcome.state == store.default_state()
    lenient = store.load_lenient()
    assert lenient.state == store.default_state()


def test_unsafe_file_unchanged_after_failed_loading(tmp_path):
    content = json.dumps(sample_state()).encode()
    path = tmp_path / "state.json"
    path.write_bytes(content)
    os.chmod(path, 0o644)
    before = path.read_bytes()
    store = make_store(tmp_path)
    store.load_strict()
    store.load_lenient()
    assert path.read_bytes() == before
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_malformed_file_unchanged_after_failed_loading(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"{oops")
    os.chmod(path, 0o600)
    before = path.read_bytes()
    store = make_store(tmp_path)
    store.load_strict()
    store.load_lenient()
    assert path.read_bytes() == before


def test_schema_version_constant():
    assert um.SCHEMA_VERSION == 1


def test_valid_file_with_settled_phase_round_trips(tmp_path):
    data = sample_state()
    write_state(tmp_path, data)
    store = make_store(tmp_path)
    outcome = store.load_strict()
    assert outcome.existed is True
    assert outcome.errors == ()
    assert outcome.state.generation == 3
    assert outcome.state.current_mode == um.CLIENT
    assert outcome.state.target_mode == um.ROUTER_AP
    assert outcome.state.client_snapshot == um.ClientSnapshot(
        connection_uuid="nm-uuid-1", nm_managed=True, ip_forward=1
    )


def lock_path(tmp_path, name="mode.lock"):
    return tmp_path / name


def test_lock_first_acquire_and_release(tmp_path):
    lock = um.ModeLock(lock_path(tmp_path), os.geteuid())
    assert lock.held is False
    lock.acquire()
    assert lock.held is True
    lock.release()
    assert lock.held is False


def test_second_lock_acquire_is_busy(tmp_path):
    path = lock_path(tmp_path)
    first = um.ModeLock(path, os.geteuid())
    second = um.ModeLock(path, os.geteuid())
    first.acquire()
    assert first.held is True
    with pytest.raises(um.ModeLockBusy):
        second.acquire()
    assert second.held is False
    assert first.held is True
    first.release()


def _child_attempt_acquire(path, expected_uid, queue):
    lock = um.ModeLock(path, expected_uid)
    try:
        lock.acquire()
        held = lock.held
        lock.release()
        queue.put(("ok", held))
    except um.ModeLockBusy:
        queue.put(("busy", False))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def run_lock_child(path, expected_uid):
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_child_attempt_acquire,
        args=(str(path), expected_uid, queue),
    )
    proc.start()
    try:
        result = queue.get(timeout=30)
    except Exception:
        proc.terminate()
        proc.join(timeout=10)
        queue.close()
        queue.join_thread()
        raise
    proc.join(timeout=30)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=10)
        queue.close()
        queue.join_thread()
        pytest.fail("lock child exceeded timeout")
    queue.close()
    queue.join_thread()
    if proc.exitcode != 0:
        pytest.fail(f"lock child exited with code {proc.exitcode}")
    return result


def test_cross_process_contention(tmp_path):
    path = lock_path(tmp_path)
    lock = um.ModeLock(path, os.geteuid())
    lock.acquire()
    assert run_lock_child(path, os.geteuid()) == ("busy", False)
    lock.release()
    assert run_lock_child(path, os.geteuid()) == ("ok", True)
    reacquired = um.ModeLock(path, os.geteuid())
    reacquired.acquire()
    assert reacquired.held is True
    reacquired.release()


def test_lock_rejects_symlink_path_unchanged(tmp_path):
    target = tmp_path / "target.lock"
    target.write_bytes(b"secret-payload")
    os.chmod(target, 0o600)
    link = tmp_path / "mode.lock"
    link.symlink_to(target)
    target_bytes = target.read_bytes()
    before = sorted(p.name for p in tmp_path.iterdir())
    lock = um.ModeLock(link, os.geteuid())
    with pytest.raises(um.ModeLockUnsafe):
        lock.acquire()
    assert lock.held is False
    assert link.is_symlink()
    assert target.read_bytes() == target_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_lock_rejects_directory_at_path(tmp_path):
    path = tmp_path / "mode.lock"
    path.mkdir()
    lock = um.ModeLock(path, os.geteuid())
    with pytest.raises(um.ModeLockUnsafe):
        lock.acquire()
    assert lock.held is False
    assert path.is_dir()
    assert list(path.iterdir()) == []


def test_lock_rejects_wrong_expected_owner(tmp_path):
    lock = um.ModeLock(lock_path(tmp_path), os.geteuid() + 999999)
    with pytest.raises(um.ModeLockUnsafe):
        lock.acquire()
    assert lock.held is False
    assert not lock_path(tmp_path).exists()
    assert list(tmp_path.iterdir()) == []


def test_lock_rejects_mode_0644_unchanged(tmp_path):
    path = lock_path(tmp_path)
    path.write_bytes(b"payload")
    os.chmod(path, 0o644)
    before = path.read_bytes()
    lock = um.ModeLock(path, os.geteuid())
    with pytest.raises(um.ModeLockUnsafe):
        lock.acquire()
    assert lock.held is False
    assert path.read_bytes() == before
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_lock_rejects_symlink_parent(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    os.chmod(real_parent, 0o700)
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    lock = um.ModeLock(link_parent / "mode.lock", os.geteuid())
    with pytest.raises(um.ModeLockUnsafe):
        lock.acquire()
    assert lock.held is False
    assert link_parent.is_symlink()
    assert list(real_parent.iterdir()) == []


def test_lock_rejects_missing_parent(tmp_path):
    parent = tmp_path / "absent"
    lock = um.ModeLock(parent / "mode.lock", os.geteuid())
    with pytest.raises(um.ModeLockUnsafe):
        lock.acquire()
    assert lock.held is False
    assert not parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_lock_missing_path_race_rejects_and_never_mutates(tmp_path):
    lock_path = tmp_path / "mode.lock"
    injected = b"raced-existing-lock-content"
    original_lstat = um.os.lstat
    triggered = False
    before_fd = _fd_count()

    def racing_lstat(path):
        nonlocal triggered
        if os.fspath(path) == os.fspath(lock_path) and not triggered:
            lock_path.write_bytes(injected)
            os.chmod(lock_path, 0o644)
            triggered = True
            raise FileNotFoundError(
                errno.ENOENT,
                os.strerror(errno.ENOENT),
                os.fspath(lock_path),
            )
        return original_lstat(path)

    um.os.lstat = racing_lstat
    lock = um.ModeLock(lock_path, os.geteuid())
    try:
        with pytest.raises(um.ModeLockUnsafe):
            lock.acquire()
    finally:
        um.os.lstat = original_lstat

    assert triggered is True
    assert lock.held is False
    assert lock_path.read_bytes() == injected
    assert stat.S_IMODE(os.stat(lock_path).st_mode) == 0o644
    assert _fd_count() == before_fd


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


def test_no_fd_leak_after_busy_acquisition(tmp_path):
    path = lock_path(tmp_path)
    first = um.ModeLock(path, os.geteuid())
    first.acquire()
    second = um.ModeLock(path, os.geteuid())
    before = _fd_count()
    with pytest.raises(um.ModeLockBusy):
        second.acquire()
    assert _fd_count() == before
    first.release()


def test_no_fd_leak_after_unsafe_rejection(tmp_path):
    path = lock_path(tmp_path)
    path.write_bytes(b"payload")
    os.chmod(path, 0o644)
    lock = um.ModeLock(path, os.geteuid())
    before = _fd_count()
    with pytest.raises(um.ModeLockUnsafe):
        lock.acquire()
    assert _fd_count() == before


def test_release_is_idempotent(tmp_path):
    lock = um.ModeLock(lock_path(tmp_path), os.geteuid())
    lock.release()
    assert lock.held is False
    lock.acquire()
    lock.release()
    lock.release()
    assert lock.held is False


def test_double_acquire_rejected(tmp_path):
    lock = um.ModeLock(lock_path(tmp_path), os.geteuid())
    lock.acquire()
    with pytest.raises(um.ModeLockError):
        lock.acquire()
    assert lock.held is True
    lock.release()


def test_context_manager_releases(tmp_path):
    path = lock_path(tmp_path)
    with um.ModeLock(path, os.geteuid()) as lock:
        assert lock.held is True
        other = um.ModeLock(path, os.geteuid())
        with pytest.raises(um.ModeLockBusy):
            other.acquire()
    assert lock.held is False
    reacquired = um.ModeLock(path, os.geteuid())
    reacquired.acquire()
    reacquired.release()


def test_context_manager_releases_after_exception(tmp_path):
    path = lock_path(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        with um.ModeLock(path, os.geteuid()) as lock:
            assert lock.held is True
            raise RuntimeError("boom")
    assert lock.held is False
    reacquired = um.ModeLock(path, os.geteuid())
    reacquired.acquire()
    reacquired.release()


def test_lock_inode_remains_after_release(tmp_path):
    path = lock_path(tmp_path)
    lock = um.ModeLock(path, os.geteuid())
    lock.acquire()
    before = os.lstat(path)
    lock.release()
    after = os.lstat(path)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert path.exists()
    assert stat.S_IMODE(after.st_mode) == 0o600


@pytest.mark.parametrize(
    "injected_errno",
    [errno.EBADF, errno.EINVAL, errno.EINTR, errno.ENOLCK, errno.EIO],
)
def test_flock_non_contention_errno_raises_lock_error(tmp_path, injected_errno):
    lock_path = tmp_path / "mode.lock"
    original_flock = um.fcntl.flock
    before_fd = _fd_count()

    def failing_flock(fd, operation, code=injected_errno):
        if operation & um.fcntl.LOCK_EX and operation & um.fcntl.LOCK_NB:
            raise OSError(code, "injected non-contention flock failure")
        return original_flock(fd, operation)

    um.fcntl.flock = failing_flock
    lock = um.ModeLock(lock_path, os.geteuid())
    try:
        with pytest.raises(um.ModeLockError) as excinfo:
            lock.acquire()
    finally:
        um.fcntl.flock = original_flock

    assert not isinstance(excinfo.value, um.ModeLockBusy)
    assert lock.held is False
    assert _fd_count() == before_fd


@pytest.mark.parametrize("busy_errno", [errno.EACCES, errno.EAGAIN])
def test_flock_busy_errno_classified_as_busy(tmp_path, busy_errno):
    lock_path = tmp_path / "mode.lock"
    original_flock = um.fcntl.flock

    def failing_flock(fd, operation, code=busy_errno):
        if operation & um.fcntl.LOCK_EX and operation & um.fcntl.LOCK_NB:
            raise OSError(code, "injected real contention")
        return original_flock(fd, operation)

    um.fcntl.flock = failing_flock
    lock = um.ModeLock(lock_path, os.geteuid())
    try:
        with pytest.raises(um.ModeLockBusy):
            lock.acquire()
    finally:
        um.fcntl.flock = original_flock

    assert lock.held is False


def local_console_observation(**kw):
    base = dict(
        interface_name=um.SAFE_MANAGEMENT_IFACE,
        interface_exists=True,
        operstate="up",
        carrier=True,
        addresses=("192.168.1.25/24",),
        default_route_interface=um.SAFE_MANAGEMENT_IFACE,
        ssh_connection_present=False,
        ssh_peer_address=None,
        peer_route_interface=None,
        local_console=True,
    )
    base.update(kw)
    return um.ManagementPathObservation(**base)


def test_local_console_safe_path_allowed():
    result = um.evaluate_management_path(local_console_observation())
    assert result.allowed is True
    assert result.errors == ()


def ssh_observation(**kw):
    base = dict(
        interface_name=um.SAFE_MANAGEMENT_IFACE,
        interface_exists=True,
        operstate="up",
        carrier=True,
        addresses=("192.168.1.25/24",),
        default_route_interface=um.SAFE_MANAGEMENT_IFACE,
        ssh_connection_present=True,
        ssh_peer_address="10.20.30.40",
        peer_route_interface=um.SAFE_MANAGEMENT_IFACE,
        local_console=False,
    )
    base.update(kw)
    return um.ManagementPathObservation(**base)


def test_ssh_safe_path_allowed():
    result = um.evaluate_management_path(ssh_observation())
    assert result.allowed is True
    assert result.errors == ()


def test_ssh_safe_path_allows_private_peer():
    result = um.evaluate_management_path(ssh_observation(ssh_peer_address="192.168.77.10"))
    assert result.allowed is True
    assert result.errors == ()


def test_ssh_safe_path_allows_ipv6_peer():
    result = um.evaluate_management_path(ssh_observation(ssh_peer_address="2001:db8::25"))
    assert result.allowed is True
    assert result.errors == ()


def test_ssh_safe_path_allows_prefixed_own_addresses():
    result = um.evaluate_management_path(
        ssh_observation(addresses=("10.20.30.40/16", "2001:db8::25/64"))
    )
    assert result.allowed is True
    assert result.errors == ()


def gate_codes(result):
    return [e.code for e in result.errors]


def test_gate_wrong_interface():
    result = um.evaluate_management_path(local_console_observation(interface_name="wlan1"))
    assert "wrong_interface" in gate_codes(result)


def test_gate_missing_interface():
    result = um.evaluate_management_path(local_console_observation(interface_exists=False))
    assert "interface_missing" in gate_codes(result)


@pytest.mark.parametrize("operstate", ["down", "unknown", "dormant", None])
def test_gate_operstate_not_up(operstate):
    result = um.evaluate_management_path(local_console_observation(operstate=operstate))
    assert "operstate_not_up" in gate_codes(result)


@pytest.mark.parametrize("carrier", [False, None])
def test_gate_carrier_unproven(carrier):
    result = um.evaluate_management_path(local_console_observation(carrier=carrier))
    assert "carrier_unproven" in gate_codes(result)


def test_gate_no_addresses():
    result = um.evaluate_management_path(local_console_observation(addresses=()))
    assert "no_usable_address" in gate_codes(result)


@pytest.mark.parametrize(
    "addresses",
    [("not-an-ip",), ("192.168.1.25/999",), ("192.168.1.25/24", "garbage")],
)
def test_gate_malformed_addresses(addresses):
    result = um.evaluate_management_path(local_console_observation(addresses=addresses))
    assert "malformed_address" in gate_codes(result)


@pytest.mark.parametrize(
    "addresses",
    [
        ("0.0.0.0/0",),
        ("127.0.0.1/8",),
        ("224.0.0.1/4",),
        ("169.254.10.10/16",),
        ("::1/128",),
        ("ff02::1/8",),
        ("fe80::1/64",),
    ],
)
def test_gate_rejects_non_usable_addresses(addresses):
    result = um.evaluate_management_path(local_console_observation(addresses=addresses))
    assert result.allowed is False
    assert "no_usable_address" in gate_codes(result)


def test_gate_wrong_default_route_interface():
    result = um.evaluate_management_path(local_console_observation(default_route_interface="wlan1"))
    assert "default_route_wrong" in gate_codes(result)


def test_gate_missing_default_route():
    result = um.evaluate_management_path(local_console_observation(default_route_interface=None))
    assert "default_route_wrong" in gate_codes(result)


def test_gate_session_ambiguous():
    result = um.evaluate_management_path(
        local_console_observation(
            ssh_connection_present=True,
            ssh_peer_address="10.0.0.1",
            peer_route_interface=um.SAFE_MANAGEMENT_IFACE,
        )
    )
    assert "session_ambiguous" in gate_codes(result)


def test_gate_management_session_unproven():
    result = um.evaluate_management_path(local_console_observation(local_console=False))
    assert "management_session_unproven" in gate_codes(result)


def test_gate_ssh_peer_missing():
    result = um.evaluate_management_path(ssh_observation(ssh_peer_address=None))
    assert "ssh_peer_missing" in gate_codes(result)
    result = um.evaluate_management_path(ssh_observation(ssh_peer_address=""))
    assert "ssh_peer_missing" in gate_codes(result)


def test_gate_ssh_peer_malformed():
    result = um.evaluate_management_path(ssh_observation(ssh_peer_address="not-an-ip"))
    assert "ssh_peer_malformed" in gate_codes(result)


def test_gate_ssh_peer_route_wrong():
    result = um.evaluate_management_path(ssh_observation(peer_route_interface="wlan1"))
    assert "ssh_peer_route_wrong" in gate_codes(result)
    result = um.evaluate_management_path(ssh_observation(peer_route_interface=None))
    assert "ssh_peer_route_wrong" in gate_codes(result)


def test_gate_local_console_has_ssh_facts():
    result = um.evaluate_management_path(
        local_console_observation(
            ssh_peer_address="10.0.0.1",
            peer_route_interface=um.SAFE_MANAGEMENT_IFACE,
        )
    )
    assert "local_console_has_ssh_facts" in gate_codes(result)


def test_gate_invalid_required_interface():
    result = um.evaluate_management_path(local_console_observation(), required_interface="")
    assert "invalid_required_interface" in gate_codes(result)
    assert "wrong_interface" in gate_codes(result)


def test_gate_aggregates_independent_errors_deterministically():
    observed = um.ManagementPathObservation(
        interface_name="wlan1",
        interface_exists=True,
        operstate="down",
        carrier=False,
        addresses=(),
        default_route_interface="wlan1",
        ssh_connection_present=False,
        ssh_peer_address=None,
        peer_route_interface=None,
        local_console=False,
    )
    result = um.evaluate_management_path(observed)
    assert result.allowed is False
    assert gate_codes(result) == [
        "wrong_interface",
        "operstate_not_up",
        "carrier_unproven",
        "no_usable_address",
        "default_route_wrong",
        "management_session_unproven",
    ]


def test_management_path_observation_immutable():
    obs = local_console_observation()
    with pytest.raises(Exception):
        obs.interface_name = "wlan1"
    with pytest.raises(Exception):
        obs.addresses = ()
    with pytest.raises(Exception):
        obs.local_console = False


def test_management_gate_error_immutable():
    err = um.ManagementGateError("ssh_peer_missing", "missing")
    with pytest.raises(Exception):
        err.code = "other"


def test_management_gate_result_immutable():
    result = um.evaluate_management_path(local_console_observation())
    with pytest.raises(Exception):
        result.allowed = False
    with pytest.raises(Exception):
        result.errors = ()


def test_repeated_evaluation_is_equal_and_deterministic():
    obs = local_console_observation()
    first = um.evaluate_management_path(obs)
    second = um.evaluate_management_path(obs)
    assert first == second
    assert first == um.evaluate_management_path(local_console_observation())
    rejected = um.evaluate_management_path(local_console_observation(operstate="down"))
    assert um.evaluate_management_path(local_console_observation(operstate="down")) == rejected


def test_gate_does_not_mutate_input_observation():
    obs = local_console_observation()
    before = um.ManagementPathObservation(
        interface_name=obs.interface_name,
        interface_exists=obs.interface_exists,
        operstate=obs.operstate,
        carrier=obs.carrier,
        addresses=obs.addresses,
        default_route_interface=obs.default_route_interface,
        ssh_connection_present=obs.ssh_connection_present,
        ssh_peer_address=obs.ssh_peer_address,
        peer_route_interface=obs.peer_route_interface,
        local_console=obs.local_console,
    )
    um.evaluate_management_path(obs)
    assert obs == before
    assert obs.addresses == ("192.168.1.25/24",)


# ---------------------------------------------------------------------------
# Slice 4: read-only observation core.
# ---------------------------------------------------------------------------


def test_read_only_command_result_is_immutable_and_equal():
    result = um.ReadOnlyCommandResult(
        argv=("echo", "hi"), returncode=0, stdout="hi\n", stderr=""
    )
    with pytest.raises(Exception):
        result.returncode = 1
    assert result == um.ReadOnlyCommandResult(
        argv=("echo", "hi"), returncode=0, stdout="hi\n", stderr=""
    )


def test_read_only_host_io_requires_all_injected_callables():
    with pytest.raises(TypeError):
        um.ReadOnlyHostIO()
    io = um.ReadOnlyHostIO(
        run_command=lambda argv, timeout: um.ReadOnlyCommandResult(argv, 0, "", ""),
        read_text=lambda path: "",
        read_link=lambda path: "",
        path_exists=lambda path: False,
        get_environment=lambda key: None,
        local_console_present=lambda: False,
    )
    with pytest.raises(Exception):
        io.run_command = None
    with pytest.raises(Exception):
        io.read_text = None


def test_observation_diagnostic_is_immutable():
    diagnostic = um.ObservationDiagnostic("code", "detail", "source")
    with pytest.raises(Exception):
        diagnostic.code = "other"
    with pytest.raises(Exception):
        diagnostic.detail = "other"
    with pytest.raises(Exception):
        diagnostic.source = "other"


def test_mode_observation_outcome_is_immutable():
    outcome = um.ModeObservationOutcome(observed=obs(), diagnostics=())
    with pytest.raises(Exception):
        outcome.observed = obs(hostapd_active=True)
    with pytest.raises(Exception):
        outcome.diagnostics = (
            um.ObservationDiagnostic("code", "detail", "source"),
        )


def test_management_observation_outcome_is_immutable():
    outcome = um.ManagementObservationOutcome(
        observed=local_console_observation(), diagnostics=()
    )
    with pytest.raises(Exception):
        outcome.observed = local_console_observation(interface_name="wlan1")
    with pytest.raises(Exception):
        outcome.diagnostics = (
            um.ObservationDiagnostic("code", "detail", "source"),
        )


def btmgmt_output(*controllers, header="Index list"):
    lines = [header]
    for index, address in controllers:
        lines.append(f"{index}\tPrimary controller")
        lines.append(f"\taddr {address}")
        lines.append("\tversion 9.0")
        lines.append("\tmanufacturer 2")
    return "\n".join(lines) + "\n"


def test_bt_parser_single_hci0():
    assert um.parse_btmgmt_info(btmgmt_output(("hci0", "38:7A:CC:84:A4:14"))) == (
        ("hci0", "38:7A:CC:84:A4:14"),
    )


@pytest.mark.parametrize(
    "index",
    ["hci0", "hci1", "hci2", "hci7", "hci9", "hci15"],
)
def test_bt_parser_supports_larger_indices(index):
    assert um.parse_btmgmt_info(btmgmt_output((index, "38:7A:CC:84:A4:14"))) == (
        (index, "38:7A:CC:84:A4:14"),
    )


def test_bt_parser_normalizes_lowercase_address_to_uppercase():
    assert um.parse_btmgmt_info(btmgmt_output(("hci0", "38:7a:cc:84:a4:14"))) == (
        ("hci0", "38:7A:CC:84:A4:14"),
    )


def test_bt_parser_preserves_multiple_controller_order():
    text = btmgmt_output(
        ("hci0", "AA:BB:CC:DD:EE:01"),
        ("hci1", "AA:BB:CC:DD:EE:02"),
        ("hci7", "AA:BB:CC:DD:EE:03"),
    )
    assert um.parse_btmgmt_info(text) == (
        ("hci0", "AA:BB:CC:DD:EE:01"),
        ("hci1", "AA:BB:CC:DD:EE:02"),
        ("hci7", "AA:BB:CC:DD:EE:03"),
    )


@pytest.mark.parametrize(
    "address",
    ["ZZ:ZZ:ZZ:ZZ:ZZ:ZZ", "AA:BB:CC:DD:EE", "not-an-address", "AA:BB:CC:DD:EE:FFF"],
)
def test_bt_parser_rejects_malformed_addresses(address):
    assert um.parse_btmgmt_info(btmgmt_output(("hci0", address))) == ()


def test_bt_parser_duplicate_index_keeps_first_record():
    text = (
        "Index list\n"
        "hci0\tPrimary controller\n"
        "\taddr AA:BB:CC:DD:EE:01\n"
        "hci0\tPrimary controller\n"
        "\taddr AA:BB:CC:DD:EE:01\n"
    )
    assert um.parse_btmgmt_info(text) == (("hci0", "AA:BB:CC:DD:EE:01"),)


def test_bt_parser_conflicting_repeated_index_keeps_first_record():
    text = (
        "Index list\n"
        "hci0\tPrimary controller\n"
        "\taddr AA:BB:CC:DD:EE:01\n"
        "hci0\tPrimary controller\n"
        "\taddr AA:BB:CC:DD:EE:02\n"
    )
    assert um.parse_btmgmt_info(text) == (("hci0", "AA:BB:CC:DD:EE:01"),)


def test_bt_parser_ignores_unrelated_text():
    text = (
        "Index list with 2 controllers\n"
        "hci0\tPrimary controller\n"
        "\taddr AA:BB:CC:DD:EE:01\n"
        "\tversion 9.0\n"
        "\tmanufacturer 2\n"
        "hci1\tPrimary controller\n"
        "\taddr AA:BB:CC:DD:EE:02\n"
        "\tstatic-addr 00:00:00:00:00:00\n"
        "\tname 'uConsole'\n"
        "\tpowered 1\n"
    )
    assert um.parse_btmgmt_info(text) == (
        ("hci0", "AA:BB:CC:DD:EE:01"),
        ("hci1", "AA:BB:CC:DD:EE:02"),
    )


def test_bt_parser_empty_input():
    assert um.parse_btmgmt_info("") == ()
    assert um.parse_btmgmt_info("Index list with 0 controllers\n") == ()


def test_bt_parser_does_not_resolve_by_fixed_index():
    text = btmgmt_output(("hci3", "38:7A:CC:84:A4:14"))
    assert um.resolve_bt_controller(um.parse_btmgmt_info(text)) == "hci3"


def test_bt_parser_accepts_real_two_controller_output():
    real_output = (
        "Index list with 2 items\n"
        "hci1:   Primary controller\n"
        "        addr 38:7A:CC:84:A4:14 version 12 manufacturer 70 class 0x6c0000\n"
        "        supported settings: powered connectable fast-connectable discoverable\n"
        "        current settings: powered bondable ssp br/edr le secure-conn\n"
        "        name clockworkpi #2\n"
        "        short name\n"
        "hci0:   Primary controller\n"
        "        addr 2C:CF:67:E5:21:9B version 9 manufacturer 305 class 0x6c0000\n"
        "        supported settings: powered connectable fast-connectable discoverable\n"
        "        current settings: powered bondable ssp br/edr le secure-conn\n"
        "        name clockworkpi\n"
        "        short name\n"
        "hci1:   Configuration options\n"
        "        supported options: public-address\n"
        "        missing options:\n"
        "hci0:   Configuration options\n"
        "        supported options: public-address\n"
        "        missing options:\n"
    )

    controllers = um.parse_btmgmt_info(real_output)

    assert controllers == (
        ("hci1", "38:7A:CC:84:A4:14"),
        ("hci0", "2C:CF:67:E5:21:9B"),
    )
    assert um.resolve_bt_controller(controllers) == "hci1"


def test_bt_parser_preserves_legacy_primary_header():
    text = "hci1   Primary controller\n        addr 38:7A:CC:84:A4:14\n"
    assert um.parse_btmgmt_info(text) == (("hci1", "38:7A:CC:84:A4:14"),)


def test_bt_parser_ignores_configuration_options_section():
    text = (
        "hci1:   Primary controller\n"
        "        addr 38:7A:CC:84:A4:14 version 12 manufacturer 70\n"
        "hci1:   Configuration options\n"
        "        supported options: public-address\n"
    )
    assert um.parse_btmgmt_info(text) == (("hci1", "38:7A:CC:84:A4:14"),)


def test_bt_parser_configuration_header_does_not_create_record():
    text = (
        "hci1:   Primary controller\n"
        "        addr 38:7A:CC:84:A4:14 version 12\n"
        "hci0:   Configuration options\n"
        "        addr 2C:CF:67:E5:21:9B\n"
    )
    records, diagnostics = um._parse_btmgmt_records(text)
    assert records == (("hci1", "38:7A:CC:84:A4:14"),)
    assert diagnostics == []


def test_bt_parser_configuration_header_clears_stale_primary_index():
    text = (
        "hci1:   Primary controller\n"
        "        addr 38:7A:CC:84:A4:14\n"
        "hci0:   Configuration options\n"
        "        supported options: public-address\n"
        "        addr 2C:CF:67:E5:21:9B\n"
    )
    records, diagnostics = um._parse_btmgmt_records(text)
    assert records == (("hci1", "38:7A:CC:84:A4:14"),)
    assert diagnostics == []


@pytest.mark.parametrize(
    "address_line",
    [
        "addr 38:7A:CC:84:A4:14",
        "addr: 38:7A:CC:84:A4:14",
        "addr 38:7A:CC:84:A4:14 version 12 manufacturer 70 class 0x6c0000",
    ],
)
def test_bt_parser_accepts_address_token_variants(address_line):
    text = f"hci1:   Primary controller\n        {address_line}\n"
    assert um.parse_btmgmt_info(text) == (("hci1", "38:7A:CC:84:A4:14"),)


@pytest.mark.parametrize(
    "address_line",
    [
        "addr ZZ:ZZ:ZZ:ZZ:ZZ:ZZ metadata",
        "addr 38:7A:CC:84:A4 metadata",
        "addr not-a-mac version 12",
        "addr:",
    ],
)
def test_bt_parser_rejects_malformed_first_address_tokens(address_line):
    text = f"hci1:   Primary controller\n        {address_line}\n"
    assert um.parse_btmgmt_info(text) == ()


class FakeHostIO:
    def __init__(self, commands=None, texts=None, links=None, env=None, local_console=False):
        self.commands = dict(commands or {})
        self.texts = dict(texts or {})
        self.links = dict(links or {})
        self.env = dict(env or {})
        self.local = local_console
        self.calls = []

    def run_command(self, argv, timeout):
        self.calls.append(("run_command", argv, timeout))
        entry = self.commands.get(argv)
        if entry is None:
            raise AssertionError(f"unexpected command {argv!r}")
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, um.ReadOnlyCommandResult):
            return entry
        returncode, stdout, stderr = entry
        return um.ReadOnlyCommandResult(
            argv=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )

    def read_text(self, path):
        self.calls.append(("read_text", path))
        entry = self.texts.get(path)
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            raise FileNotFoundError(path)
        return entry

    def read_link(self, path):
        self.calls.append(("read_link", path))
        entry = self.links.get(path)
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            raise FileNotFoundError(path)
        return entry

    def path_exists(self, path):
        self.calls.append(("path_exists", path))
        return False

    def get_environment(self, key):
        self.calls.append(("get_environment", key))
        value = self.env.get(key)
        if isinstance(value, Exception):
            raise value
        return value

    def local_console_present(self):
        self.calls.append(("local_console_present",))
        if isinstance(self.local, Exception):
            raise self.local
        return self.local

    def as_io(self):
        return um.ReadOnlyHostIO(
            run_command=self.run_command,
            read_text=self.read_text,
            read_link=self.read_link,
            path_exists=self.path_exists,
            get_environment=self.get_environment,
            local_console_present=self.local_console_present,
        )


IW_MANAGED = (
    "Interface wlan1\n"
    "\tIfindex 4\n"
    "\twdev 0x1\n"
    "\taddr 7a:6c:ac:00:00:01\n"
    "\ttype managed\n"
    "\twiphy 0\n"
)

IW_AP = (
    "Interface wlan1\n"
    "\tIfindex 4\n"
    "\twdev 0x1\n"
    "\taddr 7a:6c:ac:00:00:01\n"
    "\ttype AP\n"
    "\twiphy 0\n"
)

IP_LINK_WLAN1 = (
    "4: wlan1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP "
    "mode DEFAULT group default qlen 1000\n"
)

IP_CLIENT_ADDR = (
    "4: wlan1    inet 10.0.0.5/24 brd 10.0.0.255 scope global dynamic "
    "noprefixroute wlan1\n"
)

IP_ROUTER_ADDR = (
    "4: wlan1    inet 192.168.77.1/24 brd 192.168.77.255 scope global "
    "noprefixroute wlan1\n"
)

WIFI_DRIVER_TARGET = "../../../bus/usb/drivers/mt7921u"
BT_DRIVER_TARGET = "../../../bus/usb/drivers/btusb"


def iptables_check_argv(table, chain, spec):
    argv = ["iptables"]
    if table != "filter":
        argv += ["-t", table]
    argv += ["-C", chain]
    argv += spec.split()
    return tuple(argv)


def all_iptables_argv():
    return tuple(
        iptables_check_argv(table, chain, spec)
        for (table, chain, spec) in um.AP_FIREWALL_RULES
    )


def base_mode_commands(service="inactive"):
    rc = 0 if service == "active" else 3
    out = "active\n" if service == "active" else "inactive\n"
    return {
        ("systemctl", "is-active", "hostapd.service"): (rc, out, ""),
        ("systemctl", "is-active", "dnsmasq.service"): (rc, out, ""),
    }


def client_commands():
    commands = base_mode_commands("inactive")
    commands.update(
        {
            ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1"): (
                0,
                "yes\n",
                "",
            ),
            ("iw", "dev", "wlan1", "info"): (0, IW_MANAGED, ""),
            ("ip", "-o", "-4", "addr", "show", "dev", "wlan1"): (0, IP_CLIENT_ADDR, ""),
            ("ip", "-o", "link", "show", "dev", "wlan1"): (0, IP_LINK_WLAN1, ""),
            ("btmgmt", "info"): (0, btmgmt_output(("hci0", "38:7a:cc:84:a4:14")), ""),
        }
    )
    for argv in all_iptables_argv():
        commands[argv] = (1, "", "")
    return commands


def router_commands():
    commands = base_mode_commands("active")
    commands.update(
        {
            ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1"): (
                0,
                "no\n",
                "",
            ),
            ("iw", "dev", "wlan1", "info"): (0, IW_AP, ""),
            ("ip", "-o", "-4", "addr", "show", "dev", "wlan1"): (0, IP_ROUTER_ADDR, ""),
            ("ip", "-o", "link", "show", "dev", "wlan1"): (0, IP_LINK_WLAN1, ""),
            ("btmgmt", "info"): (0, btmgmt_output(("hci0", "38:7a:cc:84:a4:14")), ""),
        }
    )
    for argv in all_iptables_argv():
        commands[argv] = (0, "", "")
    return commands


def stable_bt_commands():
    commands = base_mode_commands("inactive")
    commands.update(
        {
            ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1"): (
                1,
                "",
                "Error: Device 'wlan1' not found",
            ),
            ("iw", "dev", "wlan1", "info"): (1, "", "command failed: No such device"),
            ("ip", "-o", "-4", "addr", "show", "dev", "wlan1"): (1, "", ""),
            ("ip", "-o", "link", "show", "dev", "wlan1"): (1, "", ""),
            ("btmgmt", "info"): (0, btmgmt_output(("hci0", "38:7a:cc:84:a4:14")), ""),
        }
    )
    for argv in all_iptables_argv():
        commands[argv] = (1, "", "")
    return commands


def full_links(wifi_bound=True, bt_bound=True):
    links = {link: BT_DRIVER_TARGET for link in um.BT_DRIVER_LINKS}
    if not bt_bound:
        links[um.BT_DRIVER_LINKS[1]] = FileNotFoundError(um.BT_DRIVER_LINKS[1])
    if wifi_bound:
        links[um.WIFI_DRIVER_LINK] = WIFI_DRIVER_TARGET
    else:
        links[um.WIFI_DRIVER_LINK] = FileNotFoundError(um.WIFI_DRIVER_LINK)
    return links


def test_observe_client_mode_happy_path():
    fake = FakeHostIO(commands=client_commands(), links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.diagnostics == ()
    assert um.classify_mode(outcome.observed) == um.CLIENT
    assert outcome.observed.hostapd_active is False
    assert outcome.observed.dnsmasq_active is False
    assert outcome.observed.wifi_driver_bound is True
    assert outcome.observed.wlan1_exists is True
    assert outcome.observed.wlan1_managed is True
    assert outcome.observed.wireless_type == "managed"
    assert outcome.observed.ap_addr_present is False
    assert outcome.observed.ap_rules_present is False
    assert outcome.observed.gerald_stopped_or_blocked is True
    assert outcome.observed.bt_drivers_bound == (True, True, True)
    assert outcome.observed.bt_controllers == (("hci0", um.CONTROLLER_BT_ADDR),)


def test_observe_router_ap_mode_happy_path():
    fake = FakeHostIO(commands=router_commands(), links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.diagnostics == ()
    assert um.classify_mode(outcome.observed) == um.ROUTER_AP
    assert outcome.observed.hostapd_active is True
    assert outcome.observed.dnsmasq_active is True
    assert outcome.observed.wifi_driver_bound is True
    assert outcome.observed.wlan1_exists is True
    assert outcome.observed.wlan1_managed is False
    assert outcome.observed.wireless_type == "AP"
    assert outcome.observed.ap_addr_present is True
    assert outcome.observed.ap_rules_present is True
    assert outcome.observed.gerald_stopped_or_blocked is True
    assert outcome.observed.bt_controllers == (("hci0", um.CONTROLLER_BT_ADDR),)


def test_observe_stable_bluetooth_mode_happy_path():
    fake = FakeHostIO(
        commands=stable_bt_commands(), links=full_links(wifi_bound=False)
    )
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=False)
    assert um.classify_mode(outcome.observed) == um.STABLE_BLUETOOTH
    assert outcome.observed.hostapd_active is False
    assert outcome.observed.dnsmasq_active is False
    assert outcome.observed.wifi_driver_bound is False
    assert outcome.observed.wlan1_exists is False
    assert outcome.observed.wlan1_managed is None
    assert outcome.observed.bt_drivers_bound == (True, True, True)
    assert outcome.observed.bt_controllers == (("hci0", um.CONTROLLER_BT_ADDR),)


def diag_codes(outcome):
    return [d.code for d in outcome.diagnostics]


def test_mode_service_command_failure():
    commands = client_commands()
    commands[("systemctl", "is-active", "hostapd.service")] = OSError("injected boom")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.hostapd_active is False
    codes = diag_codes(outcome)
    assert "command_exception" in codes
    assert "service_status_unavailable" in codes
    assert "hostapd_active" in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.UNKNOWN
    assert outcome.diagnostics == (
        um.ObservationDiagnostic(
            "command_exception",
            outcome.diagnostics[0].detail,
            "('systemctl', 'is-active', 'hostapd.service')",
        ),
        um.ObservationDiagnostic(
            "service_status_unavailable",
            outcome.diagnostics[1].detail,
            "('systemctl', 'is-active', 'hostapd.service')",
        ),
    )


def test_mode_missing_driver_link():
    fake = FakeHostIO(commands=client_commands(), links=full_links(wifi_bound=False))
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wifi_driver_bound is False
    codes = diag_codes(outcome)
    assert "driver_link_unavailable" not in codes
    assert "wifi_driver_bound" not in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) != um.CLIENT


def test_mode_wrong_driver_basename():
    links = full_links()
    links[um.WIFI_DRIVER_LINK] = "../../../bus/usb/drivers/ath11k"
    fake = FakeHostIO(commands=client_commands(), links=links)
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wifi_driver_bound is False
    codes = diag_codes(outcome)
    assert "driver_mismatch" in codes
    assert um.classify_mode(outcome.observed) != um.CLIENT


def test_mode_missing_wlan1():
    commands = client_commands()
    commands[("ip", "-o", "link", "show", "dev", "wlan1")] = (1, "", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wlan1_exists is False
    assert um.classify_mode(outcome.observed) != um.CLIENT


@pytest.mark.parametrize("stdout", ["maybe\n", "unmanaged\n", ""])
def test_mode_unknown_nm_managed_output(stdout):
    commands = client_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (
        0,
        stdout,
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wlan1_managed is None
    codes = diag_codes(outcome)
    assert "nm_managed_unavailable" in codes
    assert um.classify_mode(outcome.observed) != um.CLIENT


def test_mode_nm_managed_case_insensitive_yes():
    commands = client_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (
        0,
        "YES\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wlan1_managed is True


@pytest.mark.parametrize(
    "stdout",
    ["", IW_MANAGED + IW_AP, IW_MANAGED.replace("type managed", "type monitor")],
)
def test_mode_missing_duplicate_unsupported_wireless_type(stdout):
    commands = client_commands()
    commands[("iw", "dev", "wlan1", "info")] = (0, stdout, "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wireless_type is None
    codes = diag_codes(outcome)
    assert "wireless_type_unavailable" in codes
    assert um.classify_mode(outcome.observed) != um.CLIENT


def test_mode_wrong_ap_address():
    commands = client_commands()
    commands[("ip", "-o", "-4", "addr", "show", "dev", "wlan1")] = (
        0,
        "4: wlan1    inet 192.168.77.2/24 brd 192.168.77.255 scope global wlan1\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_addr_present is False
    assert um.classify_mode(outcome.observed) != um.ROUTER_AP


def test_mode_one_missing_firewall_rule():
    commands = router_commands()
    commands[all_iptables_argv()[1]] = (1, "", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_rules_present is False
    assert um.classify_mode(outcome.observed) != um.ROUTER_AP


def test_mode_malformed_bluetooth_output_no_records():
    commands = client_commands()
    commands[("btmgmt", "info")] = (0, "garbage output without controllers\n", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.bt_controllers == ()


def test_mode_nonempty_unparseable_bluetooth_output_is_unavailable():
    commands = client_commands()
    commands["btmgmt", "info"] = (0, "Index list with 2 items\nunrecognized controller data\n", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert "bt_controllers" in outcome.observed.unavailable_facts
    assert "bt_controller_info_unavailable" in diag_codes(outcome)
    assert outcome.observed.bt_controllers == ()


def test_mode_explicit_zero_controller_inventory_remains_trusted():
    commands = client_commands()
    commands["btmgmt", "info"] = (0, "Index list with 0 items\n", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.bt_controllers == ()
    assert "bt_controllers" not in outcome.observed.unavailable_facts
    assert "bt_controller_info_unavailable" not in diag_codes(outcome)


def test_mode_embedded_zero_controller_inventory_is_unavailable():
    commands = client_commands()
    commands["btmgmt", "info"] = (
        0,
        "Index list with 0 items\nunrecognized controller data\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.bt_controllers == ()
    assert "bt_controllers" in outcome.observed.unavailable_facts
    assert "bt_controller_info_unavailable" in diag_codes(outcome)


@pytest.mark.parametrize(
    "stdout, unavailable",
    [
        ("  \nIndex list with 0 items\n\t ", False),
        ("Index list with 0 items\nunexpected data\n", True),
        ("prefix\nIndex list with 0 items\n", True),
        ("Index list with 0 items extra\n", True),
    ],
)
def test_mode_zero_inventory_requires_exact_normalized_stdout(stdout, unavailable):
    commands = client_commands()
    commands["btmgmt", "info"] = (0, stdout, "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.bt_controllers == ()
    assert ("bt_controllers" in outcome.observed.unavailable_facts) is unavailable
    assert ("bt_controller_info_unavailable" in diag_codes(outcome)) is unavailable


def test_mode_malformed_bluetooth_record_diagnostic():
    commands = client_commands()
    commands[("btmgmt", "info")] = (0, btmgmt_output(("hci0", "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ")), "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.bt_controllers == ()
    codes = diag_codes(outcome)
    assert "bt_controller_record_invalid" in codes


def test_mode_bt_duplicate_index_diagnostic():
    commands = client_commands()
    text = (
        "Index list\n"
        "hci0\tPrimary controller\n"
        "\taddr 38:7a:cc:84:a4:14\n"
        "hci0\tPrimary controller\n"
        "\taddr 38:7a:cc:84:a4:14\n"
    )
    commands[("btmgmt", "info")] = (0, text, "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.bt_controllers == (("hci0", um.CONTROLLER_BT_ADDR),)
    codes = diag_codes(outcome)
    assert "bt_controller_record_invalid" in codes


def test_mode_one_missing_bt_driver_binding():
    fake = FakeHostIO(
        commands=client_commands(), links=full_links(bt_bound=False)
    )
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.bt_drivers_bound == (True, False, True)
    codes = diag_codes(outcome)
    assert "driver_link_unavailable" not in codes
    assert "bt_drivers_bound" not in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) != um.STABLE_BLUETOOTH


def test_mode_command_result_argv_mismatch():
    commands = client_commands()
    commands[("systemctl", "is-active", "hostapd.service")] = um.ReadOnlyCommandResult(
        argv=("systemctl", "is-active", "ssh.service"),
        returncode=0,
        stdout="active\n",
        stderr="",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.hostapd_active is False
    codes = diag_codes(outcome)
    assert "command_result_argv_mismatch" in codes
    assert "service_status_unavailable" in codes


def test_mode_command_non_integer_returncode():
    commands = client_commands()
    commands[("systemctl", "is-active", "dnsmasq.service")] = um.ReadOnlyCommandResult(
        argv=("systemctl", "is-active", "dnsmasq.service"),
        returncode="0",
        stdout="active\n",
        stderr="",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.dnsmasq_active is False
    codes = diag_codes(outcome)
    assert "command_returncode_invalid" in codes
    assert "service_status_unavailable" in codes


def test_mode_diagnostics_deterministic():
    fake1 = FakeHostIO(
        commands=stable_bt_commands(), links=full_links(wifi_bound=False)
    )
    fake2 = FakeHostIO(
        commands=stable_bt_commands(), links=full_links(wifi_bound=False)
    )
    first = um.observe_mode_state(fake1.as_io(), gerald_stopped_or_blocked=False)
    second = um.observe_mode_state(fake2.as_io(), gerald_stopped_or_blocked=False)
    assert first.diagnostics == second.diagnostics


IP_LINK_ETH1 = (
    "2: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP "
    "mode DEFAULT group default qlen 1000\n"
)

IP_ADDR_ETH1 = (
    "2: eth1    inet 192.168.1.25/24 brd 192.168.1.255 scope global dynamic "
    "noprefixroute eth1\n"
)

IP_DEFAULT_ETH1 = "default via 192.168.1.1 dev eth1 proto dhcp metric 100\n"


def local_console_commands():
    return {
        ("ip", "-o", "link", "show", "dev", um.SAFE_MANAGEMENT_IFACE): (
            0,
            IP_LINK_ETH1,
            "",
        ),
        ("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE): (
            0,
            IP_ADDR_ETH1,
            "",
        ),
        ("ip", "route", "show", "default"): (0, IP_DEFAULT_ETH1, ""),
    }


def local_console_fake():
    return FakeHostIO(
        commands=local_console_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )


def test_observe_local_console_path_allowed():
    fake = local_console_fake()
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.diagnostics == ()
    assert um.evaluate_management_path(outcome.observed).allowed is True
    assert outcome.observed.interface_name == um.SAFE_MANAGEMENT_IFACE
    assert outcome.observed.interface_exists is True
    assert outcome.observed.operstate == "up"
    assert outcome.observed.carrier is True
    assert outcome.observed.addresses == ("192.168.1.25/24",)
    assert outcome.observed.default_route_interface == um.SAFE_MANAGEMENT_IFACE
    assert outcome.observed.ssh_connection_present is False
    assert outcome.observed.ssh_peer_address is None
    assert outcome.observed.peer_route_interface is None
    assert outcome.observed.local_console is True


def ssh_commands():
    commands = local_console_commands()
    commands[("ip", "route", "get", "10.20.30.40")] = (
        0,
        "10.20.30.40 dev eth1 src 192.168.1.25 uid 1000\n",
        "",
    )
    return commands


def ssh_fake():
    return FakeHostIO(
        commands=ssh_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25 22"},
        local_console=False,
    )


def test_observe_ssh_path_allowed():
    fake = ssh_fake()
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.diagnostics == ()
    assert um.evaluate_management_path(outcome.observed).allowed is True
    assert outcome.observed.interface_name == um.SAFE_MANAGEMENT_IFACE
    assert outcome.observed.interface_exists is True
    assert outcome.observed.operstate == "up"
    assert outcome.observed.carrier is True
    assert outcome.observed.addresses == ("192.168.1.25/24",)
    assert outcome.observed.default_route_interface == um.SAFE_MANAGEMENT_IFACE
    assert outcome.observed.ssh_connection_present is True
    assert outcome.observed.ssh_peer_address == "10.20.30.40"
    assert outcome.observed.peer_route_interface == um.SAFE_MANAGEMENT_IFACE
    assert outcome.observed.local_console is False


def test_management_invalid_operstate():
    fake = FakeHostIO(
        commands=local_console_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "bogus\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.operstate is None
    codes = diag_codes(outcome)
    assert "operstate_invalid" in codes
    assert um.evaluate_management_path(outcome.observed).allowed is False


def test_management_invalid_carrier():
    fake = FakeHostIO(
        commands=local_console_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "2\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.carrier is None
    codes = diag_codes(outcome)
    assert "carrier_invalid" in codes


def test_management_missing_interface():
    fake = FakeHostIO(
        commands={
            ("ip", "-o", "link", "show", "dev", um.SAFE_MANAGEMENT_IFACE): (1, "", ""),
            ("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE): (1, "", ""),
            ("ip", "route", "show", "default"): (0, "", ""),
        },
        texts={},
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.interface_exists is False
    assert outcome.observed.operstate is None
    assert outcome.observed.carrier is None
    codes = diag_codes(outcome)
    assert "file_read_unavailable" in codes
    assert "operstate_invalid" in codes
    assert "carrier_invalid" in codes
    assert um.evaluate_management_path(outcome.observed).allowed is False


def test_management_mixed_valid_and_malformed_addresses():
    addr_output = (
        "2: eth1    inet 192.168.1.25/24 brd 192.168.1.255 scope global eth1\n"
        "2: eth1    inet 10.9.9.9/999 scope global eth1\n"
    )
    commands = local_console_commands()
    commands[("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE)] = (
        0,
        addr_output,
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.addresses == ("192.168.1.25/24",)
    codes = diag_codes(outcome)
    assert "address_output_invalid" in codes


def test_management_absent_default_route():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (0, "", "")
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.default_route_interface is None
    codes = diag_codes(outcome)
    assert "default_route_unavailable" in codes


def test_management_metric_priority_existing_fixture_selects_eth1():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (
        0,
        "default via 192.168.1.1 dev eth1 proto dhcp metric 100\n"
        "default via 10.0.0.1 dev wlan1 proto dhcp metric 600\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.default_route_interface == "eth1"
    codes = diag_codes(outcome)
    assert "default_route_ambiguous" not in codes


def test_management_metric_priority_selects_lowest_default_route():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (
        0,
        "default via 192.168.178.1 dev eth1 proto dhcp src 192.168.178.65 metric 100\n"
        "default via 192.168.178.1 dev wlan1 proto dhcp src 192.168.178.120 metric 600\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )

    outcome = um.observe_management_path(fake.as_io())

    assert outcome.observed.default_route_interface == "eth1"
    codes = diag_codes(outcome)
    assert "default_route_unavailable" not in codes
    assert "default_route_ambiguous" not in codes


def _route_outcome(stdout, returncode=0):
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (returncode, stdout, "")
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    return um.observe_management_path(fake.as_io())


def assert_unavailable_route(stdout):
    outcome = _route_outcome(stdout)
    assert outcome.observed.default_route_interface is None
    codes = diag_codes(outcome)
    assert "default_route_ambiguous" in codes
    assert "default_route_unavailable" not in codes


def test_management_metric_priority_reversed_winner_is_not_hard_coded():
    outcome = _route_outcome(
        "default via 192.168.178.1 dev eth1 metric 600\n"
        "default via 192.168.178.1 dev wlan1 metric 100\n"
    )
    assert outcome.observed.default_route_interface == "wlan1"
    assert "default_route_ambiguous" not in diag_codes(outcome)


def test_management_default_route_rejects_slash_interface_token():
    outcome = _route_outcome(
        "default dev bad/name metric 100\n"
        "default dev wlan1 metric 600\n"
    )
    assert outcome.observed.default_route_interface is None
    assert "default_route_ambiguous" in diag_codes(outcome)


def test_management_default_route_rejects_overlong_interface_token():
    outcome = _route_outcome(
        "default dev 0123456789abcdef metric 100\n"
        "default dev wlan1 metric 600\n"
    )
    assert outcome.observed.default_route_interface is None
    assert "default_route_ambiguous" in diag_codes(outcome)


@pytest.mark.parametrize(
    "value",
    ["eth1", "wlan1", "another0", "enp2s0", "br-1234", "tailscale0", "eth_1", "vlan.100", "a", "abcdefghijklmno"],
)
def test_interface_name_validator_accepts_required_names(value):
    assert um._is_valid_interface_name(value) is True


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "bad/name", "bad:name", "eth 1", "eth\t1", "eth\n1", "eth\r1", "0123456789abcdef", "äth1", "eth1\u200b"],
)
def test_interface_name_validator_rejects_required_names(value):
    assert um._is_valid_interface_name(value) is False


@pytest.mark.parametrize(
    "interface",
    ["eth1", "wlan1", "another0", "enp2s0", "br-1234", "tailscale0", "eth_1", "vlan.100", "a", "abcdefghijklmno"],
)
def test_management_route_accepts_required_interface_tokens(interface):
    other = "wlan1" if interface != "wlan1" else "eth1"
    outcome = _route_outcome(
        f"default dev {interface} metric 100\n"
        f"default dev {other} metric 600\n"
    )
    assert outcome.observed.default_route_interface == interface
    assert "default_route_ambiguous" not in diag_codes(outcome)


@pytest.mark.parametrize(
    "interface",
    ["bad/name", "bad:name", "0123456789abcdef", ".", "..", "äth1", "eth1\u200b"],
)
def test_management_route_rejects_invalid_interface_tokens(interface):
    assert_unavailable_route(
        f"default dev {interface} metric 100\n"
        "default dev wlan1 metric 600\n"
    )


def test_management_route_rejects_successful_output_with_injected_line():
    assert_unavailable_route("default dev eth1 metric 100\ninjected\n")


def test_management_route_rejects_stray_positional_token_after_interface():
    assert_unavailable_route(
        "default dev eth 1 metric 100\n"
        "default dev wlan1 metric 600\n"
    )


def test_management_gate_rejects_non_safe_metric_winner_without_gate_change():
    result = um.evaluate_management_path(
        local_console_observation(default_route_interface="wlan1")
    )
    assert result.allowed is False
    assert "default_route_wrong" in gate_codes(result)


def test_management_metric_priority_tie_is_unavailable():
    assert_unavailable_route("default dev eth1 metric 100\ndefault dev wlan1 metric 100\n")


def test_management_metric_priority_same_interface_tie_is_unavailable():
    assert_unavailable_route(
        "default via 192.168.178.1 dev eth1 metric 100\n"
        "default via 192.168.178.254 dev eth1 metric 100\n"
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "default dev eth1\ndefault dev wlan1 metric 600\n",
        "default dev eth1 metric 100\ndefault dev wlan1\n",
    ],
)
def test_management_metric_priority_requires_metrics_for_competing_routes(stdout):
    assert_unavailable_route(stdout)


def test_management_single_default_route_without_metric_remains_available():
    outcome = _route_outcome("default via 192.168.178.1 dev eth1 proto dhcp\n")
    assert outcome.observed.default_route_interface == "eth1"
    assert "default_route_ambiguous" not in diag_codes(outcome)


def test_management_single_default_route_with_metric_remains_available():
    outcome = _route_outcome(
        "default via 192.168.178.1 dev eth1 proto dhcp metric 100\n"
    )
    assert outcome.observed.default_route_interface == "eth1"
    assert "default_route_ambiguous" not in diag_codes(outcome)


@pytest.mark.parametrize(
    "metric_text",
    [
        "metric -1",
        "metric +1",
        "metric 1.0",
        "metric 0x64",
        "metric one-hundred",
        "metric",
        "metric 100 metric 200",
        "metric=100",
        "xmetric 100",
        "metric100",
    ],
)
def test_management_invalid_metric_in_competing_routes_is_unavailable(metric_text):
    assert_unavailable_route(
        f"default dev eth1 {metric_text}\n"
        "default dev wlan1 metric 600\n"
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "default metric 100\ndefault dev wlan1 metric 600\n",
        "default dev\ndefault dev wlan1 metric 600\n",
        "default dev eth1 dev wlan1 metric 100\n"
        "default dev wlan1 metric 600\n",
    ],
)
def test_management_invalid_interface_fields_are_unavailable(stdout):
    assert_unavailable_route(stdout)


def test_management_duplicate_lowest_default_route_is_unavailable():
    assert_unavailable_route(
        "default dev eth1 metric 100\n"
        "default dev eth1 metric 100\n"
        "default dev wlan1 metric 600\n"
    )


def test_management_metric_priority_unique_winner_among_three_routes():
    outcome = _route_outcome(
        "default dev eth1 metric 100\n"
        "default dev wlan1 metric 600\n"
        "default dev another0 metric 700\n"
    )
    assert outcome.observed.default_route_interface == "eth1"


def test_management_metric_priority_unique_non_safe_winner_among_three_routes():
    outcome = _route_outcome(
        "default dev eth1 metric 600\n"
        "default dev wlan1 metric 100\n"
        "default dev another0 metric 700\n"
    )
    assert outcome.observed.default_route_interface == "wlan1"


def test_management_successful_nonempty_malformed_extra_route_line_is_unavailable():
    assert_unavailable_route(
        "default dev eth1 metric 100\n"
        "not a route record\n"
    )


@pytest.mark.parametrize(
    "stdout, returncode",
    [("", 0), ("default dev eth1 metric 100\n", 1)],
)
def test_management_zero_route_and_command_failure_semantics_remain_unavailable(stdout, returncode):
    outcome = _route_outcome(stdout, returncode)
    assert outcome.observed.default_route_interface is None
    codes = diag_codes(outcome)
    assert "default_route_unavailable" in codes


def test_management_malformed_ssh_connection_field_count():
    fake = FakeHostIO(
        commands=local_console_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25"},
        local_console=False,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.ssh_connection_present is True
    assert outcome.observed.ssh_peer_address is None
    assert outcome.observed.peer_route_interface is None
    codes = diag_codes(outcome)
    assert "ssh_connection_invalid" in codes


def test_management_malformed_ssh_connection_invalid_fields():
    fake = FakeHostIO(
        commands=local_console_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "not-an-ip 54812 192.168.1.25 22"},
        local_console=False,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.ssh_connection_present is True
    assert outcome.observed.ssh_peer_address is None
    codes = diag_codes(outcome)
    assert "ssh_connection_invalid" in codes


@pytest.mark.parametrize(
    "value",
    ["not-an-ip 54812 192.168.1.25 22", "10.20.30.40 70000 192.168.1.25 22",
     "10.20.30.40 0 192.168.1.25 22", "10.20.30.40 54812 not-an-ip 22",
     "10.20.30.40 54812 192.168.1.25 abc", "10.20.30.40 54812 192.168.1.25 080"],
)
def test_management_malformed_ssh_connection_parametrized(value):
    fake = FakeHostIO(
        commands=local_console_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": value},
        local_console=False,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.ssh_connection_present is True
    assert outcome.observed.ssh_peer_address is None
    assert outcome.observed.peer_route_interface is None
    codes = diag_codes(outcome)
    assert "ssh_connection_invalid" in codes


def test_management_peer_route_command_failure():
    commands = ssh_commands()
    commands[("ip", "route", "get", "10.20.30.40")] = OSError("peer route boom")
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25 22"},
        local_console=False,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.ssh_peer_address == "10.20.30.40"
    assert outcome.observed.peer_route_interface is None
    codes = diag_codes(outcome)
    assert "peer_route_unavailable" in codes
    assert "command_exception" in codes


def test_management_conflicting_peer_routes():
    commands = ssh_commands()
    commands[("ip", "route", "get", "10.20.30.40")] = (
        0,
        "10.20.30.40 dev eth1 src 192.168.1.25 uid 1000\n"
        "10.20.30.40 dev wlan1 src 10.0.0.5 uid 1000\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25 22"},
        local_console=False,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.peer_route_interface is None
    codes = diag_codes(outcome)
    assert "peer_route_ambiguous" in codes


def test_management_local_console_and_ssh_both_present():
    fake = FakeHostIO(
        commands=ssh_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25 22"},
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.local_console is True
    assert outcome.observed.ssh_connection_present is True
    gate = um.evaluate_management_path(outcome.observed)
    assert gate.allowed is False
    assert "session_ambiguous" in [e.code for e in gate.errors]


def test_mode_observation_exact_injected_requests():
    fake = FakeHostIO(commands=client_commands(), links=full_links())
    um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    expected = [
        ("run_command", ("systemctl", "is-active", "hostapd.service"), 5.0),
        ("run_command", ("systemctl", "is-active", "dnsmasq.service"), 5.0),
        ("read_link", um.WIFI_DRIVER_LINK),
        ("run_command", ("ip", "-o", "link", "show", "dev", "wlan1"), 5.0),
        (
            "run_command",
            ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1"),
            5.0,
        ),
        ("run_command", ("iw", "dev", "wlan1", "info"), 5.0),
        ("run_command", ("ip", "-o", "-4", "addr", "show", "dev", "wlan1"), 5.0),
    ]
    for argv in all_iptables_argv():
        expected.append(("run_command", argv, 5.0))
    expected.extend(
        [
            ("read_link", um.BT_DRIVER_LINKS[0]),
            ("read_link", um.BT_DRIVER_LINKS[1]),
            ("read_link", um.BT_DRIVER_LINKS[2]),
            ("run_command", ("btmgmt", "info"), 5.0),
        ]
    )
    assert fake.calls == expected


def test_mode_observation_exact_injected_driver_links():
    assert um.WIFI_DRIVER_LINK == "/sys/bus/usb/devices/1-1.4.3:1.3/driver"
    assert um.BT_DRIVER_LINKS == (
        "/sys/bus/usb/devices/1-1.4.3:1.0/driver",
        "/sys/bus/usb/devices/1-1.4.3:1.1/driver",
        "/sys/bus/usb/devices/1-1.4.3:1.2/driver",
    )


def test_mode_observation_custom_timeout_injected():
    fake = FakeHostIO(commands=client_commands(), links=full_links())
    um.observe_mode_state(
        fake.as_io(), gerald_stopped_or_blocked=True, command_timeout=7.0
    )
    for call in fake.calls:
        if call[0] == "run_command":
            assert call[2] == 7.0


def test_correction_unavailable_service_blocks_client():
    commands = client_commands()
    commands[("systemctl", "is-active", "hostapd.service")] = OSError("injected boom")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert "hostapd_active" in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


# ---------------------------------------------------------------------------
# Availability contract for ObservedState.unavailable_facts.
# ---------------------------------------------------------------------------

def test_unavailable_facts_default_empty_tuple():
    state = um.ObservedState()
    assert state.unavailable_facts == ()
    assert isinstance(state.unavailable_facts, tuple)


def test_unavailable_facts_rejects_non_tuple():
    with pytest.raises(ValueError):
        um.ObservedState(unavailable_facts=["hostapd_active"])


def test_unavailable_facts_rejects_duplicate():
    with pytest.raises(ValueError):
        um.ObservedState(unavailable_facts=("hostapd_active", "hostapd_active"))


def test_unavailable_facts_rejects_unknown_fact():
    with pytest.raises(ValueError):
        um.ObservedState(unavailable_facts=("not_a_fact",))


def test_unavailable_facts_rejects_mixed_duplicate_and_unknown():
    with pytest.raises(ValueError):
        um.ObservedState(unavailable_facts=("hostapd_active", "not_a_fact", "hostapd_active"))


def test_unavailable_facts_is_immutable():
    state = um.ObservedState(unavailable_facts=("hostapd_active",))
    with pytest.raises(Exception):
        state.unavailable_facts = ()


def test_unavailable_facts_accepts_all_permitted_names():
    facts = tuple(um.PERMITTED_UNAVAILABLE_FACTS)
    state = um.ObservedState(unavailable_facts=facts)
    assert state.unavailable_facts == facts


# ---------------------------------------------------------------------------
# Increment 2: unavailable negative facts prevent STABLE_BLUETOOTH.
# ---------------------------------------------------------------------------

def test_correction_unavailable_negatives_block_stable_bluetooth():
    commands = stable_bt_commands()
    commands[("systemctl", "is-active", "hostapd.service")] = OSError("hostapd boom")
    commands[("systemctl", "is-active", "dnsmasq.service")] = OSError("dnsmasq boom")
    commands[("ip", "-o", "link", "show", "dev", "wlan1")] = OSError("ip boom")
    fake = FakeHostIO(commands=commands, links=full_links(wifi_bound=False))
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=False)
    assert "hostapd_active" in outcome.observed.unavailable_facts
    assert "dnsmasq_active" in outcome.observed.unavailable_facts
    assert "wlan1_exists" in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


def test_correction_unavailable_nm_blocks_stable_bluetooth_when_wlan1_present():
    commands = stable_bt_commands()
    commands[("ip", "-o", "link", "show", "dev", "wlan1")] = (0, IP_LINK_WLAN1, "")
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (
        OSError("nmcli boom")
    )
    fake = FakeHostIO(commands=commands, links=full_links(wifi_bound=False))
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=False)
    assert outcome.observed.wlan1_exists is True
    assert "wlan1_managed" in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


def test_correction_unavailable_nm_ignored_when_wlan1_absent():
    commands = stable_bt_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (
        OSError("nmcli boom")
    )
    fake = FakeHostIO(commands=commands, links=full_links(wifi_bound=False))
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=False)
    assert outcome.observed.wlan1_exists is False
    assert outcome.observed.wlan1_managed is None
    assert "wlan1_managed" not in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.STABLE_BLUETOOTH


# ---------------------------------------------------------------------------
# Increment 3: management-address provenance.
# ---------------------------------------------------------------------------

def test_management_foreign_only_address_ignored():
    addr_output = "3: wlan1    inet 192.168.50.20/24 scope global wlan1\n"
    commands = local_console_commands()
    commands[("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE)] = (
        0,
        addr_output,
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.addresses == ()
    assert um.evaluate_management_path(outcome.observed).allowed is False


def test_management_mixed_foreign_valid_malformed_addresses():
    addr_output = (
        "3: wlan1    inet 10.0.0.5/24 scope global wlan1\n"
        "2: eth1    inet 192.168.50.20/24 scope global eth1\n"
        "2: eth1    inet 10.9.9.9/999 scope global eth1\n"
        "2: eth1@if4    inet6 2001:db8::25/64 scope global\n"
    )
    commands = local_console_commands()
    commands[("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE)] = (
        0,
        addr_output,
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.addresses == (
        "192.168.50.20/24",
        "2001:db8::25/64",
    )
    codes = diag_codes(outcome)
    assert "address_output_invalid" in codes


# ---------------------------------------------------------------------------
# Increment 4: default-route provenance.
# ---------------------------------------------------------------------------

def test_management_nondefault_route_rejected():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (
        0,
        "192.168.50.0/24 dev eth1 proto kernel scope link\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.default_route_interface is None
    codes = diag_codes(outcome)
    assert "default_route_ambiguous" in codes


def test_management_one_valid_default_route():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (
        0,
        "default via 192.168.50.1 dev eth1 proto dhcp\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.default_route_interface == um.SAFE_MANAGEMENT_IFACE


def test_management_repeated_default_routes_same_interface_accepted():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (
        0,
        "default via 192.168.50.1 dev eth1 proto dhcp metric 100\n"
        "default via 192.168.50.1 dev eth1 proto dhcp metric 200\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.default_route_interface == um.SAFE_MANAGEMENT_IFACE


def test_management_conflicting_default_routes():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (
        0,
        "default via 192.168.50.1 dev eth1 proto dhcp\n"
        "default via 10.0.0.1 dev wlan1 proto dhcp\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.default_route_interface is None
    codes = diag_codes(outcome)
    assert "default_route_ambiguous" in codes


# ---------------------------------------------------------------------------
# Increment 5: AP-address provenance.
# ---------------------------------------------------------------------------

def test_mode_ap_foreign_exact_address_ignored():
    commands = router_commands()
    commands[("ip", "-o", "-4", "addr", "show", "dev", "wlan1")] = (
        0,
        "2: eth1    inet 192.168.77.1/24 scope global eth1\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_addr_present is False
    assert "ap_addr_present" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "ap_address_unavailable" in codes
    assert um.classify_mode(outcome.observed) != um.ROUTER_AP


def test_mode_ap_correct_wlan1_address_accepted():
    commands = router_commands()
    commands[("ip", "-o", "-4", "addr", "show", "dev", "wlan1")] = (
        0,
        "4: wlan1    inet 192.168.77.1/24 scope global wlan1\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_addr_present is True
    assert "ap_addr_present" not in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.ROUTER_AP


def test_mode_ap_malformed_wlan1_address_unavailable():
    commands = router_commands()
    commands[("ip", "-o", "-4", "addr", "show", "dev", "wlan1")] = (
        0,
        "4: wlan1    inet 10.9.9.9/999 scope global wlan1\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_addr_present is False
    assert "ap_addr_present" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "ap_address_unavailable" in codes
    assert um.classify_mode(outcome.observed) != um.ROUTER_AP


def test_mode_ap_foreign_malformed_address_ignored():
    commands = router_commands()
    commands[("ip", "-o", "-4", "addr", "show", "dev", "wlan1")] = (
        0,
        "2: eth1    inet 10.9.9.9/999 scope global eth1\n"
        "4: wlan1    inet 192.168.77.1/24 scope global wlan1\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_addr_present is True
    assert "ap_addr_present" not in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.ROUTER_AP


# ---------------------------------------------------------------------------
# Increment 6: failed btmgmt stdout is never trusted.
# ---------------------------------------------------------------------------

def test_mode_failed_btmgmt_returncode_blocks_stable_bluetooth():
    commands = stable_bt_commands()
    valid_stdout = btmgmt_output(("hci0", um.CONTROLLER_BT_ADDR))
    commands[("btmgmt", "info")] = um.ReadOnlyCommandResult(
        argv=("btmgmt", "info"),
        returncode=1,
        stdout=valid_stdout,
        stderr="injected failure",
    )
    fake = FakeHostIO(commands=commands, links=full_links(wifi_bound=False))
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=False)
    assert outcome.observed.bt_controllers == ()
    assert "bt_controllers" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "bt_controller_info_unavailable" in codes
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


def test_mode_btmgmt_provider_exception_blocks_stable_bluetooth():
    commands = stable_bt_commands()
    commands[("btmgmt", "info")] = OSError("btmgmt boom")
    fake = FakeHostIO(commands=commands, links=full_links(wifi_bound=False))
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=False)
    assert outcome.observed.bt_controllers == ()
    assert "bt_controllers" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "bt_controller_info_unavailable" in codes
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


def test_mode_btmgmt_valid_and_malformed_records_marks_unavailable():
    commands = stable_bt_commands()
    text = (
        "Index list\n"
        "hci0\tPrimary controller\n"
        "\taddr 38:7a:cc:84:a4:14\n"
        "hci1\tPrimary controller\n"
        "\taddr ZZ:ZZ:ZZ:ZZ:ZZ:ZZ\n"
    )
    commands[("btmgmt", "info")] = (0, text, "")
    fake = FakeHostIO(commands=commands, links=full_links(wifi_bound=False))
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=False)
    assert outcome.observed.bt_controllers == (("hci0", um.CONTROLLER_BT_ADDR),)
    assert "bt_controllers" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "bt_controller_record_invalid" in codes
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


# ---------------------------------------------------------------------------
# Increment 7: command-specific returncode policies.
# ---------------------------------------------------------------------------

def test_mode_nmcli_nonzero_ignored():
    commands = client_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (
        1,
        "yes\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wlan1_managed is None
    assert "wlan1_managed" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "nm_managed_unavailable" in codes
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


def test_mode_iw_nonzero_ignored():
    commands = client_commands()
    commands[("iw", "dev", "wlan1", "info")] = (1, "\ttypE managed\n", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.wireless_type is None
    assert "wireless_type" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "wireless_type_unavailable" in codes
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


def test_mode_ap_address_nonzero_ignored():
    commands = router_commands()
    commands[("ip", "-o", "-4", "addr", "show", "dev", "wlan1")] = (
        1,
        "4: wlan1    inet 192.168.77.1/24 scope global wlan1\n",
        "",
    )
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_addr_present is False
    assert "ap_addr_present" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "ap_address_unavailable" in codes
    assert um.classify_mode(outcome.observed) != um.ROUTER_AP


def test_management_address_nonzero_ignored():
    commands = local_console_commands()
    commands[("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE)] = (
        1,
        "2: eth1    inet 192.168.1.25/24 scope global eth1\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.addresses == ()
    codes = diag_codes(outcome)
    assert "address_output_invalid" in codes


def test_management_default_route_nonzero_ignored():
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (
        1,
        "default via 192.168.1.1 dev eth1 proto dhcp\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        local_console=True,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.default_route_interface is None
    codes = diag_codes(outcome)
    assert "default_route_unavailable" in codes


def test_management_peer_route_nonzero_ignored():
    commands = ssh_commands()
    commands[("ip", "route", "get", "10.20.30.40")] = (
        1,
        "10.20.30.40 dev eth1 src 192.168.1.25 uid 1000\n",
        "",
    )
    fake = FakeHostIO(
        commands=commands,
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25 22"},
        local_console=False,
    )
    outcome = um.observe_management_path(fake.as_io())
    assert outcome.observed.peer_route_interface is None
    codes = diag_codes(outcome)
    assert "peer_route_unavailable" in codes


def test_mode_iptables_returncode_two_unavailable():
    commands = router_commands()
    commands[all_iptables_argv()[0]] = (2, "", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_rules_present is False
    assert "ap_rules_present" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "firewall_rule_unavailable" in codes
    assert um.classify_mode(outcome.observed) != um.ROUTER_AP


def test_mode_iptables_returncode_one_absent():
    commands = router_commands()
    commands[all_iptables_argv()[0]] = (1, "", "")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert outcome.observed.ap_rules_present is False
    assert "ap_rules_present" not in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "firewall_rule_unavailable" not in codes


# ---------------------------------------------------------------------------
# Increment 8: trust precision.
# ---------------------------------------------------------------------------

def test_correction_unavailable_bt_inventory_does_not_block_client():
    commands = client_commands()
    commands[("btmgmt", "info")] = OSError("btmgmt boom")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert "bt_controllers" in outcome.observed.unavailable_facts
    assert "bt_drivers_bound" not in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.CLIENT


def test_correction_unavailable_ap_rules_do_not_block_client():
    commands = client_commands()
    for argv in all_iptables_argv():
        commands[argv] = OSError("iptables boom")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert "ap_rules_present" in outcome.observed.unavailable_facts
    codes = diag_codes(outcome)
    assert "firewall_rule_unavailable" in codes
    assert um.classify_mode(outcome.observed) == um.CLIENT


def test_correction_unavailable_wireless_type_blocks_client():
    commands = client_commands()
    commands[("iw", "dev", "wlan1", "info")] = OSError("iw boom")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert "wireless_type" in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


def test_correction_unavailable_wireless_type_blocks_router_ap():
    commands = router_commands()
    commands[("iw", "dev", "wlan1", "info")] = OSError("iw boom")
    fake = FakeHostIO(commands=commands, links=full_links())
    outcome = um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    assert "wireless_type" in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.UNKNOWN


# ---------------------------------------------------------------------------
# Continuation of existing management-path request verification.
# ---------------------------------------------------------------------------

def test_management_observation_exact_injected_requests_local():
    fake = local_console_fake()
    um.observe_management_path(fake.as_io())
    assert fake.calls == [
        (
            "run_command",
            ("ip", "-o", "link", "show", "dev", um.SAFE_MANAGEMENT_IFACE),
            5.0,
        ),
        ("read_text", f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate"),
        ("read_text", f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier"),
        (
            "run_command",
            ("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE),
            5.0,
        ),
        ("run_command", ("ip", "route", "show", "default"), 5.0),
        ("get_environment", "SSH_CONNECTION"),
        ("local_console_present",),
    ]


def test_management_observation_exact_injected_requests_ssh():
    fake = ssh_fake()
    um.observe_management_path(fake.as_io())
    assert fake.calls == [
        (
            "run_command",
            ("ip", "-o", "link", "show", "dev", um.SAFE_MANAGEMENT_IFACE),
            5.0,
        ),
        ("read_text", f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate"),
        ("read_text", f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier"),
        (
            "run_command",
            ("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE),
            5.0,
        ),
        ("run_command", ("ip", "route", "show", "default"), 5.0),
        ("get_environment", "SSH_CONNECTION"),
        ("run_command", ("ip", "route", "get", "10.20.30.40"), 5.0),
        ("local_console_present",),
    ]


def test_no_peer_route_requested_without_valid_peer():
    fake = FakeHostIO(
        commands=local_console_commands(),
        texts={
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        },
        env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25"},
        local_console=False,
    )
    um.observe_management_path(fake.as_io())
    kinds = [call[0] for call in fake.calls]
    assert ("run_command", ("ip", "route", "get", "10.20.30.40"), 5.0) not in fake.calls
    assert kinds.count("run_command") == 3


def test_no_retries_no_shell_strings():
    fake = FakeHostIO(commands=client_commands(), links=full_links())
    um.observe_mode_state(fake.as_io(), gerald_stopped_or_blocked=True)
    argv_counts = {}
    for call in fake.calls:
        if call[0] == "run_command":
            argv = call[1]
            argv_counts[argv] = argv_counts.get(argv, 0) + 1
            for token in argv:
                assert token != ""
                assert " " not in token
                assert not any(ch in token for ch in "|;&$`<>*?()[]{}~\"'\\")
    assert all(count == 1 for count in argv_counts.values())


def test_production_module_has_no_forbidden_direct_access():
    source = Path(__file__).resolve().parent.parent / "uconsole_mode.py"
    tree = ast.parse(source.read_text())
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def call_names(node):
        names = []
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name):
                names.append(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                names.append(call.func.attr)
        return names

    assert not set(call_names(functions["observe_mode_state"])) & {"run", "read_text", "readlink", "exists", "which", "ttyname"}
    assert not set(call_names(functions["observe_management_path"])) & {"run", "read_text", "readlink", "exists", "which", "ttyname"}
    assert call_names(functions["_live_run_command"]).count("run") == 1
    assert call_names(functions["_resolve_live_executable"]).count("which") == 1
    assert call_names(functions["_live_read_text"]).count("read_text") == 1
    assert call_names(functions["_live_read_link"]).count("readlink") == 1
    assert call_names(functions["_live_path_exists"]).count("exists") == 1
    assert call_names(functions["_live_get_environment"]).count("get") == 1
    assert call_names(functions["_live_local_console_present"]).count("ttyname") == 1
    forbidden = {"system", "getenv", "eval", "exec", "Popen", "sleep", "kill"}
    assert not any(name in forbidden for node in ast.walk(tree) if isinstance(node, ast.Call) for name in call_names(node))
    assert not any(
        isinstance(keyword, ast.keyword) and keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        for node in ast.walk(tree) if isinstance(node, ast.Call) for keyword in node.keywords
    )


def _direct_management_io(local_console_provider, *, ssh=False, calls=None):
    calls = [] if calls is None else calls
    commands = {
        ("ip", "-o", "link", "show", "dev", um.SAFE_MANAGEMENT_IFACE): um.ReadOnlyCommandResult(
            ("ip", "-o", "link", "show", "dev", um.SAFE_MANAGEMENT_IFACE), 0, IP_LINK_ETH1, ""
        ),
        ("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE): um.ReadOnlyCommandResult(
            ("ip", "-o", "addr", "show", "dev", um.SAFE_MANAGEMENT_IFACE), 0, IP_ADDR_ETH1, ""
        ),
        ("ip", "route", "show", "default"): um.ReadOnlyCommandResult(
            ("ip", "route", "show", "default"), 0, IP_DEFAULT_ETH1, ""
        ),
    }
    if ssh:
        commands[("ip", "route", "get", "10.20.30.40")] = um.ReadOnlyCommandResult(
            ("ip", "route", "get", "10.20.30.40"),
            0,
            "10.20.30.40 dev eth1 src 192.168.1.25 uid 1000\n",
            "",
        )

    def run_command(argv, timeout):
        calls.append(("run_command", argv, timeout))
        return commands[argv]

    def read_text(path):
        calls.append(("read_text", path))
        return {
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n",
            f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n",
        }[path]

    def get_environment(key):
        calls.append(("get_environment", key))
        return "10.20.30.40 54812 192.168.1.25 22" if ssh else None

    def provider():
        calls.append(("local_console_present",))
        return local_console_provider()

    return um.ReadOnlyHostIO(
        run_command=run_command,
        read_text=read_text,
        read_link=lambda path: "",
        path_exists=lambda path: True,
        get_environment=get_environment,
        local_console_present=provider,
    )


def test_local_console_provider_exception_fails_closed():
    calls = []

    def provider():
        raise RuntimeError("injected local-console provider failure")

    outcome = um.observe_management_path(_direct_management_io(provider, calls=calls))
    assert outcome.observed.local_console is False
    codes = diag_codes(outcome)
    assert codes.count("local_console_unavailable") == 1
    diagnostic = next(item for item in outcome.diagnostics if item.code == "local_console_unavailable")
    assert diagnostic.source == "local_console_present"
    assert "RuntimeError" in diagnostic.detail
    assert "injected local-console provider failure" in diagnostic.detail
    assert um.evaluate_management_path(outcome.observed).allowed is False
    assert outcome.observed.interface_exists is True
    assert outcome.observed.operstate == "up"
    assert outcome.observed.carrier is True
    assert outcome.observed.addresses
    assert outcome.observed.default_route_interface == um.SAFE_MANAGEMENT_IFACE
    assert calls.count(("local_console_present",)) == 1


@pytest.mark.parametrize("value", [None, 0, 1, "yes", object()])
def test_local_console_provider_nonboolean_fails_closed(value):
    calls = []
    outcome = um.observe_management_path(
        _direct_management_io(lambda: value, calls=calls)
    )
    assert outcome.observed.local_console is False
    diagnostic_codes = diag_codes(outcome)
    assert diagnostic_codes.count("local_console_unavailable") == 1
    diagnostic = next(item for item in outcome.diagnostics if item.code == "local_console_unavailable")
    assert type(value).__name__ in diagnostic.detail
    assert um.evaluate_management_path(outcome.observed).allowed is False
    assert calls.count(("local_console_present",)) == 1


def test_local_console_provider_true_preserves_exact_bool():
    calls = []
    outcome = um.observe_management_path(
        _direct_management_io(lambda: True, calls=calls)
    )
    assert outcome.observed.local_console is True
    assert "local_console_unavailable" not in diag_codes(outcome)
    assert um.evaluate_management_path(outcome.observed).allowed is True
    assert calls.count(("local_console_present",)) == 1


def test_local_console_provider_false_preserves_exact_bool_for_ssh():
    calls = []
    outcome = um.observe_management_path(
        _direct_management_io(lambda: False, ssh=True, calls=calls)
    )
    assert outcome.observed.local_console is False
    assert "local_console_unavailable" not in diag_codes(outcome)
    assert um.evaluate_management_path(outcome.observed).allowed is True
    assert calls.count(("local_console_present",)) == 1


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(lambda: True, id="valid_true"),
        pytest.param(lambda: (_ for _ in ()).throw(RuntimeError("injected")), id="raised_runtime_error"),
        pytest.param(lambda: "yes", id="invalid_nonboolean"),
    ],
)
def test_local_console_provider_invoked_exactly_once(provider):
    calls = []
    outcome = um.observe_management_path(
        _direct_management_io(provider, calls=calls)
    )
    assert calls.count(("local_console_present",)) == 1
    assert isinstance(outcome.observed.local_console, bool)


def test_live_adapter_public_errors_and_side_effect_free_factory(monkeypatch):
    touched = []
    for name in ("run", "read_text", "readlink", "exists", "env", "stdin", "ttyname", "which"):
        monkeypatch.setattr(um, name, lambda *args, _name=name, **kwargs: touched.append(_name), raising=False)
    first = um.build_live_read_only_host_io()
    second = um.build_live_read_only_host_io()
    assert issubclass(um.ReadOnlyHostIOPolicyError, RuntimeError)
    assert issubclass(um.ReadOnlyHostIOExecutionError, RuntimeError)
    assert um.ReadOnlyHostIOPolicyError is not um.ReadOnlyHostIOExecutionError
    assert isinstance(first, um.ReadOnlyHostIO)
    assert first == second
    assert first is not second
    assert touched == []
    with pytest.raises(Exception):
        first.run_command = lambda argv, timeout: None


def _live_completed(returncode=0, stdout="", stderr=""):
    return type("Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def test_live_btmgmt_info_uses_empty_input_pipe(monkeypatch):
    calls = []
    canonical = ("btmgmt", "info")

    monkeypatch.setattr(
        um.shutil,
        "which",
        lambda executable, path: calls.append(("which", executable, path)) or "/bin/" + executable,
    )
    monkeypatch.setattr(
        um.subprocess,
        "run",
        lambda actual_argv, **kwargs: calls.append(("run", actual_argv, kwargs)) or _live_completed(),
    )

    result = um._live_run_command(canonical, 5.0)

    assert calls[1][2]["input"] == ""
    assert "stdin" not in calls[1][2]
    assert calls[1][2]["timeout"] == 5.0
    assert result.argv == canonical


def test_live_btmgmt_info_preserves_complete_subprocess_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/mock/bin/" + executable)
    monkeypatch.setattr(
        um.subprocess,
        "run",
        lambda actual_argv, **kwargs: calls.append((actual_argv, kwargs)) or _live_completed(
            0, "btmgmt output\n", ""
        ),
    )

    result = um._live_run_command(("btmgmt", "info"), 5.0)

    assert result == um.ReadOnlyCommandResult(("btmgmt", "info"), 0, "btmgmt output\n", "")
    assert calls == [
        (
            ("/mock/bin/btmgmt", "info"),
            {
                "shell": False,
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 5.0,
                "env": dict(um.LIVE_COMMAND_ENVIRONMENT),
                "cwd": "/",
                "close_fds": True,
                "input": "",
            },
        )
    ]


def test_live_non_btmgmt_command_keeps_devnull_contract(monkeypatch):
    calls = []
    canonical = ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", um.WIFI_IFACE)
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/mock/bin/" + executable)
    monkeypatch.setattr(
        um.subprocess,
        "run",
        lambda actual_argv, **kwargs: calls.append((actual_argv, kwargs)) or _live_completed(),
    )

    result = um._live_run_command(canonical, 5.0)

    actual_argv, kwargs = calls[0]
    assert actual_argv == ("/mock/bin/nmcli", *canonical[1:])
    assert result.argv == canonical
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert "input" not in kwargs
    assert kwargs["timeout"] == 5.0
    assert kwargs == {
        "shell": False,
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
        "timeout": 5.0,
        "env": dict(um.LIVE_COMMAND_ENVIRONMENT),
        "cwd": "/",
        "close_fds": True,
    }


def test_live_first_firewall_check_uses_privileged_read_only_transport(monkeypatch):
    calls = []
    canonical = all_iptables_argv()[0]
    monkeypatch.setattr(
        um.shutil,
        "which",
        lambda executable, path: calls.append(("which", executable, path)) or "/mock/bin/" + executable,
    )
    monkeypatch.setattr(
        um.subprocess,
        "run",
        lambda actual_argv, **kwargs: calls.append(("run", actual_argv, kwargs)) or _live_completed(1, "", ""),
    )

    result = um._live_run_command(canonical, 5.0)

    assert calls == [
        ("which", "sudo", um.LIVE_EXECUTABLE_SEARCH_PATH),
        ("which", "iptables", um.LIVE_EXECUTABLE_SEARCH_PATH),
        (
            "run",
            ("/mock/bin/sudo", "-n", "--", "/mock/bin/iptables", *canonical[1:]),
            {
                "shell": False,
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 5.0,
                "env": dict(um.LIVE_COMMAND_ENVIRONMENT),
                "cwd": "/",
                "close_fds": True,
                "stdin": subprocess.DEVNULL,
            },
        ),
    ]
    assert result.argv == canonical


@pytest.mark.parametrize("canonical", all_iptables_argv())
def test_live_all_firewall_checks_use_exact_privileged_transport(monkeypatch, canonical):
    calls = []
    monkeypatch.setattr(
        um.shutil,
        "which",
        lambda executable, path: calls.append(("which", executable)) or "/mock/bin/" + executable,
    )
    monkeypatch.setattr(
        um.subprocess,
        "run",
        lambda actual_argv, **kwargs: calls.append(("run", actual_argv, kwargs)) or _live_completed(1),
    )

    result = um._live_run_command(canonical, 7.5)

    assert calls == [
        ("which", "sudo"),
        ("which", "iptables"),
        (
            "run",
            ("/mock/bin/sudo", "-n", "--", "/mock/bin/iptables", *canonical[1:]),
            calls[2][2],
        ),
    ]
    assert calls[2][2]["stdin"] is subprocess.DEVNULL
    assert "input" not in calls[2][2]
    assert calls[2][2]["timeout"] == 7.5
    assert result.argv == canonical


@pytest.mark.parametrize(
    "argv",
    [
        ("sudo", "-n", "--", "iptables", "-L"),
        ("iptables", "-L"),
        ("iptables", "-A", "FORWARD", "-j", "ACCEPT"),
        ("iptables", "-D", "FORWARD", "1"),
        ("iptables", "-F"),
        tuple(list(all_iptables_argv()[0][:-1]) + ["SNAT"]),
    ],
)
def test_live_firewall_privilege_is_not_broadened(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda *args, **kwargs: calls.append("which"))
    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: calls.append("run"))

    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_run_command(argv, 5.0)

    assert calls == []


@pytest.mark.parametrize("missing", ["sudo", "iptables"])
def test_live_firewall_resolution_failures_are_normalized_without_fallback(monkeypatch, missing):
    calls = []

    def which(executable, path):
        calls.append(("which", executable))
        return None if executable == missing else "/mock/bin/" + executable

    monkeypatch.setattr(um.shutil, "which", which)
    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: calls.append(("run",)))

    canonical = all_iptables_argv()[0]
    with pytest.raises(um.ReadOnlyHostIOExecutionError) as exc_info:
        um._live_run_command(canonical, 5.0)

    assert repr(canonical) in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
    assert calls == [("which", "sudo")] if missing == "sudo" else [("which", "sudo"), ("which", "iptables")]


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("iptables", 5.0), OSError("boom")])
def test_live_firewall_execution_failures_are_normalized_without_retry_or_fallback(monkeypatch, failure):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: calls.append(("which", executable)) or "/mock/bin/" + executable)

    def fail(actual_argv, **kwargs):
        calls.append(("run", actual_argv, kwargs))
        raise failure

    monkeypatch.setattr(um.subprocess, "run", fail)
    canonical = all_iptables_argv()[0]
    with pytest.raises(um.ReadOnlyHostIOExecutionError) as exc_info:
        um._live_run_command(canonical, 5.0)

    assert exc_info.value.__cause__ is failure
    assert len([item for item in calls if item[0] == "run"]) == 1
    assert all(item[0] != "run" or item[1][0].endswith("/sudo") for item in calls)


def test_live_firewall_result_preserves_logical_argv_and_return_data(monkeypatch):
    calls = []
    canonical = all_iptables_argv()[0]
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/mock/bin/" + executable)
    monkeypatch.setattr(
        um.subprocess,
        "run",
        lambda actual_argv, **kwargs: calls.append((actual_argv, kwargs)) or _live_completed(1, "", ""),
    )

    result = um._live_run_command(canonical, 5.0)

    assert result.argv == canonical
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ""
    assert calls[0][0] != canonical


def test_live_btmgmt_info_preserves_success_result_and_call_counts(monkeypatch):
    calls = []
    canonical = ("btmgmt", "info")
    stdout = (
        "Index list with 2 items\n"
        "hci1:   Primary controller\n"
        "        addr 38:7A:CC:84:A4:14 version 12 manufacturer 70 class 0x6c0000\n"
        "hci0:   Primary controller\n"
        "        addr 2C:CF:67:E5:21:9B version 9 manufacturer 305 class 0x6c0000\n"
    )
    monkeypatch.setattr(
        um.shutil,
        "which",
        lambda executable, path: calls.append(("which", executable, path)) or "/mock/bin/" + executable,
    )
    monkeypatch.setattr(
        um.subprocess,
        "run",
        lambda actual_argv, **kwargs: calls.append(("run", actual_argv, kwargs))
        or _live_completed(0, stdout, ""),
    )

    result = um._live_run_command(canonical, 5.0)

    assert result.argv == canonical
    assert result.returncode == 0
    assert result.stdout == stdout
    assert result.stderr == ""
    assert len([call for call in calls if call[0] == "which"]) == 1
    assert len([call for call in calls if call[0] == "run"]) == 1


@pytest.mark.parametrize("failure", [um.subprocess.TimeoutExpired("btmgmt", 5.0), OSError("boom")])
def test_live_btmgmt_info_failures_normalize_without_retry(monkeypatch, failure):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/mock/bin/" + executable)

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise failure

    monkeypatch.setattr(um.subprocess, "run", fail)
    with pytest.raises(um.ReadOnlyHostIOExecutionError) as exc_info:
        um._live_run_command(("btmgmt", "info"), 5.0)
    assert exc_info.value.__cause__ is failure
    assert len(calls) == 1


def test_live_factory_observes_btmgmt_controller_and_client(monkeypatch):
    commands = client_commands()
    commands["btmgmt", "info"] = (
        0,
        "Index list with 2 items\n"
        "hci1:   Primary controller\n"
        "        addr 38:7A:CC:84:A4:14 version 12 manufacturer 70 class 0x6c0000\n"
        "        supported settings: powered connectable fast-connectable discoverable\n"
        "        current settings: powered bondable ssp br/edr le secure-conn\n"
        "        name clockworkpi #2\n"
        "        short name\n"
        "hci0:   Primary controller\n"
        "        addr 2C:CF:67:E5:21:9B version 9 manufacturer 305 class 0x6c0000\n"
        "        supported settings: powered connectable fast-connectable discoverable\n"
        "        current settings: powered bondable ssp br/edr le secure-conn\n"
        "        name clockworkpi\n"
        "        short name\n"
        "hci1:   Configuration options\n"
        "        supported options: public-address\n"
        "        missing options:\n"
        "hci0:   Configuration options\n"
        "        supported options: public-address\n"
        "        missing options:\n",
        "",
    )
    calls = []
    primitive_calls = {"read_text": 0, "readlink": 0, "exists": 0, "environment": 0, "stdin": 0, "ttyname": 0}

    monkeypatch.setattr(
        um.shutil,
        "which",
        lambda executable, path: calls.append(("which", executable, path)) or "/mock/bin/" + executable,
    )

    def run(actual_argv, **kwargs):
        calls.append(("run", actual_argv, kwargs))
        canonical = (actual_argv[0].rsplit("/", 1)[-1], *actual_argv[1:])
        returncode, stdout, stderr = commands[canonical]
        return _live_completed(returncode, stdout, stderr)

    monkeypatch.setattr(um.subprocess, "run", run)
    monkeypatch.setattr(
        um.os,
        "readlink",
        lambda path: primitive_calls.__setitem__("readlink", primitive_calls["readlink"] + 1)
        or full_links()[path],
    )
    monkeypatch.setattr(
        um.Path,
        "read_text",
        lambda self, **kwargs: primitive_calls.__setitem__("read_text", primitive_calls["read_text"] + 1)
        or "",
    )
    monkeypatch.setattr(
        um.os.path,
        "exists",
        lambda path: primitive_calls.__setitem__("exists", primitive_calls["exists"] + 1) or True,
    )
    monkeypatch.setattr(
        um.os.environ,
        "get",
        lambda key: primitive_calls.__setitem__("environment", primitive_calls["environment"] + 1) or None,
    )
    monkeypatch.setattr(um.os, "ttyname", lambda fd: primitive_calls.__setitem__("ttyname", primitive_calls["ttyname"] + 1) or "/dev/tty1")

    outcome = um.observe_mode_state(
        um.build_live_read_only_host_io(),
        gerald_stopped_or_blocked=True,
    )

    assert outcome.observed.bt_controllers == (
        ("hci1", "38:7A:CC:84:A4:14"),
        ("hci0", "2C:CF:67:E5:21:9B"),
    )
    assert "bt_controllers" not in outcome.observed.unavailable_facts
    assert "bt_controller_info_unavailable" not in diag_codes(outcome)
    assert um.resolve_bt_controller(outcome.observed.bt_controllers) == "hci1"
    assert um.classify_mode(outcome.observed) == um.CLIENT
    bt_runs = [call for call in calls if call[0] == "run" and call[1][0].endswith("/btmgmt") and call[1][1:] == ("info",)]
    assert len(bt_runs) == 1
    assert bt_runs[0][2]["input"] == ""
    assert "stdin" not in bt_runs[0][2]
    assert bt_runs[0][2]["timeout"] == 5.0
    for _, actual_argv, kwargs in [call for call in calls if call[0] == "run"]:
        if actual_argv[0].endswith("/btmgmt") and actual_argv[1:] == ("info",):
            continue
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert "input" not in kwargs
    assert primitive_calls == {"read_text": 0, "readlink": 4, "exists": 0, "environment": 0, "stdin": 0, "ttyname": 0}


def test_live_command_allowlist_and_dynamic_ip_literals(monkeypatch):
    seen = []
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: seen.append((executable, path)) or "/bin/" + executable)
    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: _live_completed())
    commands = set(um._LIVE_STATIC_COMMANDS) | {
        ("ip", "route", "get", "192.0.2.10"),
        ("ip", "route", "get", "2001:db8::10"),
    }
    for argv in commands:
        um._live_run_command(argv, 5)
    assert len(seen) == len(commands) + 3


def test_nm_managed_requests_current_get_command():
    calls = []
    canonical = (
        "nmcli",
        "-g",
        "GENERAL.NM-MANAGED",
        "device",
        "show",
        um.WIFI_IFACE,
    )

    def run_command(argv, timeout):
        calls.append((argv, timeout))
        return um.ReadOnlyCommandResult(canonical, 0, "yes\n", "")

    io = um.ReadOnlyHostIO(
        run_command=run_command,
        read_text=lambda path: "",
        read_link=lambda path: "",
        path_exists=lambda path: False,
        get_environment=lambda key: None,
        local_console_present=lambda: False,
    )

    managed, unavailable, diagnostics = um._nm_managed(io, 5)

    assert calls == [(canonical, 5)]
    assert managed is True
    assert unavailable is None
    assert diagnostics == []


def test_live_current_nmcli_get_form_is_allowlisted(monkeypatch):
    calls = []
    new_argv = (
        "nmcli",
        "-g",
        "GENERAL.NM-MANAGED",
        "device",
        "show",
        um.WIFI_IFACE,
    )
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: calls.append(("which", executable, path)) or "/mock/bin/" + executable)
    monkeypatch.setattr(um.subprocess, "run", lambda actual_argv, **kwargs: calls.append(("run", actual_argv)) or _live_completed())

    result = um._live_run_command(new_argv, 5)

    assert result.argv == new_argv
    assert calls == [
        ("which", "nmcli", um.LIVE_EXECUTABLE_SEARCH_PATH),
        ("run", ("/mock/bin/nmcli", *new_argv[1:])),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ("nmcli", "-t", "-f", "GENERAL.MANAGED", "device", "show", um.WIFI_IFACE),
        ("nmcli", "-t", "-f", "GENERAL.NM-MANAGED", "device", "show", um.WIFI_IFACE),
    ],
    ids=["legacy-field", "labelled-current"],
)
def test_live_nmcli_noncanonical_forms_rejected_before_primitives(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda *args, **kwargs: calls.append("which"))
    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: calls.append("run"))

    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_run_command(argv, 5)

    assert calls == []


@pytest.mark.parametrize("stdout", ["yes", "YES", " yes\n", "true", "1"])
def test_nm_managed_plain_true_values(stdout):
    commands = client_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (0, stdout, "")
    outcome = um.observe_mode_state(FakeHostIO(commands=commands, links=full_links()).as_io(), True)
    assert outcome.observed.wlan1_managed is True
    assert "wlan1_managed" not in outcome.observed.unavailable_facts


@pytest.mark.parametrize("stdout", ["no", "NO", " no\n", "false", "0"])
def test_nm_managed_plain_false_values(stdout):
    commands = client_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (0, stdout, "")
    outcome = um.observe_mode_state(FakeHostIO(commands=commands, links=full_links()).as_io(), True)
    assert outcome.observed.wlan1_managed is False
    assert "wlan1_managed" not in outcome.observed.unavailable_facts


@pytest.mark.parametrize(
    "stdout",
    ["GENERAL.NM-MANAGED:yes", "GENERAL.MANAGED:yes", "unknown", "", "yes\nno\n"],
    ids=["labelled-current", "labelled-legacy", "unknown", "empty", "multiple-lines"],
)
def test_nm_managed_invalid_output_is_unavailable(stdout):
    commands = client_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (0, stdout, "")
    outcome = um.observe_mode_state(FakeHostIO(commands=commands, links=full_links()).as_io(), True)
    assert outcome.observed.wlan1_managed is None
    assert "wlan1_managed" in outcome.observed.unavailable_facts
    assert "nm_managed_unavailable" in diag_codes(outcome)


def test_nm_managed_new_command_nonzero_is_fail_closed():
    commands = client_commands()
    commands[("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan1")] = (2, "", "injected failure")
    outcome = um.observe_mode_state(FakeHostIO(commands=commands, links=full_links()).as_io(), True)
    assert outcome.observed.wlan1_managed is None
    assert "wlan1_managed" in outcome.observed.unavailable_facts
    assert "nm_managed_unavailable" in diag_codes(outcome)
    assert um.classify_mode(outcome.observed) != um.CLIENT


@pytest.mark.parametrize("argv", [
    (), [], "ip route show default", ("ip", "route", "get", "host.example"),
    ("ip", "route", "get", "192.0.2.1/32"), ("ip", "route", "get", "fe80::1%eth1"),
    ("sudo", "ip", "route", "show", "default"), ("systemctl", "start", "hostapd.service"),
    ("ip", "route", "add", "192.0.2.0/24"), ("iptables", "-A", "FORWARD"),
    ("sh", "-c", "true"), ("ip", "route", "show", "default", ";"),
    ("ip", "route", "get", "192.0.2.10", "extra"),
])
def test_live_rejected_commands_do_not_resolve_or_execute(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda *args, **kwargs: calls.append("which"))
    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: calls.append("run"))
    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_run_command(argv, 5)
    assert calls == []


@pytest.mark.parametrize("timeout", [0, -1, True, False, None, "5", float("nan"), float("inf"), -float("inf"), 30.0001])
def test_live_invalid_timeout_precedes_resolution(monkeypatch, timeout):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda *args, **kwargs: calls.append("which"))
    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_run_command(("ip", "route", "show", "default"), timeout)
    assert calls == []


@pytest.mark.parametrize("timeout", [0.001, 5, 5.0, 30, 30.0])
def test_live_valid_timeout_and_subprocess_contract(monkeypatch, timeout):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/sbin/" + executable)
    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or _live_completed(7, "out", "err"))
    result = um._live_run_command(("ip", "route", "show", "default"), timeout)
    assert result == um.ReadOnlyCommandResult(("ip", "route", "show", "default"), 7, "out", "err")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (("/sbin/ip", "route", "show", "default"),)
    assert kwargs == {
        "shell": False, "check": False, "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace", "stdin": um.subprocess.DEVNULL,
        "timeout": float(timeout), "env": dict(um.LIVE_COMMAND_ENVIRONMENT),
        "cwd": "/", "close_fds": True,
    }


@pytest.mark.parametrize("resolved", ["ip", "/bin/other"])
def test_live_executable_resolution_requires_absolute_matching_basename(monkeypatch, resolved):
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: resolved)
    with pytest.raises(um.ReadOnlyHostIOExecutionError):
        um._live_run_command(("ip", "route", "show", "default"), 5)


@pytest.mark.parametrize("completion", [
    _live_completed(True, "", ""), _live_completed("0", "", ""),
    _live_completed(0, b"", ""), _live_completed(0, None, ""),
    _live_completed(0, "", b""), _live_completed(0, "", None),
])
def test_live_malformed_completion_is_execution_error(monkeypatch, completion):
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/bin/" + executable)
    monkeypatch.setattr(um.subprocess, "run", lambda *args, **kwargs: completion)
    with pytest.raises(um.ReadOnlyHostIOExecutionError):
        um._live_run_command(("ip", "route", "show", "default"), 5)


@pytest.mark.parametrize("failure", [um.subprocess.TimeoutExpired("ip", 5), FileNotFoundError(), PermissionError(), OSError("boom")])
def test_live_execution_failures_are_normalized_without_retry(monkeypatch, failure):
    calls = []
    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/bin/" + executable)
    def fail(*args, **kwargs):
        calls.append(1)
        raise failure
    monkeypatch.setattr(um.subprocess, "run", fail)
    with pytest.raises(um.ReadOnlyHostIOExecutionError):
        um._live_run_command(("ip", "route", "show", "default"), 5)
    assert calls == [1]


def test_live_file_and_environment_policies_use_one_primitive_call(monkeypatch):
    text_calls, link_calls, exists_calls, env_calls = [], [], [], []
    monkeypatch.setattr(um.Path, "read_text", lambda self, **kwargs: text_calls.append((str(self), kwargs)) or "up\n")
    monkeypatch.setattr(um.os, "readlink", lambda path: link_calls.append(path) or "/sys/bus/usb/drivers/mt7921u")
    monkeypatch.setattr(um.os.path, "exists", lambda path: exists_calls.append(path) or True)
    monkeypatch.setattr(um.os.environ, "get", lambda key: env_calls.append(key) or "ssh")
    operstate = f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate"
    assert um._live_read_text(operstate) == "up\n"
    assert um._live_read_link(um.WIFI_DRIVER_LINK).endswith("mt7921u")
    assert um._live_path_exists(um.WIFI_DRIVER_LINK) is True
    assert um._live_get_environment("SSH_CONNECTION") == "ssh"
    assert len(text_calls) == len(link_calls) == len(exists_calls) == len(env_calls) == 1


@pytest.mark.parametrize("path", ["/etc/passwd", "../operstate", ".", "~", "//sys/class/net/eth1/operstate", "/sys/class/net/eth1/operstate/", "relative", "bad\x00path"])
def test_live_unauthorized_file_paths_reject_before_primitive(monkeypatch, path):
    calls = []
    monkeypatch.setattr(um.Path, "read_text", lambda *args, **kwargs: calls.append("text"))
    monkeypatch.setattr(um.os, "readlink", lambda *args, **kwargs: calls.append("link"))
    monkeypatch.setattr(um.os.path, "exists", lambda *args, **kwargs: calls.append("exists"))
    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_read_text(path)
    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_read_link(path)
    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_path_exists(path)
    assert calls == []


def test_live_environment_policy_rejects_everything_except_ssh(monkeypatch):
    calls = []
    monkeypatch.setattr(um.os.environ, "get", lambda key: calls.append(key) or None)
    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_get_environment("HOME")
    with pytest.raises(um.ReadOnlyHostIOPolicyError):
        um._live_get_environment("SSH_CONNECTION" + " ")
    assert calls == []


@pytest.mark.parametrize("method", ["_live_read_text", "_live_read_link", "_live_path_exists"])
def test_live_malformed_file_results_are_execution_errors(monkeypatch, method):
    if method == "_live_read_text":
        monkeypatch.setattr(um.Path, "read_text", lambda *args, **kwargs: None)
        args = (f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate",)
    elif method == "_live_read_link":
        monkeypatch.setattr(um.os, "readlink", lambda path: None)
        args = (um.WIFI_DRIVER_LINK,)
    else:
        monkeypatch.setattr(um.os.path, "exists", lambda path: 1)
        args = (um.WIFI_DRIVER_LINK,)
    with pytest.raises(um.ReadOnlyHostIOExecutionError):
        getattr(um, method)(*args)


class _FakeStdin:
    def __init__(self, isatty=True, fd=0, error=None):
        self._isatty, self._fd, self._error = isatty, fd, error
        self.calls = []
    def isatty(self):
        self.calls.append("isatty")
        if self._error:
            raise self._error
        return self._isatty
    def fileno(self):
        self.calls.append("fileno")
        return self._fd


@pytest.mark.parametrize("env_value", ["", "malformed ssh facts"])
def test_live_local_console_ssh_presence_has_precedence(monkeypatch, env_value):
    stdin = _FakeStdin()
    tty_calls = []
    monkeypatch.setattr(um.os.environ, "get", lambda key: env_value)
    monkeypatch.setattr(um.sys, "stdin", stdin)
    monkeypatch.setattr(um.os, "ttyname", lambda fd: tty_calls.append(fd) or "/dev/tty1")
    assert um._live_local_console_present() is False
    assert stdin.calls == []
    assert tty_calls == []


def test_live_local_console_false_is_definitive_without_fileno(monkeypatch):
    stdin = _FakeStdin(isatty=False)
    monkeypatch.setattr(um.os.environ, "get", lambda key: None)
    monkeypatch.setattr(um.sys, "stdin", stdin)
    assert um._live_local_console_present() is False
    assert stdin.calls == ["isatty"]


@pytest.mark.parametrize("tty", ["/dev/tty0", "/dev/tty1", "/dev/tty12"])
def test_live_local_console_accepts_exact_tty_pattern(monkeypatch, tty):
    stdin = _FakeStdin()
    calls = []
    monkeypatch.setattr(um.os.environ, "get", lambda key: None)
    monkeypatch.setattr(um.sys, "stdin", stdin)
    monkeypatch.setattr(um.os, "ttyname", lambda fd: calls.append(fd) or tty)
    assert um._live_local_console_present() is True
    assert stdin.calls == ["isatty", "fileno"]
    assert calls == [0]


@pytest.mark.parametrize("tty", ["/dev/tty", "/dev/console", "/dev/pts/0", "/dev/pts/12", "/dev/ptmx", "", "tty1", "/dev/tty 1"])
def test_live_local_console_rejects_non_local_tty_names(monkeypatch, tty):
    stdin = _FakeStdin()
    monkeypatch.setattr(um.os.environ, "get", lambda key: None)
    monkeypatch.setattr(um.sys, "stdin", stdin)
    monkeypatch.setattr(um.os, "ttyname", lambda fd: tty)
    assert um._live_local_console_present() is False


@pytest.mark.parametrize("stdin", [_FakeStdin(isatty="yes"), _FakeStdin(isatty=True, fd=True), _FakeStdin(isatty=True, fd=-1), _FakeStdin(isatty=True, fd="0")])
def test_live_local_console_invalid_stdin_facts_are_execution_errors(monkeypatch, stdin):
    monkeypatch.setattr(um.os.environ, "get", lambda key: None)
    monkeypatch.setattr(um.sys, "stdin", stdin)
    with pytest.raises(um.ReadOnlyHostIOExecutionError):
        um._live_local_console_present()


@pytest.mark.parametrize("failure", [RuntimeError("env"), RuntimeError("fileno"), RuntimeError("ttyname")])
def test_live_local_console_unexpected_failures_are_execution_errors(monkeypatch, failure):
    if str(failure) == "env":
        monkeypatch.setattr(um.os.environ, "get", lambda key: (_ for _ in ()).throw(failure))
        monkeypatch.setattr(um.sys, "stdin", _FakeStdin())
    elif str(failure) == "fileno":
        stdin = _FakeStdin()
        stdin.fileno = lambda: (_ for _ in ()).throw(failure)
        monkeypatch.setattr(um.os.environ, "get", lambda key: None)
        monkeypatch.setattr(um.sys, "stdin", stdin)
    else:
        monkeypatch.setattr(um.os.environ, "get", lambda key: None)
        monkeypatch.setattr(um.sys, "stdin", _FakeStdin())
        monkeypatch.setattr(um.os, "ttyname", lambda fd: (_ for _ in ()).throw(failure))
    with pytest.raises(um.ReadOnlyHostIOExecutionError):
        um._live_local_console_present()


def _mock_live_factory(monkeypatch, commands, links, *, env=None, tty="/dev/tty1", counters=None, requests=None, actual_calls=None):
    counters = counters if counters is not None else {name: 0 for name in ("subprocess", "file", "link", "exists", "env", "stdin", "tty")}
    command_map = dict(commands)
    def which(executable, path):
        return "/mock/bin/" + executable
    def run(actual_argv, **kwargs):
        counters["subprocess"] += 1
        if actual_calls is not None:
            actual_calls.append((actual_argv, kwargs))
        if actual_argv[0].endswith("/sudo"):
            assert actual_argv[1:4] == ("-n", "--", "/mock/bin/iptables")
            canonical = ("iptables", *actual_argv[4:])
        else:
            canonical = (actual_argv[0].rsplit("/", 1)[-1], *actual_argv[1:])
        if requests is not None:
            requests.append(canonical)
        entry = command_map[canonical]
        if isinstance(entry, um.ReadOnlyCommandResult):
            return _live_completed(entry.returncode, entry.stdout, entry.stderr)
        return _live_completed(*entry)
    monkeypatch.setattr(um.shutil, "which", which)
    monkeypatch.setattr(um.subprocess, "run", run)
    monkeypatch.setattr(um.Path, "read_text", lambda path, **kwargs: counters.__setitem__("file", counters["file"] + 1) or {f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate": "up\n", f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/carrier": "1\n"}[str(path)])
    def readlink(path):
        counters["link"] += 1
        value = links[path]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(um.os, "readlink", readlink)
    monkeypatch.setattr(um.os.path, "exists", lambda path: counters.__setitem__("exists", counters["exists"] + 1) or True)
    monkeypatch.setattr(um.os.environ, "get", lambda key: counters.__setitem__("env", counters["env"] + 1) or (env or {}).get(key))
    stdin = _FakeStdin()
    monkeypatch.setattr(um.sys, "stdin", stdin)
    monkeypatch.setattr(um.os, "ttyname", lambda fd: counters.__setitem__("tty", counters["tty"] + 1) or tty)
    return um.build_live_read_only_host_io(), counters, stdin


@pytest.mark.parametrize("returncodes", [(1, 1, 1), (0, 0, 0), (0, 1, 1)])
def test_ap_firewall_semantics_remain_available_for_zero_one_results(returncodes):
    commands = client_commands()
    for argv, returncode in zip(all_iptables_argv(), returncodes):
        commands[argv] = (returncode, "", "")

    outcome = um.observe_mode_state(
        FakeHostIO(commands=commands, links=full_links()).as_io(),
        gerald_stopped_or_blocked=True,
    )

    expected_present = returncodes == (0, 0, 0)
    assert outcome.observed.ap_rules_present is expected_present
    assert "ap_rules_present" not in outcome.observed.unavailable_facts
    assert "firewall_rule_unavailable" not in diag_codes(outcome)


@pytest.mark.parametrize("returncode", [2, 4, 126, 127])
def test_ap_firewall_other_returncodes_remain_unavailable(returncode):
    commands = client_commands()
    commands[all_iptables_argv()[0]] = (returncode, "", "")

    outcome = um.observe_mode_state(
        FakeHostIO(commands=commands, links=full_links()).as_io(),
        gerald_stopped_or_blocked=True,
    )

    assert outcome.observed.ap_rules_present is False
    assert "ap_rules_present" in outcome.observed.unavailable_facts
    assert "firewall_rule_unavailable" in diag_codes(outcome)


def test_live_factory_privileged_firewall_checks_preserve_client_and_read_only_contract(monkeypatch):
    commands = client_commands()
    commands["btmgmt", "info"] = (
        0,
        "Index list with 2 items\n"
        "hci1:   Primary controller\n"
        "        addr 38:7A:CC:84:A4:14 version 12\n"
        "hci0:   Primary controller\n"
        "        addr 2C:CF:67:E5:21:9B version 9\n",
        "",
    )
    actual_calls = []
    io, counters, _ = _mock_live_factory(
        monkeypatch,
        commands,
        full_links(),
        env={},
        actual_calls=actual_calls,
    )

    outcome = um.observe_mode_state(io, gerald_stopped_or_blocked=True)

    firewall_calls = [
        (argv, kwargs)
        for argv, kwargs in actual_calls
        if argv[0].endswith("/sudo")
    ]
    assert len(firewall_calls) == 3
    assert {
        ("iptables", *argv[4:]) for argv, _ in firewall_calls
    } == set(all_iptables_argv())
    for argv, kwargs in firewall_calls:
        assert argv[:3] == ("/mock/bin/sudo", "-n", "--")
        assert argv[3] == "/mock/bin/iptables"
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert "input" not in kwargs
        assert kwargs["timeout"] == 5.0

    non_bt_calls = [
        (argv, kwargs)
        for argv, kwargs in actual_calls
        if not argv[0].endswith("/btmgmt") and not argv[0].endswith("/sudo")
    ]
    assert non_bt_calls
    for argv, kwargs in non_bt_calls:
        assert not argv[0].endswith("/sudo")
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert "input" not in kwargs

    bt_calls = [(argv, kwargs) for argv, kwargs in actual_calls if argv[0].endswith("/btmgmt")]
    assert len(bt_calls) == 1
    assert bt_calls[0][1]["input"] == ""
    assert "stdin" not in bt_calls[0][1]
    assert bt_calls[0][1]["timeout"] == 5.0
    assert outcome.observed.ap_rules_present is False
    assert "ap_rules_present" not in outcome.observed.unavailable_facts
    assert "firewall_rule_unavailable" not in diag_codes(outcome)
    assert outcome.observed.bt_controllers == (
        ("hci1", "38:7A:CC:84:A4:14"),
        ("hci0", "2C:CF:67:E5:21:9B"),
    )
    assert um.resolve_bt_controller(outcome.observed.bt_controllers) == "hci1"
    assert um.classify_mode(outcome.observed) == um.CLIENT
    assert counters["subprocess"] == len(actual_calls)


def test_live_factory_client_uses_only_current_nmcli_and_classifies_client(monkeypatch):
    requests = []
    io, counters, _ = _mock_live_factory(
        monkeypatch,
        client_commands(),
        full_links(),
        env={},
        requests=requests,
    )

    outcome = um.observe_mode_state(io, gerald_stopped_or_blocked=True)

    current_nmcli = (
        "nmcli",
        "-g",
        "GENERAL.NM-MANAGED",
        "device",
        "show",
        um.WIFI_IFACE,
    )
    assert outcome.observed.wlan1_managed is True
    assert "wlan1_managed" not in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.CLIENT
    assert [request for request in requests if request[0] == "nmcli"] == [current_nmcli]
    assert counters["subprocess"] > 0


def test_live_factory_end_to_end_client_is_fully_mocked(monkeypatch):
    io, counters, stdin = _mock_live_factory(monkeypatch, client_commands(), full_links(), env={})
    outcome = um.observe_mode_state(io, gerald_stopped_or_blocked=True)
    assert um.classify_mode(outcome.observed) == um.CLIENT
    assert counters["subprocess"] > 0 and counters["link"] > 0


def test_live_factory_end_to_end_stable_bluetooth_is_fully_mocked(monkeypatch):
    links = full_links()
    links[um.WIFI_DRIVER_LINK] = FileNotFoundError(um.WIFI_DRIVER_LINK)
    io, counters, stdin = _mock_live_factory(monkeypatch, stable_bt_commands(), links, env={})
    outcome = um.observe_mode_state(io, gerald_stopped_or_blocked=False)
    assert um.classify_mode(outcome.observed) == um.STABLE_BLUETOOTH
    assert outcome.observed.wifi_driver_bound is False
    assert "wifi_driver_bound" not in outcome.observed.unavailable_facts
    assert counters["subprocess"] > 0 and counters["link"] > 0
    assert counters["link"] == 4


def test_live_factory_nonabsence_wifi_link_failure_is_unavailable(monkeypatch):
    links = full_links()
    links[um.WIFI_DRIVER_LINK] = PermissionError("injected")
    io, counters, stdin = _mock_live_factory(monkeypatch, stable_bt_commands(), links, env={})
    outcome = um.observe_mode_state(io, gerald_stopped_or_blocked=False)
    assert outcome.observed.wifi_driver_bound is False
    assert "wifi_driver_bound" in outcome.observed.unavailable_facts
    assert um.classify_mode(outcome.observed) == um.UNKNOWN
    diagnostic = next(item for item in outcome.diagnostics if item.code == "driver_link_unavailable")
    assert diagnostic.source == um.WIFI_DRIVER_LINK
    assert counters["link"] == 4


def test_live_factory_end_to_end_local_console_management_is_fully_mocked(monkeypatch):
    io, counters, stdin = _mock_live_factory(monkeypatch, local_console_commands(), {}, env={})
    outcome = um.observe_management_path(io)
    assert um.evaluate_management_path(outcome.observed).allowed is True
    assert counters["env"] == 2
    assert stdin.calls == ["isatty", "fileno"]


@pytest.mark.parametrize(
    "route_stdout, expected_interface, allowed, expected_error",
    [
        (
            "default via 192.168.178.1 dev eth1 metric 100\n"
            "default via 192.168.178.1 dev wlan1 metric 600\n",
            "eth1",
            True,
            False,
        ),
        (
            "default via 192.168.178.1 dev eth1 metric 600\n"
            "default via 192.168.178.1 dev wlan1 metric 100\n",
            "wlan1",
            False,
            True,
        ),
        (
            "default via 192.168.178.1 dev eth1 metric 100\n"
            "default via 192.168.178.1 dev wlan1 metric 100\n",
            None,
            False,
            True,
        ),
    ],
)
def test_live_factory_management_metric_priority_integration(
    monkeypatch, route_stdout, expected_interface, allowed, expected_error
):
    commands = local_console_commands()
    commands[("ip", "route", "show", "default")] = (0, route_stdout, "")
    actual_calls = []
    io, counters, _ = _mock_live_factory(
        monkeypatch,
        commands,
        {},
        env={},
        actual_calls=actual_calls,
    )

    outcome = um.observe_management_path(io)
    gate = um.evaluate_management_path(outcome.observed)

    assert outcome.observed.default_route_interface == expected_interface
    assert "default_route_unavailable" not in diag_codes(outcome)
    assert ("default_route_ambiguous" in diag_codes(outcome)) is (expected_interface is None)
    assert gate.allowed is allowed
    assert ("default_route_wrong" in gate_codes(gate)) is expected_error
    assert sum(1 for argv, _kwargs in actual_calls if argv[0].endswith("/ip")) == 3
    assert all(not argv[0].endswith("/sudo") for argv, _kwargs in actual_calls if argv[0].endswith("/ip"))
    assert counters["subprocess"] == len(actual_calls)


def test_live_factory_end_to_end_ssh_management_has_precedence_over_stdin(monkeypatch):
    io, counters, stdin = _mock_live_factory(monkeypatch, ssh_commands(), {}, env={"SSH_CONNECTION": "10.20.30.40 54812 192.168.1.25 22"})
    outcome = um.observe_management_path(io)
    assert um.evaluate_management_path(outcome.observed).allowed is True
    assert stdin.calls == []
    assert counters["tty"] == 0


def test_live_factory_timeout_failure_is_fail_closed(monkeypatch):
    counters = {name: 0 for name in ("subprocess", "file", "link", "exists", "env", "stdin", "tty")}
    io, counters, _ = _mock_live_factory(monkeypatch, local_console_commands(), {}, env={}, counters=counters)
    def timeout(*args, **kwargs):
        counters["subprocess"] += 1
        raise um.subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(um.subprocess, "run", timeout)
    outcome = um.observe_management_path(io)
    assert outcome.observed.interface_exists is False
    assert um.evaluate_management_path(outcome.observed).allowed is False
    assert any(d.code == "interface_observation_unavailable" for d in outcome.diagnostics)


def test_factory_construction_live_access_counters_are_zero(monkeypatch):
    counters = {
        "real_subprocess_invocations": 0,
        "real_file_reads": 0,
        "real_link_reads": 0,
        "real_exists_checks": 0,
        "real_environment_reads": 0,
        "real_stdin_inspections": 0,
        "real_ttyname_calls": 0,
    }
    monkeypatch.setattr(um.subprocess, "run", lambda *a, **k: counters.__setitem__("real_subprocess_invocations", counters["real_subprocess_invocations"] + 1))
    monkeypatch.setattr(um.Path, "read_text", lambda *a, **k: counters.__setitem__("real_file_reads", counters["real_file_reads"] + 1))
    monkeypatch.setattr(um.os, "readlink", lambda *a, **k: counters.__setitem__("real_link_reads", counters["real_link_reads"] + 1))
    monkeypatch.setattr(um.os.path, "exists", lambda *a, **k: counters.__setitem__("real_exists_checks", counters["real_exists_checks"] + 1))
    monkeypatch.setattr(um.os.environ, "get", lambda *a, **k: counters.__setitem__("real_environment_reads", counters["real_environment_reads"] + 1))
    monkeypatch.setattr(um.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(um.os, "ttyname", lambda *a, **k: counters.__setitem__("real_ttyname_calls", counters["real_ttyname_calls"] + 1))
    um.build_live_read_only_host_io()
    assert counters == {name: 0 for name in counters}


def test_mocked_provider_execution_never_reaches_original_primitives(monkeypatch):
    original_run = um.subprocess.run
    original_read_text = um.Path.read_text
    original_readlink = um.os.readlink
    original_exists = um.os.path.exists
    original_env_get = um.os.environ.get
    original_stdin = um.sys.stdin
    original_ttyname = um.os.ttyname
    original_calls = []
    fake_calls = []
    assert all(callable(value) for value in (original_run, original_read_text, original_readlink, original_exists, original_env_get, original_ttyname))

    def fake_run(*args, **kwargs):
        fake_calls.append("subprocess.run")
        return _live_completed()

    def fake_read_text(path, **kwargs):
        fake_calls.append("Path.read_text")
        return "up\n"

    def fake_readlink(path):
        fake_calls.append("os.readlink")
        return "driver"

    def fake_exists(path):
        fake_calls.append("os.path.exists")
        return True

    def fake_env_get(key):
        fake_calls.append("os.environ.get")
        return None

    fake_stdin = _FakeStdin()

    def fake_ttyname(fd):
        fake_calls.append("os.ttyname")
        return "/dev/tty1"

    monkeypatch.setattr(um.shutil, "which", lambda executable, path: "/mock/bin/" + executable)
    monkeypatch.setattr(um.subprocess, "run", fake_run)
    monkeypatch.setattr(um.Path, "read_text", fake_read_text)
    monkeypatch.setattr(um.os, "readlink", fake_readlink)
    monkeypatch.setattr(um.os.path, "exists", fake_exists)
    monkeypatch.setattr(um.os.environ, "get", fake_env_get)
    monkeypatch.setattr(um.sys, "stdin", fake_stdin)
    monkeypatch.setattr(um.os, "ttyname", fake_ttyname)

    io = um.build_live_read_only_host_io()
    assert io.run_command(("ip", "route", "show", "default"), 5).returncode == 0
    assert io.read_text(f"/sys/class/net/{um.SAFE_MANAGEMENT_IFACE}/operstate") == "up\n"
    assert io.read_link(um.WIFI_DRIVER_LINK) == "driver"
    assert io.path_exists(um.WIFI_DRIVER_LINK) is True
    assert io.get_environment("SSH_CONNECTION") is None
    assert io.local_console_present() is True
    assert set(fake_calls) == {"subprocess.run", "Path.read_text", "os.readlink", "os.path.exists", "os.environ.get", "os.ttyname"}
    assert original_calls == []
    assert original_stdin is not fake_stdin


@pytest.mark.parametrize("link_path", [um.WIFI_DRIVER_LINK, um.BT_DRIVER_LINKS[0]])
def test_live_read_link_preserves_authorized_missing_link(monkeypatch, link_path):
    calls = []

    def missing(path):
        calls.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(um.os, "readlink", missing)
    with pytest.raises(FileNotFoundError):
        um._live_read_link(link_path)
    assert calls == [link_path]


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError(errno.EIO, "injected I/O failure"), RuntimeError("injected provider failure")])
def test_live_read_link_normalizes_nonabsence_failures(monkeypatch, failure):
    calls = []

    def failed(path):
        calls.append(path)
        raise failure

    monkeypatch.setattr(um.os, "readlink", failed)
    with pytest.raises(um.ReadOnlyHostIOExecutionError):
        um._live_read_link(um.WIFI_DRIVER_LINK)
    assert calls == [um.WIFI_DRIVER_LINK]
