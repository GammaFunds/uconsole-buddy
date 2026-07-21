import asyncio
import json

from bridge.ble_central import BleCentral
from bridge.daemon import Bridge, _make_handler
from bridge.protocol import parse_permission  # noqa

def run(coro): return asyncio.run(coro)

def make_bridge():
    sent = []
    async def send_snapshot(line): sent.append(line)
    return Bridge(send_snapshot), sent

def test_approval_allow():
    async def scenario():
        b, sent = make_bridge()
        task = asyncio.create_task(b.request_approval("r1", "Bash", "ls", timeout=5))
        await asyncio.sleep(0)  # request_approval sendet Prompt + registriert Future
        b.on_ble_line('{"cmd":"permission","id":"r1","decision":"once"}')
        res = await task
        assert res == "allow"
        assert any('"prompt"' in s and '"r1"' in s for s in sent)   # Prompt gesendet
        assert any('"waiting": 0' in s for s in sent)               # cleared danach
        return res
    assert run(scenario()) == "allow"

def test_approval_deny():
    async def scenario():
        b, _ = make_bridge()
        task = asyncio.create_task(b.request_approval("r2", "Bash", "x", timeout=5))
        await asyncio.sleep(0)
        b.on_ble_line('{"cmd":"permission","id":"r2","decision":"deny"}')
        return await task
    assert run(scenario()) == "deny"

def test_approval_timeout_asks():
    async def scenario():
        b, _ = make_bridge()
        return await b.request_approval("r3", "Bash", "x", timeout=0.05)  # keine Antwort
    assert run(scenario()) == "ask"

def test_stale_permission_ignored():
    async def scenario():
        b, _ = make_bridge()
        task = asyncio.create_task(b.request_approval("r4", "Bash", "x", timeout=0.2))
        await asyncio.sleep(0)
        b.on_ble_line('{"cmd":"permission","id":"OTHER","decision":"once"}')  # falsche id
        return await task
    assert run(scenario()) == "ask"   # nur die falsche id kam → Timeout → ask


def test_duplicate_response_after_resolution_is_ignored():
    async def scenario():
        b, _ = make_bridge()
        task = asyncio.create_task(b.request_approval("r-dup", "Bash", "x", timeout=5))
        await asyncio.sleep(0)
        b.on_ble_line('{"cmd":"permission","id":"r-dup","decision":"once"}')
        assert await task == "allow"
        b.on_ble_line('{"cmd":"permission","id":"r-dup","decision":"deny"}')
        assert not b._pending
    run(scenario())


def test_concurrent_approval_falls_back_without_second_prompt():
    async def scenario():
        b, sent = make_bridge()
        first = asyncio.create_task(b.request_approval("r-first", "Bash", "x", timeout=5))
        await asyncio.sleep(0)
        before = len(sent)
        assert await b.request_approval("r-second", "Bash", "y", timeout=5) == "ask"
        assert len(sent) == before
        b.on_ble_line('{"cmd":"permission","id":"r-first","decision":"deny"}')
        assert await first == "deny"
    run(scenario())


def test_ble_disconnect_fails_pending_without_allowing():
    async def scenario():
        b, _ = make_bridge()
        task = asyncio.create_task(b.request_approval("r-disconnect", "Bash", "x", timeout=5))
        await asyncio.sleep(0)
        b.fail_pending()
        assert await task == "ask"
        b.on_ble_line('{"cmd":"permission","id":"r-disconnect","decision":"once"}')
        assert not b._pending
    run(scenario())


def test_unknown_device_decision_never_allows():
    async def scenario():
        b, _ = make_bridge()
        task = asyncio.create_task(b.request_approval("r-unknown", "Bash", "x", timeout=5))
        await asyncio.sleep(0)
        b.on_ble_line('{"cmd":"permission","id":"r-unknown","decision":"always"}')
        assert await task == "ask"
    run(scenario())


def test_approval_allow_restores_retained_running_snapshot():
    async def scenario():
        b, sent = make_bridge()
        hud = {"model": "gpt-5.4", "project": "gerald"}
        await b.push_event(state="running", msg="working", entry="12:00 Bash", hud=hud)

        task = asyncio.create_task(b.request_approval("r-restore", "Bash", "pytest -q", timeout=5))
        await asyncio.sleep(0)
        b.on_ble_line('{"cmd":"permission","id":"r-restore","decision":"once"}')
        assert await task == "allow"

        snapshots = [json.loads(line) for line in sent]
        assert [snapshot["state"] for snapshot in snapshots[-2:]] == ["waiting", "running"]
        assert snapshots[-1]["entries"] == ["12:00 Bash"]
        assert snapshots[-1]["hud"] == hud
        assert snapshots[-1]["prompt"] is None
        assert snapshots[-1]["waiting"] == 0

    run(scenario())


def test_approval_deny_and_fallback_restore_retained_running_snapshot():
    async def scenario(decision):
        b, sent = make_bridge()
        hud = {"model": "gpt-5.4", "project": "gerald"}
        await b.push_event(state="running", msg="working", entry="12:00 Bash", hud=hud)

        task = asyncio.create_task(b.request_approval("r-restore", "Bash", "pytest -q", timeout=0.02))
        await asyncio.sleep(0)
        if decision is not None:
            b.on_ble_line(
                json.dumps({"cmd": "permission", "id": "r-restore", "decision": decision})
            )
        result = await task

        snapshot = json.loads(sent[-1])
        assert result == ("deny" if decision == "deny" else "ask")
        assert snapshot["state"] == "running"
        assert snapshot["entries"] == ["12:00 Bash"]
        assert snapshot["hud"] == hud
        assert snapshot["prompt"] is None
        assert snapshot["waiting"] == 0

    run(scenario("deny"))
    run(scenario(None))


