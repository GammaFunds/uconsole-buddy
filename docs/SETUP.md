# Setup — Gerald (uConsole Claude & Codex Buddy)

Get Gerald running end to end. Two machines are involved:

- **Device** — the BLE peripheral that shows the face (a uConsole / Raspberry Pi CM4, or any Linux box with BlueZ). Code in `device/`.
- **Bridge host** — where you run Claude Code or Codex CLI (macOS or Linux). Runs the BLE daemon + the hook scripts. Code in `bridge/`.

The Unix socket defaults to `~/.uconsole-buddy/run/bridge.sock`. Set
`UCONSOLE_BRIDGE_SOCK` before starting the daemon and agent if you need a different path.

---

## 1. Device (`device/`)

On the uConsole / Linux device:

```bash
git clone https://github.com/Nikolaibibo/uconsole-buddy.git
cd uconsole-buddy/device
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # bluez-peripheral, dbus-fast, textual, pyfiglet
.venv/bin/python -m pytest -q                   # optional: pure-logic tests, no hardware needed
```

**Pick the right Bluetooth adapter.** The app advertises via BlueZ on the onboard
controller. On the uConsole that's `hci0` (onboard Cypress, UART); a USB dongle may show up
as `hci1` and hijack `bluetoothctl`'s default — pin the right one. Check with `hciconfig`.

**Run it:**

```bash
.venv/bin/python -m companion.main
```

It starts a Textual UI and advertises as `Claude-uConsole` over Nordic UART. With no bridge
connected it shows the `offline` face — that's expected until step 2 connects.

Tests live in `device/tests/` (`pytest`), the package is `device/companion/`.

---

## 2. Bridge host (`bridge/`)

On the machine where you run Claude Code:

```bash
cd uconsole-buddy/bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # bleak
.venv/bin/python -m pytest -q                   # optional
```

**Run the daemon** (it scans for the device by NUS service UUID, connects, and listens on a
unix socket):

```bash
.venv/bin/python -m bridge.daemon
```

- **macOS:** the first BLE access triggers a one-time Bluetooth permission prompt for the
  terminal (System Settings → Privacy & Security → Bluetooth). Grant it.
- Watch `bridge/bridge.log` for `BLE connected to uConsole`.

The daemon self-heals: if the device app restarts or the link drops, a failed send (or the
disconnect callback) triggers an automatic reconnect loop — no manual restart needed.

While connected, the daemon also refreshes Gerald's current snapshot every 10 seconds so
the device's 30-second liveness timeout does not mark a healthy idle link offline. During
an active approval, the heartbeat keeps the approval overlay while preserving the latest
retained feed and HUD metadata.

---

## 3. Wire up an agent

### Claude Code

The buddy reacts to your sessions through Claude Code **hooks**. Merge the hooks from
`bridge/settings-snippet.json` into your project's `.claude/settings.json` (or your user
settings), **fixing the absolute paths** to where you cloned `bridge/`.

Hooks used:

| Hook | Effect |
|------|--------|
| `SessionStart` / `UserPromptSubmit` | `thinking` |
| `PostToolUse` (`*`) | `running` + appends a feed line |
| `PreToolUse` (`Bash`) | approval overlay → `Y`/`N` on the device → `allow`/`deny` |
| `Notification` | `waiting` (chime) |
| `Stop` | `done` → decays to `idle` after ~5 s |
| `SessionEnd` | `idle` |

The status hooks are fire-and-forget: if the daemon isn't running, they no-op and Claude Code
behaves normally. Start the daemon **before** `claude`.

> The `PreToolUse(Bash)` hook routes **every** Bash command through the device for approval.
> If you keep a large allow-list, consider omitting that one hook and using only the status
> hooks (mood/feed/sound) — those never touch your permissions.

---

### Codex

Codex support uses lifecycle hooks and does not start a separate app server or replace the
Codex frontend. Choose the hook scope that matches how you want Gerald to behave:

1. For a Gerald buddy that follows your Codex activity across projects, including Codex
   Desktop sessions, copy `bridge/codex/hooks.json` to `~/.codex/hooks.json`, or merge its
   event groups into an existing user hook file.
2. For one repository only, copy it to that repository's `.codex/hooks.json`, or merge its
   event groups into the existing project hook file. Avoid installing the same Gerald hooks
   at both user and project scope because Codex loads matching hooks from multiple sources.
3. Replace `/absolute/path/to/uconsole-buddy` in every Gerald command with this clone's
   absolute path.
4. Start the Gerald bridge daemon, run `codex`, then use `/hooks` to inspect the loaded
   source and review and trust the exact Gerald hook definitions. Changed hook definitions
   must be reviewed again before they run.

User hooks load from the active user configuration layer independently of project-local
hook trust. Project-local hooks additionally depend on the repository's `.codex/` layer
being trusted.

The adapter maps only signals defined by the supported hook schemas:

| Codex hook | Gerald effect |
|------------|---------------|
| `SessionStart` (`startup`, `resume`, `clear`) | initial `idle` + model/project HUD |
| `UserPromptSubmit` | `thinking` (prompt text is discarded) |
| `PreToolUse` | `running` + sanitized, maximum-120-character activity line |
| `PermissionRequest` | eligible short Bash requests show `waiting`; `Y` allows once, `N` denies once |
| `Stop` | `done`, then the existing daemon decays to `idle` |
| `SessionEnd` | `idle` when Codex dispatches the lifecycle event |

