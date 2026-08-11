#!/bin/bash
set -x
cd "$HOME"/voice/rvc/Applio || exit
"$HOME"/voice/rvc/venv/bin/python core.py prerequisites --pretraineds_hifigan True --models True --exe False
echo "=== PREREQ_DONE rc=$? ==="
du -sh rvc/models/* 2>/dev/null
find rvc/models -type f \( -name "*.pth" -o -name "*.pt" -o -name "*.bin" -o -name "*.json" \) -printf "%p %s\n"
