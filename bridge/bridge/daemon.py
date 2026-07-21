# bridge/daemon.py  (Bridge-Kern; Socket-Server folgt in Task 1.2)
import asyncio
from collections import deque
from typing import Awaitable, Callable
from .protocol import (
    build_prompt_snapshot,
    build_snapshot,
    decision_to_hook,
    parse_permission,
)


class Bridge:
    def __init__(self, send_snapshot: Callable[[str], Awaitable[bool | None]]):
        self._send = send_snapshot
        self._pending: dict[str, asyncio.Future] = {}
        self._state = "idle"
        self._msg = "idle"
        self._entries: deque[str] = deque(maxlen=8)
        self._hud: dict | None = None
        self._idle_task = None

    async def request_approval(self, req_id: str, tool: str, hint: str, timeout: float) -> str:
        # Gerald has one approval overlay. Concurrent requests fall through to the
        # agent's native prompt instead of hiding or queueing an unresolved request.
        if self._pending:
            return "ask"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            delivered = await self._send(build_prompt_snapshot(req_id, tool, hint))
            if delivered is False:
                return "ask"
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return "ask"
        finally:
            self._pending.pop(req_id, None)
            try:
                await self._send(self._build_state_snapshot())
            except Exception:
                pass

    def on_ble_line(self, line: str) -> None:
        p = parse_permission(line)
        if not p:
            return
        fut = self._pending.get(p["id"])
        if fut and not fut.done():
            fut.set_result(decision_to_hook(p["decision"]))

    def _build_state_snapshot(self) -> str:
        return build_snapshot(
            state=self._state,
            total=1,
            running=1 if self._state in ("running", "thinking") else 0,
            waiting=1 if self._state == "waiting" else 0,
            msg=self._msg,
            entries=list(self._entries),
            hud=self._hud,
        )

    async def push_event(self, state: str | None = None, msg: str | None = None,
                         entry: str | None = None, decay: float = 5.0,
                         hud: dict | None = None) -> None:
        if hud:
            self._hud = hud
        if entry:
            self._entries.append(entry)
        if state is not None:
            self._state = state
            self._cancel_decay()
            if state == "done":
                self._idle_task = asyncio.ensure_future(self._decay_to_idle(decay))
        if msg is not None:
            self._msg = msg
        if self._pending:          # aktiver Approval-Overlay hat Vorrang
            return
        await self._send(self._build_state_snapshot())

    def _cancel_decay(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _decay_to_idle(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._state = "idle"
        self._msg = "idle"
        if not self._pending:
            await self._send(self._build_state_snapshot())

    async def push_status(self, state: str, msg: str = "") -> None:
        """Rückwärtskompatibler Wrapper (alte Hooks + Tests)."""
        await self.push_event(state=state, msg=msg)

    def fail_pending(self) -> None:
        """Bei BLE-Disconnect: alle offenen Approvals fail-safe auf 'ask' auflösen (P3)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result("ask")
        self._pending.clear()


# ---- Daemon-Außenschale: Unix-Socket-Server + BLE-Verdrahtung (Task 1.2) ----
import json, logging, os
from pathlib import Path
from .ble_central import BleCentral
from .hooks._paths import socket_path

APPROVE_TIMEOUT = 100.0
SOCK = Path(socket_path())
logging.basicConfig(filename="bridge.log", level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bridge")


def _make_handler(bridge: "Bridge"):
    async def handle(reader, writer):
        try:
            raw = await reader.readline()
            req = json.loads(raw.decode("utf-8"))
            if req.get("type") == "approve":
                approval = asyncio.create_task(bridge.request_approval(
                    req["id"], req.get("tool", "?"), req.get("hint", ""), APPROVE_TIMEOUT))
                disconnected = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait(
                    (approval, disconnected), return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    approval.cancel()
                    await asyncio.gather(approval, return_exceptions=True)
                    return
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)
                decision = approval.result()
            elif req.get("type") == "status":
                await bridge.push_event(state=req.get("state"),
                                        msg=req.get("msg") if "msg" in req else None,
                                        entry=req.get("entry"),
                                        hud=req.get("hud"))
                decision = "ask"   # kein Approval — Antwort wird vom fire-and-forget-Hook ignoriert
            else:
                decision = "ask"   # unbekannter Typ — kein Approval
            writer.write((json.dumps({"decision": decision}) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:
            log.info("handler error: %s", e)
            try:
                writer.write(b'{"decision":"ask"}\n')
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
    return handle


async def _serve(bridge: "Bridge"):
    SOCK.parent.mkdir(parents=True, exist_ok=True)
    if SOCK.exists():
        SOCK.unlink()
    server = await asyncio.start_unix_server(_make_handler(bridge), path=str(SOCK))
    os.chmod(SOCK, 0o600)
    log.info("socket listening at %s", SOCK)
    print(f"socket listening at {SOCK}")
    async with server:
        await server.serve_forever()


async def _main():
    # bridge und ble referenzieren sich gegenseitig (ble braucht bridge.fail_pending als
    # on_disconnect-Callback, bridge braucht ble.send_line als send_snapshot). Auflösung über
    # dasselbe bridge_ref-Indirektions-Pattern, das on_line schon fuer on_ble_line nutzt.
    bridge_ref: dict = {}

    def on_line(line: str):
        log.info("BLE< %s", line)
        if "bridge" in bridge_ref:
            bridge_ref["bridge"].on_ble_line(line)

    def on_disconnect():
        log.info("BLE disconnected — failing pending approvals")
        if "bridge" in bridge_ref:
            bridge_ref["bridge"].fail_pending()

    ble = BleCentral(on_line, on_disconnect=on_disconnect)
    print("verbinde mit uConsole (NUS) ...")
    while True:
        try:
            await ble.connect()
            break
        except Exception as e:
            log.info("initial connect failed: %s — retry in 5s", e)
            print(f"connect fehlgeschlagen ({e}); retry in 5s ...")
            await asyncio.sleep(5)
    log.info("BLE connected to uConsole")
    print("BLE verbunden.")
    bridge = Bridge(lambda s: ble.send_line(s))
    bridge_ref["bridge"] = bridge
    await _serve(bridge)


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
