#!/bin/bash
# Waits for the finisher to build the index, then runs the inference smoke test
# once, unattended, while the GPU is free (training over, xtts not yet restarted).
FIN=$HOME/voice/rvc/finish.log
for i in $(seq 1 480); do
  if grep -aq "INDEX_DONE\|NO_INDEX" "$FIN" 2>/dev/null; then break; fi
  sleep 60
done
if ! grep -aq "INDEX_DONE" "$FIN" 2>/dev/null; then
  echo "=== AUTO_SMOKE_SKIPPED: no index built by $(date -Is) ==="; exit 1
fi
sleep 10
echo "=== AUTO_SMOKE start $(date -Is) ==="
$HOME/voice/rvc/smoke.sh
echo "=== AUTO_SMOKE end rc=$? at $(date -Is) ==="
