# voice-loop speech server — CPU-only image.
#
#   docker build -t voice-loop-server plugins/voice-loop/server/
#   docker run --rm -p 127.0.0.1:8355:8355 voice-loop-server
#
# Models are baked in at build time, so the container starts warm and needs no network at runtime.
# For GPU, start from an nvidia/cuda base and install the matching torch wheel instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    VOICE_LOOP_HOST=0.0.0.0 \
    VOICE_LOOP_PORT=8355 \
    VOICE_LOOP_DEVICE=cpu \
    VOICE_LOOP_STT_MODEL=small

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -r requirements.txt

COPY voice_server.py .

# Pre-download the models (whisper small + Silero) so the first request is fast and offline-safe.
ARG PREFETCH_MODELS=1
RUN if [ "$PREFETCH_MODELS" = "1" ]; then python -c "\
from faster_whisper import WhisperModel; import torch, os;\
WhisperModel(os.environ['VOICE_LOOP_STT_MODEL'], device='cpu', compute_type='int8');\
torch.hub.load('snakers4/silero-models', 'silero_tts', language='ru', speaker='v4_ru', trust_repo=True)"; fi

EXPOSE 8355
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD curl -fsS "http://127.0.0.1:${VOICE_LOOP_PORT}/health" || exit 1

CMD ["python", "voice_server.py"]
