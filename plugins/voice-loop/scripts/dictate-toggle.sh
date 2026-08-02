#!/usr/bin/env bash
# voice-loop — push-to-talk toggle LAUNCHER. The toggle's logic lives in dictate.py (python3,
# stdlib only); this wrapper stays as the one stable entry point hotkey bindings register
# (gsettings / sway bindsym / skhd all point here), so an update to the implementation never
# touches the keybinding. Bind it to a global hotkey (e.g. F9):
#   press once  -> recording starts
#   press again -> recording stops, audio goes to STT, the text lands in your clipboard
#                  (and, if auto_paste is enabled, is pasted into the focused window)
#
# Usage: dictate-toggle.sh [send|paste]
#   send  (default) paste the text AND press Enter — hands-free prompting
#   paste           paste only, you press Enter yourself
#
# Config: ~/.config/voice-loop/config.json (see README). Zero-root by default: the text is put on
# the clipboard and you press your own paste key. Auto-paste is the opt-in tier.
#
# A hotkey with no python3 (or a missing dictate.py) does nothing rather than half-recording.
set -u
command -v python3 >/dev/null 2>&1 || exit 0
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd) || exit 0
[ -f "$DIR/dictate.py" ] || exit 0
exec python3 "$DIR/dictate.py" "$@"
