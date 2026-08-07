# Gerald — uConsole Claude & Codex Buddy

**Gerald** is a physical desk companion that reacts to [Claude Code](https://claude.com/claude-code) and [Codex CLI](https://developers.openai.com/codex/cli/) sessions. A handheld terminal (a [ClockworkPi uConsole](https://www.clockworkpi.com/uconsole), Raspberry Pi CM4) sits next to you and shows — as a big drawn face — what the coding agent is doing right now: thinking, working, waiting for you, done. It plays a chime when the agent needs your input, streams a live tool feed, and lets you approve or deny eligible permission requests with a physical key press. Codex activity is privacy-bounded.

It started as a client for Anthropic's official **Hardware Buddy** (BLE) reference and grew into a full terminal-driven buddy via Claude Code hooks.

```
        ◦ Gerald ◦

     ╭───────────────╮
     │   ‾       ‾   │        <- brows carry the emotion
     │   ●       ●   │
     │     ╲___╱     │        <- mouth + eyes morph per state
     ╰───────────────╯

        A R B E I T E         <- big figlet status word (mood-coloured)
      ▱ ▱ ▱ ▱ ▰ ▰ ▰ ▱ ▱       <- running activity bar
      14:23 Bash: npm test    <- live tool feed

     ● connected      ♪ sound on
```

## What it does

- **Ambient mood** — a drawn face (eyebrows + eyes + mouth) morphs and re-colours per state: `idle` · `thinking` · `running` · `waiting` · `done` · `error`.
- **Live activity feed** — the last few tool calls (`Bash: npm test`, `Edit: ui.py`, …).
- **Sound notifications** — a bright chime when the agent needs you (`waiting`), a soft tone when it's `done`, a distinct tone on `error`. Mute with `m`.
- **Physical approvals** — press `Y`/`N` to allow or deny an eligible request once. For Codex, this is limited to Bash requests with authoritative session/turn identity whose complete command is safe to render and fits the 46-character device limit. All other Codex requests use native approval.
- **Full-screen kiosk** with a mood-coloured border and small animations (blinking, running bar, drifting `z z z`).
- **Multilingual** — English (default) and German UI, switch with the `GERALD_LANG` env var.
- **Session HUD** — Claude provides model, context-window fill, and usage limits through its statusline integration. Codex hooks reliably provide model and project only.

## How it works

```
Claude Code hooks ──┐                    Mac / Linux              uConsole (BLE peripheral)
                    ├─ agent adapter ──> bridge daemon    --BLE-->  Textual kiosk UI
Codex hooks ────────┘                    (unix socket,              (bluez-peripheral,
                                          bleak central)             Nordic UART / NUS)
```

The agent-specific hooks send tiny JSON messages to the same daemon Unix socket. The daemon holds one BLE connection to the device and pushes JSON snapshots (`state`, rolling `entries`, …) over **Nordic UART (NUS)**, one JSON object per line — the same protocol as [`anthropics/claude-desktop-buddy`](https://github.com/anthropics/claude-desktop-buddy). Approval requests round-trip through the existing device `permission` (`once`/`deny`) message.

## Repo layout

| Path | What |
|------|------|
| **`bridge/`** | Mac/Linux side: Claude and Codex hook adapters + the BLE daemon (central). |
| **`device/`** | uConsole side: the Textual kiosk UI + BLE peripheral. |
| **`docs/`** | `SETUP.md` (getting started) + the original German design specs & plans. |

Both parts keep their own git history (imported via `git subtree`).

## Requirements

- **Bridge host** (where you run Claude Code or Codex): macOS or Linux, Python 3, Bluetooth LE. Depends on `bleak`.
- **Device**: a Linux box with BlueZ + a BLE adapter (built on a uConsole / Raspberry Pi CM4, Python 3.11). Depends on `bluez-peripheral`, `dbus-fast`, `textual`, `pyfiglet`. A Wayland compositor (labwc) for the kiosk look, but any terminal works for testing.
- **Claude Code** ≥ v2.1.210 (hook `permissionDecision` contract), or **Codex CLI** with stable lifecycle hooks and `PermissionRequest` support (verified with `codex-cli 0.144.6`).

## Quick start

See **[docs/SETUP.md](docs/SETUP.md)** for the full walkthrough (device + bridge + wiring the hooks + kiosk + troubleshooting).

The bridge socket defaults to `~/.uconsole-buddy/run/bridge.sock`. Set `UCONSOLE_BRIDGE_SOCK` to override it; hook command paths still need to point at your clone.

## Credits

Built on Anthropic's [`claude-desktop-buddy`](https://github.com/anthropics/claude-desktop-buddy) protocol (reference firmware: M5StickC Plus). This project adds a uConsole peripheral, a Claude-Code-hooks bridge for terminal sessions, and the Gerald kiosk UI. Design specs & implementation plans (German) live in `docs/`.
