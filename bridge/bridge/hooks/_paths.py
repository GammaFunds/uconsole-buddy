"""Shared bridge socket-path resolution for standalone hook scripts."""
import os


DEFAULT_SOCKET = "~/.uconsole-buddy/run/bridge.sock"


def socket_path() -> str:
    return os.path.expanduser(os.environ.get("UCONSOLE_BRIDGE_SOCK", DEFAULT_SOCKET))
