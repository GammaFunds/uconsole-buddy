import io
import json
import socket

from codex.gerald import (
    ACTIVITY_MAX,
    APPROVAL_HINT_MAX,
    BridgeClient,
    approval_id,
    approval_hint,
    feed_line,
    handle_event,
    main,
    tool_summary,
)


class FakeClient:
    def __init__(self, decision="ask", fail=False):
        self.decision = decision
        self.fail = fail
        self.statuses = []
        self.approvals = []

    def send_status(self, **fields):
        if self.fail:
            raise OSError("bridge unavailable")
        self.statuses.append(fields)

    def request_approval(self, req_id, tool, hint):
        if self.fail:
            raise socket.timeout()
        self.approvals.append((req_id, tool, hint))
        return self.decision


def event(name, **extra):
    return {
        "hook_event_name": name,
        "session_id": "session-1",
        "turn_id": "turn-2",
        "cwd": "/work/gerald",
        "model": "gpt-5.4",
        **extra,
    }


def test_lifecycle_and_hud_mapping():
    client = FakeClient()
    handle_event(event("SessionStart"), client)
    handle_event(event("UserPromptSubmit", prompt="never send me"), client)
    handle_event(event("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}), client)
    handle_event(event("Stop", last_assistant_message="never send this either"), client)

    assert [item["state"] for item in client.statuses] == ["idle", "thinking", "running", "done"]
    assert client.statuses[0]["hud"] == {"model": "gpt-5.4", "project": "gerald"}
    assert client.statuses[2]["entry"].endswith("Bash: pytest -q")
    transmitted = json.dumps(client.statuses)
    assert "never send me" not in transmitted
    assert "never send this either" not in transmitted


def test_patch_feed_uses_only_filename_not_contents():
    patch = "*** Begin Patch\n*** Update File: bridge/codex/gerald.py\n@@\n-SECRET\n+TOP_SECRET\n*** End Patch"
    summary = tool_summary("apply_patch", {"patch": patch})
    assert summary == "Edit: gerald.py"
    assert "SECRET" not in summary


def test_apply_patch_command_key_summarizes_filename_not_contents():
    patch = "*** Begin Patch\n*** Update File: bridge/codex/gerald.py\n@@\n-SECRET\n+TOP_SECRET\n*** End Patch"
    summary = tool_summary("apply_patch", {"command": patch})
    assert summary == "Edit: gerald.py"
    assert "SECRET" not in summary
    assert "TOP_SECRET" not in summary


def test_bash_feed_redacts_environment_and_sensitive_arguments():
    command = "API_TOKEN=supersecret curl -H 'Authorization: Bearer hidden' https://private.example"
    summary = tool_summary("Bash", {"command": command})
    assert summary == "Bash"
    assert "supersecret" not in summary
    assert "hidden" not in summary
    assert "private.example" not in summary


def test_activity_is_sanitized_and_bounded():
    line = feed_line("mcp__bad\nname", {"value": "ignored"}, "12:34")
    assert "\n" not in line
    assert len(line) <= ACTIVITY_MAX
    assert len(feed_line("x" * 300, {}, "12:34")) <= ACTIVITY_MAX


def test_approval_identity_binds_session_and_turn():
    req_id = approval_id(event("PermissionRequest"), nonce="unique-request")
    assert req_id == 'codex:["session-1","turn-2","unique-request"]'


def test_approval_without_authoritative_identity_falls_through():
    client = FakeClient("allow")
    assert handle_event({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}, client) is None
    assert client.approvals == []


def test_approval_allow_once_and_deny_outputs():
    allow = FakeClient("allow")
    output = handle_event(event("PermissionRequest", tool_name="Bash",
                                tool_input={"command": "pytest -q"}), allow)
    assert output == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                              "decision": {"behavior": "allow"}}}
    assert allow.approvals[0][1] == "Bash"
    assert allow.approvals[0][2] == "pytest -q"

    deny = FakeClient("deny")
    output = handle_event(event("PermissionRequest", tool_name="Bash",
                                tool_input={"command": "pytest -q"}), deny)
    assert output["hookSpecificOutput"]["decision"] == {
        "behavior": "deny", "message": "Denied once from Gerald"}


