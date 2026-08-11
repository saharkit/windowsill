#!/bin/bash
# Applio training launcher for experiment "scheherazade" (RVC v2, 40k, HiFi-GAN).
# usage: train.sh [BATCH] [EPOCHS] [CLEANUP] [SAVE_EVERY] [CHECKPOINTING]
BATCH=${1:-2}
EPOCHS=${2:-70}
CLEANUP=${3:-False}
SAVE_EVERY=${4:-10}
CKPT=${5:-True}
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
cd $HOME/voice/rvc/Applio
echo "=== TRAIN_LAUNCH batch=$BATCH epochs=$EPOCHS save_every=$SAVE_EVERY checkpointing=$CKPT cleanup=$CLEANUP alloc_conf=$PYTORCH_CUDA_ALLOC_CONF at $(date -Is) ==="
$HOME/voice/rvc/venv/bin/python core.py train \
  --model_name scheherazade \
  --vocoder HiFi-GAN \
  --sample_rate 40000 \
  --batch_size "$BATCH" \
  --total_epoch "$EPOCHS" \
  --save_every_epoch "$SAVE_EVERY" \
  --save_only_latest False \
  --save_every_weights True \
  --gpu 0 \
  --pretrained True \
  --custom_pretrained False \
  --cache_data_in_gpu False \
  --checkpointing "$CKPT" \
  --cleanup "$CLEANUP" \
  --index_algorithm Auto
echo "=== TRAIN_EXIT rc=$? at $(date -Is) ==="
