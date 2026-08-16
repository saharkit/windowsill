#!/usr/bin/env bash
# voice-loop — speaking-hook LAUNCHER (Stop, and the opt-in eager PostToolUse). The hook's logic
# lives in speak.py (python3, stdlib only); this wrapper stays as the one stable entry point
# hooks.json registers, so an update to the implementation never touches the hook registration. It
# reads nothing itself — stdin (the hook payload JSON, hook_event_name and all) passes straight
# through to python3, which is what tells the two events apart.
#
# Never fails a turn: no python3 (or a missing speak.py) means silence, not an error.
set -u
command -v python3 >/dev/null 2>&1 || exit 0
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd) || exit 0
[ -f "$DIR/speak.py" ] || exit 0
# speak.py imports the provider registry beside it; a half-copied scripts/ must be silence too,
# not a traceback in the middle of a turn.
[ -f "$DIR/providers.py" ] || exit 0
[ -f "$DIR/contracts.py" ] || exit 0
exec python3 "$DIR/speak.py"
