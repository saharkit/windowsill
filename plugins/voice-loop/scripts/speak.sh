#!/usr/bin/env bash
# voice-loop — Stop hook LAUNCHER. The hook's logic lives in speak.py (python3, stdlib only);
# this wrapper stays as the one stable entry point hooks.json registers, so an update to the
# implementation never touches the hook registration. It reads nothing itself — stdin (the hook
# payload JSON) passes straight through to python3.
#
# Never fails a turn: no python3 (or a missing speak.py) means silence, not an error.
set -u
command -v python3 >/dev/null 2>&1 || exit 0
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd) || exit 0
[ -f "$DIR/speak.py" ] || exit 0
exec python3 "$DIR/speak.py"
