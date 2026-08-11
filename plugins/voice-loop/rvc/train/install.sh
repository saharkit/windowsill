#!/bin/bash
set -x
export PATH="$HOME/.local/bin:$PATH"
export UV_HTTP_TIMEOUT=600
V=$HOME/voice/rvc/venv/bin/python
# Each step's real status is captured and later steps are skipped once one has
# failed: a failed STEP1/STEP2 followed by a lucky cuda check used to report
# INSTALL_DONE rc=0, because $? here reflected only the LAST command.
RC=0
echo "=== STEP1 torch cu128 ==="
uv pip install --python "$V" --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0 torchaudio==2.11.0 || RC=$?
if [ "$RC" -eq 0 ]; then
  echo "=== STEP2 requirements ==="
  uv pip install --python "$V" -r "$HOME/voice/rvc/Applio/requirements.txt" --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match || RC=$?
fi
if [ "$RC" -eq 0 ]; then
  echo "=== STEP3 cuda check ==="
  "$V" -c "import torch,torchaudio;print(\"torch\",torch.__version__,\"ta\",torchaudio.__version__,\"cuda\",torch.cuda.is_available(),torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"NONE\")" || RC=$?
fi
echo "=== INSTALL_DONE rc=$RC ==="
exit "$RC"
