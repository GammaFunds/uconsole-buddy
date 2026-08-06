# companion/ble_nus.py
"""Nordic-UART BLE-Peripheral auf hci0 (bluez-peripheral 0.1.7 / BlueZ D-Bus).

API-Hinweis: die installierte `bluez-peripheral`-Version (0.1.7) baut auf
`dbus_next` (nicht `dbus_fast`!) und exportiert `get_message_bus()` direkt aus
`bluez_peripheral.util`. Adapter werden hier aus BlueZs verwalteten Objekten
gefiltert und anschließend hart per Adresse ausgewählt.
"""
import asyncio
import logging
import os
from typing import Callable, Optional

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as Flags
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.util import Adapter, get_message_bus

from .framing import LineReassembler, chunk_for_mtu

log = logging.getLogger("companion.ble")

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Mac -> Gerät (write)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Gerät -> Mac (notify)
ADAPTER_ADDR = "2C:CF:67:FE:1E:1D"  # hci0 onboard Cypress -- HART gepinnt
ADAPTER_ADDR_ENV = "GERALD_BT_ADAPTER_ADDR"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
ADVERT_PATH = "/com/spacecheese/bluez_peripheral/advert0"  # bluez_peripheral-Default, explizit gehalten für Re-Advertise


class _NusService(Service):
    def __init__(self, on_line: Callable[[str], None]):
        self._on_line = on_line
        self._reasm = LineReassembler()
        self._tx_value = bytearray()
        self._mtu = 185  # macOS-typisch; keine dynamische MTU-Abfrage in Phase 0
        super().__init__(NUS_SERVICE, True)

    @characteristic(NUS_TX, Flags.NOTIFY)
    def tx(self, options):
        return bytes(self._tx_value)

    @characteristic(NUS_RX, Flags.WRITE | Flags.WRITE_WITHOUT_RESPONSE)
    def rx(self, options):
        return b""

    @rx.setter
    def rx(self, value, options):
        for line in self._reasm.feed(bytes(value)):
            self._on_line(line)

    def notify_line(self, line: str) -> None:
        data = line.encode("utf-8")
        for chunk in chunk_for_mtu(data, self._mtu):
            self._tx_value = bytearray(chunk)
            self.tx.changed(bytes(chunk))  # sendet Notify an subscribte Clients


