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

cd "$SCRIPT_DIR" || {
  printf '%s\n' \
    'UCONSOLE_MANAGEMENT_GATE status=INTERNAL_CONTRACT_ERROR code=script_directory_unavailable' >&2
  exit 22
}
export GERALD_LANG=de
source .venv/bin/activate
while true; do
  run_uconsole_management_gate
  gate_rc=$?
  if test "$gate_rc" -ne 0; then
    exit "$gate_rc"
  fi
  # BlueZ frisch: verwaiste GATT-Registrierung (SIGKILL/Crash-Reste) + Controller-Reset (#1)
  sudo systemctl restart bluetooth 2>/dev/null
  sleep 4
  python -m companion.main
  code=$?
  [ "$code" -eq 0 ] && break        # sauberes Quit (q) -> nicht neu starten
  echo "[companion crash (exit $code) - Neustart in 3s]"
  sleep 3
done
