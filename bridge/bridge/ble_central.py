"""BLE-Central (bleak): verbindet sich mit dem uConsole-Peripheral, NUS-Serial."""
import asyncio
import logging
from typing import Callable
from bleak import BleakScanner, BleakClient
from .framing import LineReassembler, chunk_for_mtu

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"  # zum Auffinden (Name unzuverlässig)
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # WRITE (Central → Gerät)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # NOTIFY (Gerät → Central)

RECONNECT_BACKOFF = (2.0, 4.0, 8.0, 15.0)  # Sekunden; letzter Wert wird wiederholt (Cap)

log = logging.getLogger("bridge.ble")


class BleCentral:
    def __init__(
        self,
        on_line: Callable[[str], None],
        device_name: str = "Claude-uConsole",
        on_disconnect: Callable[[], None] | None = None,
    ):
        self._on_line = on_line
        self._on_disconnect = on_disconnect
        self._name = device_name
        self._reasm = LineReassembler()
        self._client: BleakClient | None = None
        self._connected = False
        self._reconnect_task: asyncio.Task | None = None

    async def connect(self) -> None:
        # Alten Client sauber loslassen, sonst hält macOS/CoreBluetooth ein stale Handle,
        # das den Reconnect in einen TimeoutError laufen lässt.
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        dev = await BleakScanner.find_device_by_filter(
            lambda d, ad: NUS_SERVICE.lower() in [u.lower() for u in (ad.service_uuids or [])],
            timeout=15.0)
        if dev is None:
            raise RuntimeError(f"kein Gerät mit NUS-Service {NUS_SERVICE} gefunden")
        self._client = BleakClient(dev, disconnected_callback=self._on_disc)
        await self._client.connect()
        await self._client.start_notify(NUS_TX, self._rx)
        self._connected = True

    def _rx(self, _char, data: bytearray) -> None:
        for line in self._reasm.feed(bytes(data)):
            self._on_line(line)

    def _trigger_reconnect(self) -> None:
        """Als getrennt markieren + Reconnect-Loop starten (idempotent). Von _on_disc UND
        vom Send-Fehler-Pfad genutzt — falls bleaks disconnected_callback mal nicht feuert."""
        self._connected = False
        if self._on_disconnect is not None:
            self._on_disconnect()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    def _on_disc(self, _client: BleakClient) -> None:
        """bleak-Callback bei ungeplantem Verbindungsverlust. Synchron — keine awaits hier."""
        log.info("BLE disconnected")
        self._trigger_reconnect()

    async def _reconnect_loop(self) -> None:
        attempt = 0
        while not self._connected:
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            log.info("reconnect attempt %d in %.0fs", attempt + 1, delay)
            await asyncio.sleep(delay)
            try:
                await self.connect()
                log.info("BLE reconnected")
            except Exception as e:
                log.info("reconnect failed: %s", e)
                attempt += 1

    async def send_line(self, line: str) -> bool:
        if self._client is None or not self._connected:
            return False  # nicht verbunden — Prompt kann nicht zugestellt werden
        data = line.encode("utf-8")
        mtu = getattr(self._client, "mtu_size", 23) or 23
        try:
            for chunk in chunk_for_mtu(data, mtu):
                await self._client.write_gatt_char(NUS_RX, chunk, response=False)
                await asyncio.sleep(0.01)
            return True
        except Exception as e:
            # Toter Link: Send scheitert oft, BEVOR bleaks disconnected_callback feuert.
            # Selbst als Disconnect behandeln → Reconnect-Loop anwerfen.
            log.info("send failed (%s) — treating as disconnect", e)
            self._trigger_reconnect()
            return False

    async def disconnect(self) -> None:
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
        if self._client:
            await self._client.disconnect()