class NusPeripheral:
    def __init__(self, device_name: str, on_line: Callable[[str], None],
                 adapter_addr: str = ADAPTER_ADDR):
        self.device_name = device_name
        self.adapter_addr = os.environ.get(ADAPTER_ADDR_ENV) or adapter_addr
        self._svc = _NusService(on_line)
        self._bus = None  # dbus_next.aio.MessageBus, via get_message_bus()
        self._adapter: Optional[Adapter] = None
        self._advert: Optional[Advertisement] = None
        self._connected = False

    async def start(self) -> None:
        self._bus = await get_message_bus()
        self._adapter = await self._select_adapter(self._bus, self.adapter_addr)
        bound_addr = await self._adapter.get_address()
        log.info("bound to adapter %s (address %s)", self._adapter._proxy.path, bound_addr)

        await self._svc.register(self._bus, adapter=self._adapter)
        self._advert = Advertisement(self.device_name, [NUS_SERVICE], 0, 0)
        await self._advert.register(self._bus, adapter=self._adapter, path=ADVERT_PATH)

        await self._watch_connections()

    @staticmethod
    async def _enumerate_adapters(bus):
        """Return Adapter objects only for managed objects exposing Adapter1."""
        introspection = await bus.introspect("org.bluez", "/")
        root = bus.get_proxy_object("org.bluez", "/", introspection)
        om = root.get_interface(OBJECT_MANAGER_INTERFACE)
        objects = await om.call_get_managed_objects()

        adapters = []
        for path, interfaces in objects.items():
            if ADAPTER_INTERFACE not in interfaces:
                continue
            adapter_introspection = await bus.introspect("org.bluez", path)
            proxy = bus.get_proxy_object("org.bluez", path, adapter_introspection)
            adapters.append(Adapter(proxy))
        return adapters

    @staticmethod
    async def _select_adapter(bus, addr: str) -> Adapter:
        """Wählt den Adapter mit der gegebenen BT-Adresse explizit aus.

        Die Kandidaten kommen ausschließlich aus verwalteten BlueZ-Objekten
        mit `org.bluez.Adapter1`; die Auswahl bleibt adressbasiert.
        """
        adapters = await NusPeripheral._enumerate_adapters(bus)
        for adapter in adapters:
            candidate_addr = await adapter.get_address()
            if candidate_addr.casefold() == addr.casefold():
                return adapter
        found_addresses = [await adapter.get_address() for adapter in adapters]
        found = ", ".join(found_addresses) or "keine"
        raise RuntimeError(
            f"Bluetooth-Adapter mit Adresse {addr} nicht gefunden (gefunden: {found}). "
            "Ist hci0 up? `hciconfig -a` prüfen."
        )

    # ---- Connection tracking (best effort; für Auto-Re-Advertise nach Disconnect) ----

    async def _watch_connections(self) -> None:
        introspection = await self._bus.introspect("org.bluez", "/")
        root = self._bus.get_proxy_object("org.bluez", "/", introspection)
        om = root.get_interface("org.freedesktop.DBus.ObjectManager")
        adapter_path = self._adapter._proxy.path
        dev_prefix = adapter_path + "/dev_"

        def handle_change(path: str, connected: bool) -> None:
            if self._connected == connected:
                return
            self._connected = connected
            log.info("central %s %s", path, "connected" if connected else "disconnected")
            if not connected:
                asyncio.create_task(self._reassert_advertising())

        async def subscribe_device(path: str) -> None:
            try:
                dev_introspection = await self._bus.introspect("org.bluez", path)
            except Exception as e:  # device already gone
                log.debug("could not introspect %s: %s", path, e)
                return
            dev = self._bus.get_proxy_object("org.bluez", path, dev_introspection)
            props = dev.get_interface("org.freedesktop.DBus.Properties")

            def on_props_changed(interface, changed, invalidated):
                if interface == "org.bluez.Device1" and "Connected" in changed:
                    handle_change(path, bool(changed["Connected"].value))

            props.on_properties_changed(on_props_changed)

        def on_interfaces_added(path, interfaces):
            if not path.startswith(dev_prefix):
                return
            dev = interfaces.get("org.bluez.Device1")
            if dev is None:
                return
            if "Connected" in dev:
                handle_change(path, bool(dev["Connected"].value))
            asyncio.create_task(subscribe_device(path))

        om.on_interfaces_added(on_interfaces_added)

        # Bereits existierende Device1-Objekte unter unserem Adapter erfassen
        # (z.B. bereits gebondete Geräte, die vor unserem Start verbunden waren).
        objects = await om.call_get_managed_objects()
        for path, interfaces in objects.items():
            if not path.startswith(dev_prefix):
                continue
            dev = interfaces.get("org.bluez.Device1")
            if dev is None:
                continue
            if "Connected" in dev:
                handle_change(path, bool(dev["Connected"].value))
            await subscribe_device(path)

    async def _reassert_advertising(self) -> None:
        """Registriert das Advertisement nach einem Disconnect neu.

        BlueZ pausiert Advertising auf manchen (insb. Single-Instance-)
        Controllern während einer aktiven Verbindung. `Advertisement` bietet
        kein `unregister()`; wir versuchen best-effort erst abzumelden (falls
        BlueZ es noch als registriert führt) und registrieren dann neu.
        """
        try:
            mgr = self._adapter._proxy.get_interface(Advertisement._MANAGER_INTERFACE)
            try:
                await mgr.call_unregister_advertisement(ADVERT_PATH)
            except Exception:
                pass  # war nicht (mehr) registriert -- ok
            self._advert = Advertisement(self.device_name, [NUS_SERVICE], 0, 0)
            await self._advert.register(self._bus, adapter=self._adapter, path=ADVERT_PATH)
            log.info("re-advertising after disconnect")
        except Exception as e:
            log.warning("failed to re-assert advertising: %s", e)

    async def send_line(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        self._svc.notify_line(line)

    def is_connected(self) -> bool:
        return self._connected
