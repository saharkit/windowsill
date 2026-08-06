#!/usr/bin/env bash
# voice-loop — contour poller LAUNCHER. The poller's logic lives in contour_poll.py (python3,
# stdlib only); this wrapper is the stable entry point a cron line or a systemd --user timer is
# written against, so an update to the implementation never changes the scheduled command.
#
# One poll per run: every configured service's /health, free VRAM, the alert rules of #40
# (unreachable, device demoted off its expected device, free VRAM under the floor, oom_overflows
# rising or restarting) — written atomically to contour.json (contour.status_path, default in the
# state dir), which the speaking hook reads to voice an alert once.
#
# Bounded: (services + 1) x contour.timeout, polled serially — at the shipped default, 10 s for
# one service, under a minute for ten, against a five-minute cadence.
#
# Usage: contour-poll.sh [--json] [--status PATH]
# Exit:  0 polled, no alert · 1 polled, an alert is active (the status file says which)
#        64 called wrong, or a config that names no usable service
set -u
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required" >&2; exit 64; }
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd) || exit 64
[ -f "$DIR/contour_poll.py" ] || { echo "FAIL: contour_poll.py is missing beside $0" >&2; exit 64; }
exec python3 "$DIR/contour_poll.py" "$@"
