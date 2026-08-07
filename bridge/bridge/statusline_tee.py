#!/usr/bin/env python3
# bridge/statusline_tee.py — statusLine-Wrapper: HUD-Daten an Gerald teen, dann claude-hud rendern.
"""Claude Code ruft dieses Script als statusLine-Command auf. Es
1) liest den Statusline-Payload (stdin, JSON),
2) schickt extrahierte HUD-Felder fire-and-forget an den Bridge-Socket (gedrosselt),
3) reicht den Payload unverändert an claude-hud (bun) weiter und gibt dessen Output aus.
claude-hud darf NIE blockiert oder gebrochen werden — jeder Fehler wird geschluckt."""
import glob
import json
import os
import socket
import subprocess
import sys
import time
try:
    from .hooks._paths import socket_path
except ImportError:  # Claude may execute this file directly rather than as a module.
    from hooks._paths import socket_path

SOCK = socket_path()
USAGE_CACHE = os.path.expanduser("~/.claude/plugins/claude-hud/.usage-cache.json")
STATE_FILE = "/tmp/gerald-hud-state.json"
HEARTBEAT_S = 30.0
BUN = os.path.expanduser("~/.bun/bin/bun")
PLUGIN_GLOB = os.path.expanduser("~/.claude/plugins/cache/claude-hud/claude-hud/*/")


def extract_hud(stdin_obj: dict, cache_obj: dict | None) -> dict:
    """Statusline-Payload + Usage-Cache → kompaktes hud-Dict. Fehlende Felder werden weggelassen."""
    hud: dict = {}
    model = (stdin_obj.get("model") or {}).get("display_name")
    if model:
        hud["model"] = model
    pct = (stdin_obj.get("context_window") or {}).get("used_percentage")
    if isinstance(pct, (int, float)):
        hud["ctx_pct"] = round(pct)
    ws = stdin_obj.get("workspace") or {}
    proj = ws.get("project_dir") or ws.get("current_dir") or stdin_obj.get("cwd") or ""
    if proj:
        hud["project"] = os.path.basename(proj.rstrip("/"))
    data = None
    if isinstance(cache_obj, dict):
        data = cache_obj.get("data") or cache_obj.get("lastGoodData")
    if isinstance(data, dict):
        if isinstance(data.get("fiveHour"), (int, float)):
            hud["usage_5h"] = round(data["fiveHour"])
        if isinstance(data.get("sevenDay"), (int, float)):
            hud["usage_7d"] = round(data["sevenDay"])
        if data.get("fiveHourResetAt"):
            hud["reset_5h_iso"] = data["fiveHourResetAt"]
        if data.get("planName"):
            hud["plan"] = data["planName"]
    return hud


def should_send(prev: dict | None, hud: dict, now: float) -> bool:
    """Senden bei Wertänderung oder Heartbeat (>30s). Leeres hud → nie."""
    if not hud:
        return False
    if not prev or prev.get("hud") != hud:
        return True
    return (now - prev.get("sent_at", 0)) > HEARTBEAT_S


def _send_to_bridge(hud: dict) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        s.connect(SOCK)
        s.sendall((json.dumps({"type": "status", "hud": hud}) + "\n").encode("utf-8"))


def _latest_plugin_dir() -> str | None:
    def key(p: str):
        v = os.path.basename(p.rstrip("/"))
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)
    dirs = glob.glob(PLUGIN_GLOB)
    return sorted(dirs, key=key)[-1] if dirs else None


def main() -> int:
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        pass  # Fehler beim stdin-Lesen → mit leerem payload weitermachen
    try:
        stdin_obj = json.loads(raw.decode("utf-8"))
        if not isinstance(stdin_obj, dict):
            stdin_obj = {}
        try:
            with open(USAGE_CACHE) as f:
                cache_obj = json.load(f)
        except Exception:
            cache_obj = None
        hud = extract_hud(stdin_obj, cache_obj)
        now = time.time()
        try:
            with open(STATE_FILE) as f:
                prev = json.load(f)
        except Exception:
            prev = None
        if should_send(prev, hud, now):
            try:
                _send_to_bridge(hud)
            except Exception:
                pass  # Daemon down → egal, claude-hud läuft weiter
            try:
                with open(STATE_FILE, "w") as f:
                    json.dump({"sent_at": now, "hud": hud}, f)
            except Exception:
                pass
    except Exception:
        pass  # Tee darf die Statusline nie brechen
    try:
        plugin_dir = _latest_plugin_dir()
        if plugin_dir is None:
            return 0
        proc = subprocess.run([BUN, os.path.join(plugin_dir, "src", "index.ts")], input=raw)
        return proc.returncode
    except Exception:
        return 0  # Fehler beim Plugin-Lookup oder -Ausführung → lautlos ignorieren


if __name__ == "__main__":
    sys.exit(main())
