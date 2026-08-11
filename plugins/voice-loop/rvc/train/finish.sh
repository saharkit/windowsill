#!/bin/bash
# Post-training finisher for experiment "scheherazade".
#
# WHY THIS EXISTS: Applio 3.6.4 rvc/train/train.py ends a completed run with
# os._exit(2333333) -> shell exit status 149. core.py run_train_script treats any
# non-zero rc as failure and therefore SKIPS its own run_index_script(). So the
# .index file is never produced by "core.py train". This watcher waits for the
# trainer to exit and builds the index itself.
#
# COMPLETION DETECTION: os._exit() does NOT flush stdio, so the log marker
# "Training has been successfully completed" is often DISCARDED. As with
# train-status.sh, the reliable evidence is ON DISK: the final weights file for
# the launched epoch target. The log marker is a fallback — and only in the
# POSITIVE direction (it is printed solely on success). A hand-launched run
# (RUNBOOK §3's raw `core.py train`) writes no TRAIN_LAUNCH line, and no other
# artifact records the epoch target — every weights file, final or periodic,
# is scheherazade_<epoch>e_<epoch*461>s.pth — so with neither target nor marker
# this script CANNOT know whether the run finished. It then logs
# COMPLETION_UNKNOWN and exits 1 instead of logging NO_INDEX, which asserted a
# failure it had not established (the same false alarm the on-disk check was
# built to kill, arriving by the back door).
#
# Verdicts logged to finish.log: INDEX_BUILD / INDEX_DONE / INDEX_FAILED /
# NO_INDEX / COMPLETION_UNKNOWN. INDEX_DONE is printed ONLY when the index
# command exited 0 AND left a freshly-written .index on disk — auto-smoke.sh
# gates on that token, so it must never appear on an unverified path.
LOG=$HOME/voice/rvc/train.log
DIR=$HOME/voice/rvc/Applio/logs/scheherazade
while pgrep -f "rvc/train/train.py" > /dev/null; do sleep 60; done
sleep 5
cd "$HOME/voice/rvc/Applio" || exit 1

TARGET=$(grep -a "TRAIN_LAUNCH" "$LOG" 2>/dev/null | tail -1 | sed -n "s/.*epochs=\([0-9]*\).*/\1/p")
FINAL_W=""
# shellcheck disable=SC2012
[ -n "$TARGET" ] && FINAL_W=$(ls "$DIR"/scheherazade_"${TARGET}"e_*s.pth 2>/dev/null | tail -1)

if [ -n "$FINAL_W" ] || grep -aq "Training has been successfully completed" "$LOG"; then
  HOW="completion marker present"
  [ -n "$FINAL_W" ] && HOW="final weights on disk: $FINAL_W"
  echo "=== INDEX_BUILD start $(date -Is) ($HOW) ==="
  REF=$(mktemp)
  RC=0
  "$HOME/voice/rvc/venv/bin/python" core.py index --model_name scheherazade --index_algorithm Auto || RC=$?
  NEWIDX=$(find "$DIR" -maxdepth 1 -name '*.index' -newer "$REF" | head -1)
  rm -f "$REF"
  if [ "$RC" -eq 0 ] && [ -n "$NEWIDX" ]; then
    echo "=== INDEX_DONE rc=0 built=$NEWIDX at $(date -Is) ==="
    ls -la "$DIR"/*.index 2>/dev/null
  else
    echo "=== INDEX_FAILED rc=$RC fresh_index=${NEWIDX:-NONE} at $(date -Is) — the build did not verify; auto-smoke will not fire on this ==="
    exit 1
  fi
elif [ -z "$TARGET" ]; then
  echo "=== COMPLETION_UNKNOWN: trainer exited, but no TRAIN_LAUNCH line in $LOG (hand-launched run per RUNBOOK §3 writes none) and no completion marker — cannot confirm the run finished, NOT building the index on a guess at $(date -Is) ==="
  echo "    verify by hand: ls -t $DIR/scheherazade_*e_*s.pth — then, if the final weights are there: $HOME/voice/rvc/venv/bin/python core.py index --model_name scheherazade --index_algorithm Auto"
  exit 1
else
  echo "=== NO_INDEX: no final weights for target=${TARGET}ep and no completion marker at $(date -Is) ==="
  tail -5 "$LOG"
fi