`SessionEnd` support is additive. Codex versions or surfaces that do not
dispatch it reliably on TUI shutdown still fall back to the existing
`Stop` -> `done` -> `idle` decay.

Physical Codex approval is available only when the request is Bash, authoritative `session_id`
and `turn_id` values exist, and the complete command can be rendered safely and fits the
46-character device approval display. All other requests fall back to Codex's native approval
handling.

Approval IDs bind the Codex `session_id` and `turn_id` plus the unique hook process. Only
one Gerald approval can be visible at a time. A stale or duplicate device response is ignored;
a concurrent request, timeout, bridge failure, BLE disconnect, or canceled hook connection
also falls back to native approval. No session or persistent allow choice is returned.

#### Activity feed

The activity feed is a privacy-bounded summary only. It does not send arbitrary command
arguments, prompts, assistant responses, tool output, file contents, or environment values.
Bash feed entries expose only a command name and a small allow-list of non-sensitive flags;
patch entries expose only the affected basename.

#### Physical approval

For an eligible short Bash command, the complete command is intentionally sent to Gerald
because the human must see the exact action before approving it.

Current Codex-hook limitations are deliberate: hooks provide no reliable context percentage or
account usage-limit values, and no distinct turn-failure or cancellation event. Therefore the
Codex path leaves context/usage HUD fields empty and does not fabricate `error` or a cancellation
state. Hosted tools that do not emit `PreToolUse` also cannot appear in the live feed.

## 4. Configure the socket and command paths

The daemon and both agent adapters use this resolution order:

1. `UCONSOLE_BRIDGE_SOCK`, with `~` expansion, when set.
2. `~/.uconsole-buddy/run/bridge.sock` otherwise.

Keep the environment override identical for the daemon and agent process. Claude's
`bridge/settings-snippet.json` and Codex's `bridge/codex/hooks.json` still contain example
absolute command paths; replace those with the path to your clone.

---

## 5. Kiosk & autostart (optional, uConsole/labwc)

To make it a full-screen appliance instead of a windowed terminal:

- **`device/run-debug.sh`** is a supervising wrapper: it runs `sudo systemctl restart bluetooth`
  (clears any orphaned GATT registration from a killed instance — see Troubleshooting), then
  loops the app, auto-restarting on crash and exiting cleanly on `q`.
- **`device/launch-display.sh`** kills any old instance (window + wrapper + app) and launches
  the wrapper in an `lxterminal`. Wire it into your compositor autostart
  (e.g. labwc `~/.config/labwc/autostart`).
- **lxterminal** (`~/.config/lxterminal/lxterminal.conf`): `hidemenubar=true`,
  `hidescrollbar=true`, `hidepointer=true`, and a larger `fontname` (e.g. `Monospace 18`) to
  make the face big.
- **labwc** (`~/.config/labwc/rc.xml`): a window rule to full-screen it and drop the titlebar:
  ```xml
  <windowRules>
    <windowRule identifier="lxterminal" serverDecoration="no" skipTaskbar="yes">
      <action name="ToggleFullscreen"/>
    </windowRule>
  </windowRules>
  ```
- **Volume**: set it once (`amixer sset Master 85% unmute`) and persist with `sudo alsactl store`.

These live under `~/.config` on the device, not in this repo.

---

## Language

The device UI is localized (English + German). Pick the language with the `GERALD_LANG`
environment variable, read once at startup:

```bash
GERALD_LANG=en .venv/bin/python -m companion.main   # English (default)
GERALD_LANG=de .venv/bin/python -m companion.main   # German
```

Default is `en`. The kiosk wrapper (`device/run-debug.sh`) exports a fixed value for the
appliance — change that line to switch your device permanently. Strings live in
`device/companion/i18n.py`; add a language by adding a block to `_STRINGS`.

## Troubleshooting

- **Only one BLE central at a time.** The device firmware holds a single connection. Disconnect
  Anthropic's Hardware Buddy (or any other connected app) before starting the daemon.
- **`connect()` times out / "Multiple Characteristics with this UUID".** A killed device app can
  leave an orphaned GATT registration in BlueZ, so the peripheral advertises but rejects new
  connects. Restarting `bluetooth` on the device clears it — the `run-debug.sh` wrapper does this
  on every launch.
- **Nothing renders / black screen.** Usually two app instances fighting over the display —
  make sure only one `companion.main` runs (`pgrep -f companion.main`); `launch-display.sh` kills
  old instances for you.
- **No sound.** Check the mixer isn't at a low volume (`amixer sget Master`) and that the default
  sink is your speaker, not HDMI (`pactl list short sinks`).
- **macOS launchd autostart fails with "Operation not permitted".** macOS TCC blocks launchd
  background jobs from `~/Documents`. Either move the daemon out of `~/Documents` (and the socket
  to `/tmp`), or grant the daemon's interpreter Full Disk Access. Running the daemon from a normal
  terminal works without this.

## Known limitations

- Hook command paths in the example JSON must be adjusted to the clone location.
- Claude and Codex hooks currently expose no reliable event that drives `error`.
- Token counters are always `0` (Claude Code hook payloads aren't wired to real usage yet).
- Codex lifecycle hooks expose model/project but not context percentage or account usage limits.
- Kiosk config lives under `~/.config` on the device, outside this repo.