def test_short_complete_bash_command_is_physically_approvable():
    client = FakeClient("allow")
    output = handle_event(event("PermissionRequest", tool_name="Bash",
                                tool_input={"command": "  pytest   -q  "}), client)
    assert output["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert client.approvals[0][2] == "pytest -q"
    assert len(client.approvals[0][2]) <= APPROVAL_HINT_MAX


def test_approval_hint_preserves_complete_quoted_content():
    command = "printf   'keep  these spaces'"
    assert approval_hint(event("PermissionRequest", tool_name="Bash",
                               tool_input={"command": command})) == command.replace("   ", " ", 1)


def test_long_bash_command_falls_through_without_contacting_gerald():
    client = FakeClient("allow")
    command = "echo " + "x" * (APPROVAL_HINT_MAX + 1)
    assert handle_event(event("PermissionRequest", tool_name="Bash",
                              tool_input={"command": command}), client) is None
    assert client.approvals == []


def test_unsafe_or_malformed_bash_command_falls_through():
    client = FakeClient("allow")
    for command in ("echo 'unterminated", "echo hi\\ world", "echo hi\nrm -rf /"):
        assert handle_event(event("PermissionRequest", tool_name="Bash",
                                  tool_input={"command": command}), client) is None
    assert client.approvals == []


def test_render_deceptive_or_unicode_bash_commands_cannot_allow():
    commands = (
        "echo [conceal]; rm -rf /tmp/x # [/]",
        "echo [black]; rm -rf /tmp/x # [/]",
        "echo [bold]hidden[/]",
        "echo safe\u202e; rm -rf /tmp/x",
        "echo zero\u200bwidth",
        "printf %s a\u00a0b",
        "printf %s a\u2003b",
        "echo\tunsafe",
        "echo safe\nrm -rf /tmp/x",
        "echo bell\x07",
        "echo del\x7f",
        "echo 'unterminated",
        'echo "unterminated',
        "echo hi\\ world",
    )

    for command in commands:
        client = FakeClient("allow")
        permission = event(
            "PermissionRequest", tool_name="Bash", tool_input={"command": command}
        )
        assert approval_hint(permission) is None
        assert handle_event(permission, client) is None
        assert client.approvals == []


def test_permission_request_accepts_optional_text_description():
    client = FakeClient("allow")
    output = handle_event(event(
        "PermissionRequest", tool_name="Bash",
        tool_input={"command": "pytest -q", "description": "Run focused tests"}), client)
    assert output["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert client.approvals[0][1] == "Bash"
    assert client.approvals[0][2] == "pytest -q"


def test_permission_request_accepts_null_description():
    client = FakeClient("allow")
    assert approval_hint(event(
        "PermissionRequest", tool_name="Bash",
        tool_input={"command": "pytest -q", "description": None})) == "pytest -q"


def test_permission_request_unknown_extra_key_fails_closed():
    client = FakeClient("allow")
    assert handle_event(
        event("PermissionRequest", tool_name="Bash",
              tool_input={"command": "pytest -q", "unexpected": "value"}), client) is None
    assert client.approvals == []


def test_permission_request_non_string_non_null_description_fails_closed():
    client = FakeClient("allow")
    assert handle_event(
        event("PermissionRequest", tool_name="Bash",
              tool_input={"command": "pytest -q", "description": 123}), client) is None
    assert client.approvals == []


def test_non_bash_permission_falls_through_and_cannot_allow():
    client = FakeClient("allow")
    assert handle_event(event("PermissionRequest", tool_name="apply_patch",
                              tool_input={"patch": "*** Update File: x"}), client) is None
    assert client.approvals == []


def test_approval_timeout_or_bridge_failure_falls_through():
    assert handle_event(event("PermissionRequest", tool_name="Bash", tool_input={}), FakeClient(fail=True)) is None
    missing = BridgeClient("/definitely/missing/gerald.sock")
    assert missing.request_approval("id", "Bash", "Bash") == "ask"
    missing.send_status(state="thinking")


def test_bridge_client_read_timeout_falls_through(monkeypatch):
    class TimedOutSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def sendall(self, _data):
            pass

        def recv(self, _size):
            raise socket.timeout()

    monkeypatch.setattr("codex.gerald.socket.socket", lambda *_args: TimedOutSocket())
    assert BridgeClient("/fake/gerald.sock").request_approval("id", "Bash", "Bash") == "ask"
    assert BridgeClient("/fake/gerald.sock").request_approval("id", "Other", "safe") == "ask"


def test_unknown_and_malformed_events_are_ignored():
    client = FakeClient()
    assert handle_event(None, client) is None
    assert handle_event({"hook_event_name": "FutureEvent", "prompt": "secret"}, client) is None
    assert client.statuses == []


def test_main_malformed_input_never_breaks_codex(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main() == 0
