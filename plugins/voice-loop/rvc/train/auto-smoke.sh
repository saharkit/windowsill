#!/bin/bash
# Waits for the finisher to build the index, then runs the inference smoke test
# once, unattended, while the GPU is free (training over, xtts not yet restarted).
#
# RUN SCOPING: finish.log is APPEND-only across runs, so a previous run's
# INDEX_DONE used to satisfy this gate instantly and fire the smoke MID-TRAINING
# on a re-armed resume run (RUNBOOK §6b) — against §8's VRAM rule. This script
# therefore reads only finisher verdicts logged AFTER it was armed (a line-count
# watermark into finish.log), and as a hard floor it refuses to run the smoke
# while a trainer process exists, regardless of what any log says. The floor is
# also what covers the case where finish.sh was never re-armed and the 8 h wait
# expires while a long resume is still running.
#
# INDEX DETECTION: the finisher logs INDEX_DONE/INDEX_FAILED/NO_INDEX/
# COMPLETION_UNKNOWN. As a belt-and-suspenders fallback, this script also checks
# whether the .index file exists on disk directly — the index is built from the
# preprocess/extract features, never from the trained weights (RUNBOOK §5), so
# an on-disk index is valid regardless of what the current run did. The trainer
# guard below is what makes that fallback safe.
FIN=$HOME/voice/rvc/finish.log
IDX=$HOME/voice/rvc/Applio/logs/scheherazade/scheherazade.index
# Watermark: only finisher verdicts below this line belong to THIS run.
if [ -f "$FIN" ]; then ARMED_AT=$(wc -l < "$FIN"); else ARMED_AT=0; fi
verdicts_this_run() { tail -n +"$((ARMED_AT + 1))" "$FIN" 2>/dev/null; }
for _ in $(seq 1 480); do
  if verdicts_this_run | grep -aq "INDEX_DONE\|INDEX_FAILED\|NO_INDEX\|COMPLETION_UNKNOWN"; then break; fi
  sleep 60
done
if ! verdicts_this_run | grep -aq "INDEX_DONE"; then
  if [ -f "$IDX" ]; then
    echo "=== AUTO_SMOKE: no INDEX_DONE this run, but index exists on disk — proceeding ==="
  else
    echo "=== AUTO_SMOKE_SKIPPED: no index built this run (no INDEX_DONE below the arming watermark, and $IDX missing) by $(date -Is) ==="; exit 1
  fi
fi
if pgrep -f "rvc/train/train.py" > /dev/null; then
  echo "=== AUTO_SMOKE_SKIPPED: a trainer is RUNNING — the smoke needs ~1.5 GiB VRAM and training leaves < 500 MiB free (RUNBOOK §8) — by $(date -Is) ==="; exit 1
fi
sleep 10
echo "=== AUTO_SMOKE start $(date -Is) ==="
RC=0
"$HOME/voice/rvc/smoke.sh" || RC=$?
echo "=== AUTO_SMOKE end rc=$RC at $(date -Is) ==="
exit "$RC"
