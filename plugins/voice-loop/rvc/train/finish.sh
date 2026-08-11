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
# the launched epoch target. The log marker is a fallback.
LOG=$HOME/voice/rvc/train.log
DIR=$HOME/voice/rvc/Applio/logs/scheherazade
while pgrep -f "rvc/train/train.py" > /dev/null; do sleep 60; done
sleep 5
cd $HOME/voice/rvc/Applio || exit 1

TARGET=$(grep -a "TRAIN_LAUNCH" "$LOG" 2>/dev/null | tail -1 | sed -n "s/.*epochs=\([0-9]*\).*/\1/p")
FINAL_W=""
[ -n "$TARGET" ] && FINAL_W=$(ls "$DIR"/scheherazade_${TARGET}e_*s.pth 2>/dev/null | tail -1)

if [ -n "$FINAL_W" ] || grep -aq "Training has been successfully completed" "$LOG"; then
  HOW="completion marker present"
  [ -n "$FINAL_W" ] && HOW="final weights on disk: $FINAL_W"
  echo "=== INDEX_BUILD start $(date -Is) ($HOW) ==="
  $HOME/voice/rvc/venv/bin/python core.py index --model_name scheherazade --index_algorithm Auto
  echo "=== INDEX_DONE rc=$? at $(date -Is) ==="
  ls -la $HOME/voice/rvc/Applio/logs/scheherazade/*.index 2>/dev/null
else
  echo "=== NO_INDEX: no final weights for target=${TARGET:-?}ep and no completion marker at $(date -Is) ==="
  tail -5 "$LOG"
fi
