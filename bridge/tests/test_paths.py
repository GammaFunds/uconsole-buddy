import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

from bridge.hooks._paths import socket_path


def test_socket_path_explicit_override(monkeypatch):
    monkeypatch.setenv("UCONSOLE_BRIDGE_SOCK", "~/custom/gerald.sock")
    assert socket_path() == os.path.expanduser("~/custom/gerald.sock")


def test_socket_path_user_local_default(monkeypatch):
    monkeypatch.delenv("UCONSOLE_BRIDGE_SOCK", raising=False)
    assert socket_path() == os.path.expanduser("~/.uconsole-buddy/run/bridge.sock")


def test_hook_helpers_support_package_import_without_conftest_path(monkeypatch):
    hooks_dir = Path(__file__).parents[1] / "bridge" / "hooks"
    monkeypatch.setattr(sys, "path", [item for item in sys.path if item != str(hooks_dir)])
    sys.modules.pop("bridge.hooks._send", None)
    sys.modules.pop("bridge.hooks.pretooluse", None)

    sender = importlib.import_module("bridge.hooks._send")
    pretooluse = importlib.import_module("bridge.hooks.pretooluse")

    assert callable(sender.send_status)
    assert callable(pretooluse.main)


def test_hook_helpers_support_direct_script_execution(tmp_path):
    hooks_dir = Path(__file__).parents[1] / "bridge" / "hooks"
    env = os.environ.copy()
    env["UCONSOLE_BRIDGE_SOCK"] = str(tmp_path / "missing.sock")

    send_result = subprocess.run(
        [sys.executable, str(hooks_dir / "_send.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    pretool_result = subprocess.run(
        [sys.executable, str(hooks_dir / "pretooluse.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "true"}}),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert send_result.returncode == 0
    assert pretool_result.returncode == 0
    assert json.loads(pretool_result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
