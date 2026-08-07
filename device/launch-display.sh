#!/usr/bin/env bash
SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &&
    pwd -P
)" || {
  printf '%s\n' \
    'UCONSOLE_MANAGEMENT_GATE status=INTERNAL_CONTRACT_ERROR code=script_directory_unavailable' >&2
  exit 22
}
MODE_MODULE="$SCRIPT_DIR/uconsole_mode.py"
MODE_PYTHON="$SCRIPT_DIR/.venv/bin/python"

run_uconsole_management_gate() {
  if test ! -f "$MODE_MODULE" ||
     test -L "$MODE_MODULE" ||
     test ! -r "$MODE_MODULE"
  then
    printf '%s\n' \
      'UCONSOLE_MANAGEMENT_GATE status=INTERNAL_CONTRACT_ERROR code=module_unavailable' >&2
    return 22
  fi

  if test ! -x "$MODE_PYTHON"; then
    printf '%s\n' \
      'UCONSOLE_MANAGEMENT_GATE status=INTERNAL_CONTRACT_ERROR code=interpreter_unavailable' >&2
    return 22
  fi

  (
    unset PYTHONPATH PYTHONHOME
    export PYTHONNOUSERSITE=1
    export PYTHONDONTWRITEBYTECODE=1

    exec "$MODE_PYTHON" \
      -I \
      -B \
      "$MODE_MODULE" \
      management-gate
  )
}

run_uconsole_management_gate
gate_rc=$?
if test "$gate_rc" -ne 0; then
  exit "$gate_rc"
fi

UID_N=$(id -u)
export XDG_RUNTIME_DIR=/run/user/$UID_N
export WAYLAND_DISPLAY=wayland-0
export DISPLAY=:0
for xa in /run/user/$UID_N/.mutter-Xwaylandauth.* "$HOME/.Xauthority"; do
  [ -f "$xa" ] && export XAUTHORITY="$xa"
done
# Alte Instanz KOMPLETT beenden: erst Fenster + Supervising-Wrapper, dann die App.
# (Sonst startet der Wrapper die App nach dem companion.main-kill sofort neu -> Doppel.)
pkill -f "lxterminal.*Claude-Companion" 2>/dev/null
pkill -f run-debug.sh 2>/dev/null
pkill -f companion.main 2>/dev/null
sleep 2
setsid lxterminal --title=Claude-Companion -e "$SCRIPT_DIR/run-debug.sh" >/tmp/lxterm-launch.log 2>&1 &
disown
