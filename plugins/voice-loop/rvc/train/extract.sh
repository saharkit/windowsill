#!/bin/bash
set -x
cd $HOME/voice/rvc/Applio
$HOME/voice/rvc/venv/bin/python core.py extract \
  --model_name scheherazade \
  --f0_method rmvpe \
  --cpu_cores 6 \
  --gpu 0 \
  --sample_rate 40000 \
  --embedder_model contentvec \
  --include_mutes 2
echo "=== EXTRACT_DONE rc=$? ==="
for d in f0 f0_voiced extracted sliced_audios sliced_audios_16k; do echo -n "$d: "; ls $HOME/voice/rvc/Applio/logs/scheherazade/$d 2>/dev/null | wc -l; done
wc -l $HOME/voice/rvc/Applio/logs/scheherazade/filelist.txt 2>/dev/null
head -2 $HOME/voice/rvc/Applio/logs/scheherazade/filelist.txt 2>/dev/null
