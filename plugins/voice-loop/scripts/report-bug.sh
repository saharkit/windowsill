#!/usr/bin/env bash
# voice-loop — bug-report LAUNCHER. The collector's logic lives in report_bug.py (python3, stdlib
# only); this wrapper is the one stable entry point the /report-bug skill invokes, so an update to
# the implementation never touches the skill.
#
# Usage: report-bug.sh <collect|transports|url|mailto|gh> [options]   (report-bug.sh collect --help)
#
#   collect      write ONE redacted bundle file and print the exact bytes it contains
#   transports   which of the three report transports this machine has
#   url          a pre-filled GitHub new-issue URL for a bundle (nothing is sent by printing it)
#   mailto       a mailto: URL for a bundle
#   gh           file the bundle as an issue with the gh CLI — this one SENDS; ask the user first
#
# Unlike the hook and hotkey launchers, this one is invoked BY A HUMAN asking for a diagnosis, so a
# missing python3 is an error it says out loud rather than a silent exit 0.
set -u
if ! command -v python3 >/dev/null 2>&1; then
  echo "report-bug: python3 not found — the collector needs Python 3.10+" >&2
  exit 1
fi
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd) || exit 1
if [ ! -f "$DIR/report_bug.py" ]; then
  echo "report-bug: report_bug.py is missing next to this script" >&2
  exit 1
fi
exec python3 "$DIR/report_bug.py" "$@"
