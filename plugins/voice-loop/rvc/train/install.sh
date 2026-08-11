#!/bin/bash
set -x
export PATH="$HOME/.local/bin:$PATH"
export UV_HTTP_TIMEOUT=600
V=$HOME/voice/rvc/venv/bin/python
echo "=== STEP1 torch cu128 ==="
uv pip install --python "$V" --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0 torchaudio==2.11.0
echo "=== STEP2 requirements ==="
uv pip install --python "$V" -r $HOME/voice/rvc/Applio/requirements.txt --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match
echo "=== STEP3 cuda check ==="
"$V" -c "import torch,torchaudio;print(\"torch\",torch.__version__,\"ta\",torchaudio.__version__,\"cuda\",torch.cuda.is_available(),torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"NONE\")"
echo "=== INSTALL_DONE rc=$? ==="
