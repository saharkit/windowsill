#!/bin/bash
# Waits for the finisher to build the index, then runs the inference smoke test
# once, unattended, while the GPU is free (training over, xtts not yet restarted).
#
# INDEX DETECTION: the finisher logs INDEX_DONE/NO_INDEX, but finish.sh (like
# train-status.sh) may have been fixed to check on-disk weights. As a belt-and-
# suspenders fallback, this script also checks whether the .index file exists on
# disk directly — if it does, proceed regardless of the finisher's log.
FIN=$HOME/voice/rvc/finish.log
IDX=$HOME/voice/rvc/Applio/logs/scheherazade/scheherazade.index
for i in $(seq 1 480); do
  if grep -aq "INDEX_DONE\|NO_INDEX" "$FIN" 2>/dev/null; then break; fi
  sleep 60
done
if ! grep -aq "INDEX_DONE" "$FIN" 2>/dev/null; then
  if [ -f "$IDX" ]; then
    echo "=== AUTO_SMOKE: finisher did not log INDEX_DONE, but index exists on disk — proceeding ==="
  else
    echo "=== AUTO_SMOKE_SKIPPED: no index built (finisher log has no INDEX_DONE, and $IDX missing) by $(date -Is) ==="; exit 1
  fi
fi
sleep 10
echo "=== AUTO_SMOKE start $(date -Is) ==="
$HOME/voice/rvc/smoke.sh
echo "=== AUTO_SMOKE end rc=$? at $(date -Is) ==="
