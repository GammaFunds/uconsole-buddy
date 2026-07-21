#!/usr/bin/env python3
# bridge/hooks/pretooluse.py — dünn, zustandslos, fail-safe ask
import json, os, socket, sys
try:
    from ._paths import socket_path
except ImportError:  # Claude executes this hook file directly.
    from _paths import socket_path

SOCK = socket_path()
HINT_MAX = 120


def hint_from(tool_name, tool_input):
    if tool_name == "Bash":
        return (tool_input.get("command", "") or "")[:HINT_MAX]
    return json.dumps(tool_input)[:HINT_MAX]


def out(decision, reason=""):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        out("ask", "hook: bad stdin")
    tool = ev.get("tool_name", "?")
    req_id = f"{ev.get('session_id', 's')}#{os.getpid()}"
    hint = hint_from(tool, ev.get("tool_input", {}) or {})
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(115)
        s.connect(SOCK)
        s.sendall((json.dumps({"type": "approve", "id": req_id, "tool": tool, "hint": hint}) + "\n").encode())
        reply = json.loads(s.recv(400).decode())
        decision = reply.get("decision", "ask")
        if decision not in ("allow", "deny"):
            decision = "ask"
        out(decision, f"uConsole: {decision}")
    except Exception as e:
        out("ask", f"bridge unavailable: {e}")


if __name__ == "__main__":
    main()
