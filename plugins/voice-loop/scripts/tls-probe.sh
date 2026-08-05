#!/usr/bin/env bash
# voice-loop — TLS probe LAUNCHER. The probe's logic lives in tls-probe.py (python3, stdlib only);
# this wrapper is the stable entry point /voice-setup and the docs name, so an update to the
# implementation never changes the command anyone has written down.
#
# It answers one question about the interpreter that will do the talking: does an https certificate
# verify from here? On macOS with python.org-installer Python the answer is no until
# "/Applications/Python 3.x/Install Certificates.command" has been run once — and the probe says so
# by name, or runs it with --fix.
#
# Usage: tls-probe.sh [--url URL] [--timeout SECONDS] [--fix] [--json]
# Exit:  0 verified · 1 certificate verification failed · 2 host unreachable (TLS not proven)
#        64 called wrong (unknown flag, missing value, non-https URL) — never a diagnosis
#
# IMPORTANT: this probes the python3 on PATH, which is the one the hooks run under. To probe a
# different interpreter (a venv, a specific framework version), call it directly:
#   ~/.local/share/voice-loop/venv/bin/python .../scripts/tls-probe.py --fix
set -u
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required" >&2; exit 1; }
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd) || exit 1
[ -f "$DIR/tls-probe.py" ] || { echo "FAIL: tls-probe.py is missing beside $0" >&2; exit 1; }
exec python3 "$DIR/tls-probe.py" "$@"
