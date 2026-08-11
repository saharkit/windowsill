#!/bin/bash
# Post-training finisher for experiment "scheherazade".
#
# WHY THIS EXISTS: Applio 3.6.4 rvc/train/train.py ends a completed run with
# os._exit(2333333) -> shell exit status 149. core.py run_train_script treats any
# non-zero rc as failure and therefore SKIPS its own run_index_script(). So the
# .index file is never produced by "core.py train". This watcher waits for the
# trainer to exit and builds the index itself.
LOG=$HOME/voice/rvc/train.log
while pgrep -f "rvc/train/train.py" > /dev/null; do sleep 60; done
sleep 5
cd $HOME/voice/rvc/Applio || exit 1
if grep -aq "Training has been successfully completed" "$LOG"; then
  echo "=== INDEX_BUILD start $(date -Is) ==="
  $HOME/voice/rvc/venv/bin/python core.py index --model_name scheherazade --index_algorithm Auto
  echo "=== INDEX_DONE rc=$? at $(date -Is) ==="
  ls -la $HOME/voice/rvc/Applio/logs/scheherazade/*.index 2>/dev/null
else
  echo "=== NO_INDEX: trainer exited without the completion marker at $(date -Is) ==="
  tail -5 "$LOG"
fi
