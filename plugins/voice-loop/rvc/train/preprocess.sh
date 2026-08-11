#!/bin/bash
set -x
cd "$HOME"/voice/rvc/Applio || exit
"$HOME"/voice/rvc/venv/bin/python core.py preprocess \
  --model_name scheherazade \
  --dataset_path "$HOME"/voice/corpus/eleven \
  --sample_rate 40000 \
  --cpu_cores 6 \
  --cut_preprocess Automatic \
  --process_effects True \
  --noise_reduction False \
  --normalization_mode none
echo "=== PREPROCESS_DONE rc=$? ==="
# shellcheck disable=SC2012
ls "$HOME"/voice/rvc/Applio/logs/scheherazade/sliced_audios | wc -l
# shellcheck disable=SC2012
ls "$HOME"/voice/rvc/Applio/logs/scheherazade/sliced_audios_16k | wc -l
du -sh "$HOME"/voice/rvc/Applio/logs/scheherazade
cat "$HOME"/voice/rvc/Applio/logs/scheherazade/model_info.json 2>/dev/null