def test_approval_while_ble_already_disconnected_immediately_asks():
    async def scenario():
        central = BleCentral.__new__(BleCentral)
        central._client = None
        central._connected = False
        b = Bridge(central.send_line)

        result = await asyncio.wait_for(
            b.request_approval("r-offline", "Bash", "pytest -q", timeout=5),
            timeout=0.2,
        )
        assert result == "ask"
        assert not b._pending

        central._connected = True
        b.on_ble_line('{"cmd":"permission","id":"r-offline","decision":"once"}')
        assert not b._pending

    run(scenario())


def test_approval_client_disconnect_cancels_visible_prompt(monkeypatch):
    class Reader:
        async def readline(self):
            return b'{"type":"approve","id":"r-cancel","tool":"Bash","hint":"x"}\n'

        async def read(self, _size):
            await asyncio.sleep(0)
            return b""

    class Writer:
        def __init__(self):
            self.data = b""
            self.closed = False

        def write(self, data):
            self.data += data

        async def drain(self):
            pass

        def close(self):
            self.closed = True

    async def scenario():
        b, sent = make_bridge()
        writer = Writer()
        monkeypatch.setattr("bridge.daemon.APPROVE_TIMEOUT", 5)
        await _make_handler(b)(Reader(), writer)
        assert writer.closed
        assert writer.data == b""
        assert not b._pending
        assert any('"waiting": 0' in snapshot for snapshot in sent)
    run(scenario())


def test_push_status_sends_when_idle():
    async def scenario():
        b, sent = make_bridge()
        await b.push_status("running", "x")
        assert any('"running": 1' in s for s in sent)
        return sent
    run(scenario())


def test_push_status_skips_during_approval():
    async def scenario():
        b, sent = make_bridge()
        task = asyncio.create_task(b.request_approval("r5", "Bash", "x", timeout=5))
        await asyncio.sleep(0)  # Prompt-Snapshot gesendet, Future pending
        before = len(sent)
        await b.push_status("running", "x")
        assert len(sent) == before  # kein zusätzlicher Status-Snapshot während Approval aktiv
        b.on_ble_line('{"cmd":"permission","id":"r5","decision":"once"}')  # aufräumen
        await task
    run(scenario())


def test_push_event_sets_state_and_entry():
    async def scenario():
        b, sent = make_bridge()
        await b.push_event(state="running", msg="arbeite", entry="14:23 Bash: ls")
        m = json.loads(sent[-1])
        assert m["state"] == "running"
        assert m["entries"] == ["14:23 Bash: ls"]
        assert m["msg"] == "arbeite"
    run(scenario())

def test_push_event_entries_ring_keeps_last_8():
    async def scenario():
        b, sent = make_bridge()
        for i in range(10):
            await b.push_event(state="running", entry=f"e{i}")
        m = json.loads(sent[-1])
        assert m["entries"] == [f"e{i}" for i in range(2, 10)]  # nur letzte 8
    run(scenario())

def test_push_event_skips_send_during_approval_but_keeps_state():
    async def scenario():
        b, sent = make_bridge()
        task = asyncio.create_task(b.request_approval("rA", "Bash", "x", timeout=5))
        await asyncio.sleep(0)
        before = len(sent)
        await b.push_event(state="running", entry="hidden")
        assert len(sent) == before                      # kein Push während Approval
        b.on_ble_line('{"cmd":"permission","id":"rA","decision":"once"}')
        await task
        await b.push_event(state="done")                # jetzt frei
        m = json.loads(sent[-1])
        assert m["state"] == "done"
        assert "hidden" in m["entries"]                 # während Approval gemerkte Zeile ist da
    run(scenario())


def test_done_decays_to_idle():
    async def scenario():
        b, sent = make_bridge()
        await b.push_event(state="done", decay=0.05)
        assert json.loads(sent[-1])["state"] == "done"
        await asyncio.sleep(0.12)
        assert json.loads(sent[-1])["state"] == "idle"   # automatisch zerfallen
    run(scenario())

def test_new_event_cancels_decay():
    async def scenario():
        b, sent = make_bridge()
        await b.push_event(state="done", decay=0.10)
        await b.push_event(state="running")              # canceled Zerfall
        await asyncio.sleep(0.15)
        assert json.loads(sent[-1])["state"] == "running"
    run(scenario())


def test_hud_persists_across_events():
    async def scenario():
        b, sent = make_bridge()
        await b.push_event(hud={"model": "Fable 5", "ctx_pct": 12})
        await b.push_event(state="running", entry="09:00 Bash: ls")

        assert json.loads(sent[0])["hud"] == {"model": "Fable 5", "ctx_pct": 12}
        assert json.loads(sent[1])["hud"] == {"model": "Fable 5", "ctx_pct": 12}  # bleibt erhalten
        assert json.loads(sent[1])["state"] == "running"

    run(scenario())
