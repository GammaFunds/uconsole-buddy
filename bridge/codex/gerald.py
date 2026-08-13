#!/usr/bin/env python3
"""Translate supported Codex lifecycle hooks to Gerald's existing socket protocol."""
import json
from pathlib import Path
import re
import secrets
import shlex
import socket
import sys
from datetime import datetime

HOOKS_DIR = Path(__file__).resolve().parents[1] / "bridge" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
from _paths import socket_path  # noqa: E402

ACTIVITY_MAX = 120
APPROVAL_HINT_MAX = 46
STATUS_TIMEOUT = 0.5
APPROVAL_TIMEOUT = 105.0
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Delete|Update) File: (.+)$")
_SAFE_ARGS = {
    "pytest": {"-q", "-x", "-s", "--quiet", "--collect-only"},
    "git": {"status", "diff", "show", "log", "branch", "-s", "--short", "--stat", "--check", "--porcelain"},
    "npm": {"test"},
    "cargo": {"test", "check"},
    "go": {"test"},
}


def _clean(value: object, limit: int = ACTIVITY_MAX) -> str:
    text = _CONTROL.sub(" ", str(value))
    return " ".join(text.split())[:limit]


def _basename(value: object) -> str:
    return _clean(str(value).replace("\\", "/").rsplit("/", 1)[-1], 80)


def _bash_summary(command: object) -> str:
    try:
        parts = shlex.split(str(command), posix=True)
    except (TypeError, ValueError):
        return "Bash"
    if not parts or "=" in parts[0] or parts[0] in {"env", "sudo", "sh", "bash", "zsh"}:
        return "Bash"
    executable = _basename(parts[0])
    allowed = _SAFE_ARGS.get(executable, set())
    safe_args = [part for part in parts[1:] if part in allowed]
    if executable == "python" and parts[1:3] == ["-m", "pytest"]:
        safe_args = ["-m", "pytest"] + [part for part in parts[3:] if part in _SAFE_ARGS["pytest"]]
    detail = " ".join([executable, *safe_args])
    return _clean(f"Bash: {detail}")


def tool_summary(tool_name: object, tool_input: object) -> str:
    name = tool_name if isinstance(tool_name, str) and tool_name else "Tool"
    data = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return _bash_summary(data.get("command", ""))
    if name == "apply_patch":
        patch = data.get("patch", "")
        if isinstance(patch, str):
            for line in patch.splitlines():
                match = _PATCH_PATH.match(line)
                if match:
                    return _clean(f"Edit: {_basename(match.group(1))}")
        return "Edit"
    if name in {"Read", "Edit", "Write"}:
        filename = _basename(data.get("file_path") or data.get("path") or "")
        return _clean(f"{name}: {filename}" if filename else name)
    return _clean(name.replace("_", " "), 60)


def feed_line(tool_name: object, tool_input: object, hhmm: str | None = None) -> str:
    return _clean(f"{hhmm or datetime.now().strftime('%H:%M')} {tool_summary(tool_name, tool_input)}")


def _normalize_approval_command(command: object) -> str | None:
    """Return a complete, display-safe command or reject physical approval."""
    if not isinstance(command, str) or not command:
        return None
    if any(not 0x20 <= ord(ch) <= 0x7E for ch in command):
        return None
    if any(ch in "[]\\`" for ch in command):
        return None

    # Plain ASCII spaces outside quotes are shell token separators, so collapsing
    # them is semantics-preserving. Everything else is retained byte-for-byte.
    result: list[str] = []
    quote: str | None = None
    pending_space = False
    for char in command:
        if quote:
            result.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            if pending_space and result:
                result.append(" ")
            pending_space = False
            quote = char
            result.append(char)
        elif char == " ":
            pending_space = True
        else:
            if pending_space and result:
                result.append(" ")
            pending_space = False
            result.append(char)
    if quote:
        return None
    normalized = "".join(result).strip()
    return normalized if normalized and len(normalized) <= APPROVAL_HINT_MAX else None


def approval_hint(event: dict) -> str | None:
    """Only an entirely visible Bash command may reach Gerald's approval overlay."""
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    keys = set(tool_input)
    if keys == {"command"}:
        pass
    elif (
        keys == {"command", "description"}
        and isinstance(tool_input.get("description"), str)
    ):
        pass
    else:
        return None
    return _normalize_approval_command(tool_input.get("command"))


def hud_from(event: dict) -> dict:
    hud = {}
    model = event.get("model")
    if isinstance(model, str) and model:
        hud["model"] = _clean(model, 60)
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        hud["project"] = _basename(cwd.rstrip("/\\"))
    return hud


def approval_id(event: dict, nonce: str | None = None) -> str | None:
    session_id = event.get("session_id")
    turn_id = event.get("turn_id")
    if not isinstance(session_id, str) or not session_id or not isinstance(turn_id, str) or not turn_id:
        return None
    identities = [session_id, turn_id, nonce or secrets.token_hex(8)]
    return "codex:" + json.dumps(identities, separators=(",", ":"))


class BridgeClient:
    def __init__(self, path: str | None = None):
        self.path = path or socket_path()

    def send_status(self, **fields) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(STATUS_TIMEOUT)
                sock.connect(self.path)
                sock.sendall((json.dumps({"type": "status", **fields}) + "\n").encode())
        except Exception:
            pass

    def request_approval(self, req_id: str, tool: str, hint: str) -> str:
        if tool != "Bash" or not isinstance(hint, str) or len(hint) > APPROVAL_HINT_MAX:
            return "ask"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(APPROVAL_TIMEOUT)
                sock.connect(self.path)
                request = {"type": "approve", "id": req_id, "tool": tool, "hint": hint}
                sock.sendall((json.dumps(request) + "\n").encode())
                raw = bytearray()
                while b"\n" not in raw and len(raw) < 4096:
                    chunk = sock.recv(4096 - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                reply = json.loads(bytes(raw).split(b"\n", 1)[0].decode())
                decision = reply.get("decision")
                return decision if decision in {"allow", "deny"} else "ask"
        except Exception:
            return "ask"


def _status(client: BridgeClient, **fields) -> None:
    try:
        client.send_status(**fields)
    except Exception:
        pass


def handle_event(event: object, client: BridgeClient) -> dict | None:
    if not isinstance(event, dict):
        return None
    name = event.get("hook_event_name")
    hud = hud_from(event)
    if name == "SessionStart":
        _status(client, state="idle", msg="idle", hud=hud)
    elif name == "UserPromptSubmit":
        _status(client, state="thinking", msg="thinking", hud=hud)
    elif name == "PreToolUse":
        _status(client, state="running", msg="running",
                entry=feed_line(event.get("tool_name"), event.get("tool_input")), hud=hud)
    elif name == "Stop":
        _status(client, state="done", msg="done", hud=hud)
    elif name == "SessionEnd":
        _status(client, state="idle", msg="idle", hud=hud)
    elif name == "PermissionRequest":
        req_id = approval_id(event)
        hint = approval_hint(event)
        if req_id is None or hint is None:
            return None
        try:
            decision = client.request_approval(
                req_id, "Bash", hint)
        except Exception:
            decision = "ask"
        if decision in {"allow", "deny"}:
            result = {"behavior": decision}
            if decision == "deny":
                result["message"] = "Denied once from Gerald"
            return {"hookSpecificOutput": {
                "hookEventName": "PermissionRequest", "decision": result}}
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        output = handle_event(event, BridgeClient())
        if output is not None:
            print(json.dumps(output, separators=(",", ":")))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
