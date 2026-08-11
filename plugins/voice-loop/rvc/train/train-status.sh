#!/bin/bash
# One-shot status for the "scheherazade" RVC training run.
# Prints exactly one of: RUNNING / FINISHED / DIED / UNKNOWN, plus evidence.
# Poll it with:  ssh <user>@<gpu-host> "~/voice/rvc/train-status.sh"
#
# COMPLETION DETECTION -- read this before changing it.
# train.py ends a completed run with os._exit(2333333), which does NOT flush
# stdio. Because stdout is redirected to a file it is block-buffered, so the
# final epoch's output -- including the line "Training has been successfully
# completed with N epoch..." -- is DISCARDED at exit. A perfectly successful run
# therefore leaves NO completion marker in train.log. Keying on that marker alone
# reported a finished 70-epoch run as DIED (2026-08-02).
# The reliable evidence is ON DISK: the final weights file for the launched epoch
# target. We take the target from the TRAIN_LAUNCH line this script's launcher writes.
#
# THE TARGET HAS NO SECOND SOURCE: a hand-launched run (RUNBOOK §3's raw
# `core.py train` command) writes no TRAIN_LAUNCH line, and nothing else a run
# produces records the epoch target -- every weights file, final or periodic,
# is scheherazade_<epoch>e_<epoch*461>s.pth, so the newest file on disk cannot
# say whether it is the last one. In that case this script prints UNKNOWN
# rather than guessing: the marker is sound only in the POSITIVE direction
# (it is printed only on success), so marker-present still means FINISHED, but
# marker-absent with an unknown target must NOT report DIED -- that is exactly
# the false alarm the on-disk check was built to kill, arriving by the back door.
LOG=$HOME/voice/rvc/train.log
DIR=$HOME/voice/rvc/Applio/logs/scheherazade

LASTEP=$(tr "\r" "\n" < "$LOG" 2>/dev/null | grep -a "epoch=" | tail -1)
TARGET=$(grep -a "TRAIN_LAUNCH" "$LOG" 2>/dev/null | tail -1 | sed -n "s/.*epochs=\([0-9]*\).*/\1/p")
FINAL_W=""
[ -n "$TARGET" ] && FINAL_W=$(ls "$DIR"/scheherazade_${TARGET}e_*s.pth 2>/dev/null | tail -1)

if pgrep -f "rvc/train/train.py" > /dev/null; then
  PROG=$(tr "\r" "\n" < "$LOG" | grep -a "%|" | tail -1 | tr -s " ")
  VRAM=$(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader | tr "\n" " ")
  echo "RUNNING | ${LASTEP:-<no epoch finished yet>} | in-epoch:${PROG} | vram_used/free: ${VRAM}"
elif [ -n "$FINAL_W" ] || grep -aq "Training has been successfully completed" "$LOG" 2>/dev/null; then
  IDX=$(ls "$DIR"/*.index 2>/dev/null | tail -1)
  ALLW=$(ls "$DIR"/scheherazade_*e_*s.pth 2>/dev/null | wc -l)
  HOW="completion marker present"
  [ -n "$FINAL_W" ] && HOW="final weights for the ${TARGET}-epoch target are on disk"
  echo "FINISHED (${HOW}) | target=${TARGET:-?}ep | final=${FINAL_W:-MISSING} | ${ALLW} weights file(s) total | index: ${IDX:-MISSING} | last logged: ${LASTEP:-none}"
elif [ -z "$TARGET" ]; then
  echo "UNKNOWN | trainer not running, but the epoch target is unknowable: no TRAIN_LAUNCH line in $LOG (a hand-launched run per RUNBOOK §3 writes none) and no completion marker (os._exit may have discarded it). Refusing to guess DIED -- check the on-disk weights by hand: ls -t $DIR/scheherazade_*e_*s.pth | last logged: ${LASTEP:-none}"
else
  ERR=$(tr "\r" "\n" < "$LOG" 2>/dev/null | grep -aiE "error|Traceback|out of memory" | tail -2 | tr "\n" " ")
  echo "DIED | target=${TARGET}ep | last: ${LASTEP:-<no epoch finished>} | reason: ${ERR:-<no error line; check ~/voice/rvc/train.log>}"
fi
