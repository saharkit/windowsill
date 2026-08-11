#!/bin/bash
# Samples GPU memory every 5s so the save-epoch peak is captured, not guessed.
while true; do
  echo "$(date -Is) $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader | tr -d "\n") | $(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | tr "\n" ";")"
  sleep 5
done
