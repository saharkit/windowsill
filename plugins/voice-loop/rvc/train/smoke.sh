#!/bin/bash
# Inference smoke test for the "scheherazade" RVC model.
# usage: smoke.sh [INPUT_WAV] [OUTPUT_WAV]
# Defaults to a slice of the training corpus as input (self-contained; it proves
# the CLI + weights + index load and produce non-silent audio of the right length).
# DO NOT run this while training is running - the GPU has < 500 MiB spare then.
set -e
DIR=$HOME/voice/rvc/Applio/logs/scheherazade
IN=${1:-$(ls $DIR/sliced_audios/*.wav | head -1)}
OUT=${2:-$HOME/voice/rvc/smoke_out.wav}
PTH=$(ls -t $DIR/scheherazade_*e_*s.pth 2>/dev/null | head -1)
IDX=$(ls -t $DIR/*.index 2>/dev/null | head -1)
echo "input:   $IN"
echo "weights: $PTH"
echo "index:   $IDX"
[ -n "$PTH" ] || { echo "SMOKE_FAIL: no weights found in $DIR"; exit 1; }
[ -n "$IDX" ] || { echo "SMOKE_FAIL: no .index found in $DIR"; exit 1; }
cd $HOME/voice/rvc/Applio
export PYTHONPATH=$HOME/voice/rvc/shims   # pedalboard shim: this CPU has no AVX2 (see RUNBOOK 8)
START=$(date +%s)
$HOME/voice/rvc/venv/bin/python core.py infer \
  --input_path "$IN" \
  --output_path "$OUT" \
  --pth_path "$PTH" \
  --index_path "$IDX" \
  --f0_method rmvpe \
  --pitch 0 \
  --index_rate 0.75 \
  --volume_envelope 1 \
  --protect 0.33
RC=$?
END=$(date +%s)
echo "=== infer rc=$RC wall_clock_s=$((END-START)) ==="
$HOME/voice/rvc/venv/bin/python - "$IN" "$OUT" <<"PY"
import sys, numpy as np, soundfile as sf, os
for label, p in (("IN", sys.argv[1]), ("OUT", sys.argv[2])):
    if not os.path.isfile(p):
        print(f"{label}: MISSING {p}"); continue
    a, sr = sf.read(p)
    a = np.asarray(a, dtype="float64")
    print(f"{label}: {p} dur={len(a)/sr:.2f}s sr={sr} peak={np.abs(a).max():.4f} rms={np.sqrt((a**2).mean()):.4f}")
PY
echo "=== SMOKE_DONE ==="
