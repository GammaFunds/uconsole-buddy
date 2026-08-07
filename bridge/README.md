# uConsole Claude Companion — Bridge (Terminal)

Verbindet Claude Code (Terminal) via PreToolUse-Hook + BLE-Daemon mit der uConsole.
Gerät = Peripheral `Claude-uConsole`. Spec/Plan: Vault `Privat/Projekte/Uconsole/`.

## Voraussetzung: nur EIN Central gleichzeitig

Die uConsole-Firmware hält nur eine BLE-Verbindung. **Bevor der Daemon läuft, "Hardware
Buddy" (oder jede andere App, die schon mit der uConsole verbunden ist) trennen** — sonst
findet `BleCentral.connect()` das Gerät zwar per NUS-Service-UUID, der Connect schlägt aber
fehl oder die andere App wird gekickt.

## Start — manuell

    source .venv/bin/activate
    python -m bridge.daemon        # Daemon (hält BLE + Socket), Ctrl-C zum Stoppen
    pytest -q                      # Pure-Logic-Tests (kein Hardware nötig)

`bridge/run.sh` macht dasselbe (aktiviert `.venv`, startet `python -m bridge.daemon`) und ist
das Skript, das launchd aufruft.

## Start — launchd (Autostart + Keepalive)

Der Daemon läuft als User-Agent, startet bei Login neu und wird von launchd neu gestartet,
falls er abstürzt (`KeepAlive`).

    cp com.uconsole.bridge.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.uconsole.bridge.plist
    launchctl list | grep uconsole        # sollte laufen (PID sichtbar)

Stoppen / deaktivieren:

    launchctl unload ~/Library/LaunchAgents/com.uconsole.bridge.plist

Logs bei launchd-Betrieb:

- App-Log: `bridge.log` im Repo-Root (relativ zu `run.sh`s `cd`)
- stdout/stderr des launchd-Jobs: `/tmp/uconsole-bridge.stdout.log`, `/tmp/uconsole-bridge.stderr.log`

## BLE-Reconnect

Fällt die BLE-Verbindung weg (Gerät außer Reichweite, uConsole-App neu gestartet, …), erkennt
`BleCentral` das über `disconnected_callback` und:

1. löst alle offenen Approval-Prompts sofort fail-safe auf `"ask"` auf (`Bridge.fail_pending`)
   — ein hängender PreToolUse-Hook bekommt so garantiert eine Antwort, nie ein Timeout-Hang.
2. versucht im Hintergrund automatisch neu zu verbinden (Backoff 2s → 4s → 8s → 15s, dann
   alle 15s), bis die Verbindung wieder steht.

Kein manuelles Eingreifen nötig — der Daemon muss dafür nicht neu gestartet werden.

## Troubleshooting

**macOS Bluetooth-Permission (TCC).** Beim allerersten BLE-Connect fragt macOS einmalig,
ob das ausführende Programm (Terminal / iTerm / launchd-Kontext) auf Bluetooth zugreifen darf.
Erlauben unter **Systemeinstellungen → Datenschutz & Sicherheit → Bluetooth**. Läuft der Daemon
über launchd, braucht **der launchd-Kontext selbst** die Freigabe — das ist nicht automatisch
dieselbe Freigabe wie fürs manuell gestartete Terminal. Zeigt `bridge.log` nach dem Start
keinen `"BLE connected to uConsole"`-Eintrag und hängt bei `find_device_by_filter`, zuerst hier
nachsehen. Workaround: Daemon vorübergehend manuell aus einem bereits freigegebenen Terminal
starten, bis die TCC-Freigabe für den launchd-Kontext gesetzt ist.

**Reihenfolge: Daemon vor `claude` starten.** Der `PreToolUse`-Hook verbindet sich pro
Tool-Call neu zum aufgelösten Unix-Socket (`UCONSOLE_BRIDGE_SOCK` oder
`~/.uconsole-buddy/run/bridge.sock`) und ist fail-safe auf `"ask"`, wenn der
Socket nicht existiert — läuft der Daemon nicht, bekommt man einfach den normalen nativen
Terminal-Prompt statt eines Bridge-Fehlers. Für den vollen uConsole-Loop den Daemon (manuell
oder via launchd) **vor** dem Start der `claude`-Session laufen haben.

**Nur ein Central.** Siehe oben — Hardware Buddy (oder andere verbundene Apps) vorher trennen.

**Socket-Pfad.** Zuerst `UCONSOLE_BRIDGE_SOCK` (mit `~`-Expansion), sonst
`~/.uconsole-buddy/run/bridge.sock`. Daemon und Hooks müssen denselben Override verwenden.
