#!/bin/bash
# Stop the Applio training run for experiment "scheherazade".
# Patterns live INSIDE this file on purpose: running pkill with these patterns
# directly in an ssh command line makes the remote shell match (and kill) itself.
pkill -f "core.py train"
pkill -f "rvc/train/train.py"
sleep 6
pkill -9 -f "rvc/train/train.py" 2>/dev/null
sleep 2
if pgrep -f "rvc/train/train.py" > /dev/null; then
  echo "TRAIN_STILL_RUNNING"
else
  echo "TRAIN_STOPPED"
fi
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader
