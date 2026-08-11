#!/bin/bash
# Inference smoke test for the "scheherazade" RVC model.
# usage: smoke.sh [INPUT_WAV] [OUTPUT_WAV]
# Defaults to a slice of the training corpus as input (self-contained; it proves
# the CLI + weights + index load and produce non-silent audio of the right length).
# DO NOT run this while training is running - the GPU has < 500 MiB spare then.
#
# VERIFICATION CONTRACT: this script is the unattended safety net (auto-smoke.sh
# runs it with nobody watching), so it must not be able to print SMOKE_DONE on a
# path it did not check. It prints SMOKE_FAIL and exits non-zero when:
#   - weights or index are missing,
#   - core.py infer exits non-zero (previously set -e killed the script BEFORE
#     the rc= report and the analysis, so the failure vanished from the log),
#   - the output wav is missing, shorter than 0.1 s, or quieter than rms 0.005
#     (RUNBOOK §8's silence rule — previously printed but NEVER asserted, so a
#     silent conversion "passed"),
#   - the output length differs from the input by more than 0.5 s (conversion
#     preserves length; healthy runs match to within samples, so this cannot
#     fire on one).
# SMOKE_DONE is printed only after every one of those checks has passed.
set -e
DIR=$HOME/voice/rvc/Applio/logs/scheherazade
IN=${1:-$(ls "$DIR"/sliced_audios/*.wav | head -1)}
OUT=${2:-$HOME/voice/rvc/smoke_out.wav}
PTH=$(ls -t "$DIR"/scheherazade_*e_*s.pth 2>/dev/null | head -1)
IDX=$(ls -t "$DIR"/*.index 2>/dev/null | head -1)
echo "input:   $IN"
echo "weights: $PTH"
echo "index:   $IDX"
[ -n "$PTH" ] || { echo "SMOKE_FAIL: no weights found in $DIR"; exit 1; }
[ -n "$IDX" ] || { echo "SMOKE_FAIL: no .index found in $DIR"; exit 1; }
cd "$HOME/voice/rvc/Applio"
export PYTHONPATH="$HOME/voice/rvc/shims"   # pedalboard shim: this CPU has no AVX2 (see RUNBOOK 8)
START=$(date +%s)
RC=0
"$HOME/voice/rvc/venv/bin/python" core.py infer \
  --input_path "$IN" \
  --output_path "$OUT" \
  --pth_path "$PTH" \
  --index_path "$IDX" \
  --f0_method rmvpe \
  --pitch 0 \
  --index_rate 0.75 \
  --volume_envelope 1 \
  --protect 0.33 || RC=$?
END=$(date +%s)
echo "=== infer rc=$RC wall_clock_s=$((END-START)) ==="
if [ "$RC" -ne 0 ]; then
  echo "=== SMOKE_FAIL: core.py infer exited rc=$RC ==="
  exit "$RC"
fi
"$HOME/voice/rvc/venv/bin/python" - "$IN" "$OUT" <<"PY"
import sys, numpy as np, soundfile as sf, os
fail = []
dur = {}
for label, p in (("IN", sys.argv[1]), ("OUT", sys.argv[2])):
    if not os.path.isfile(p):
        fail.append(f"{label}: MISSING {p}")
        continue
    a, sr = sf.read(p)
    a = np.asarray(a, dtype="float64")
    rms = float(np.sqrt((a**2).mean()))
    dur[label] = len(a) / sr
    print(f"{label}: {p} dur={dur[label]:.2f}s sr={sr} peak={np.abs(a).max():.4f} rms={rms:.4f}")
    if label == "OUT":
        if dur[label] < 0.1:
            fail.append(f"OUT is silence-grade short: dur={dur[label]:.2f}s < 0.1s (RUNBOOK §8 silence rule)")
        if rms < 0.005:
            fail.append(f"OUT is silent: rms={rms:.4f} < 0.005 (RUNBOOK §8 silence rule)")
if "IN" in dur and "OUT" in dur and abs(dur["IN"] - dur["OUT"]) > 0.5:
    fail.append(f"duration mismatch: IN={dur['IN']:.2f}s vs OUT={dur['OUT']:.2f}s (conversion must preserve length)")
if fail:
    for f in fail:
        print(f"SMOKE_FAIL: {f}")
    sys.exit(1)
PY
echo "=== SMOKE_DONE ==="
