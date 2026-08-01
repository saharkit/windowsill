# voice-loop speech server

One small FastAPI app with two endpoints:

| endpoint | what it does |
|---|---|
| `POST /stt` | multipart `audio=@file.wav`, query `?language=ru` → `{"text": ..., "language": ..., "duration": ...}` |
| `POST /tts` | JSON `{"text": ..., "language": "ru", "speaker": "baya"}` → `audio/wav` |
| `GET /health` | device, models, what is loaded |

STT is [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (multilingual). TTS is
[Silero](https://github.com/snakers4/silero-models), with automatic stress marking for Russian
([RUAccent](https://github.com/Den4ikAI/ruaccent)) and Ukrainian
([ukrainian-word-stress](https://github.com/lang-uk/ukrainian-word-stress)) — that stress pass is the
difference between a voice that reads and a voice that stumbles.

## Requirements

**Python >= 3.10** (the server checks at startup and exits with a clear message on anything older).
The unit tests run on 3.10, 3.11, 3.12 and 3.13 in CI; the loopback lanes run 3.12.

## Run it bare

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch   # CPU-only torch, much smaller
pip install -r requirements.txt
pip install "ukrainian-word-stress>=1.0"                             # only if you want Ukrainian

python voice_server.py            # http://127.0.0.1:8355
curl -s http://127.0.0.1:8355/health
```

Models download on first use (roughly 0.5–1.5 GB depending on the whisper size) into `HF_HOME` /
`TORCH_HOME`. Nothing is preloaded at import, so the process starts instantly and warms lazily.

## Run it in Docker (CPU)

```sh
docker build -t voice-loop-server .
docker run --rm -p 127.0.0.1:8355:8355 voice-loop-server
```

The image bakes the models in at build time, so the container needs no network at runtime. Build with
`--build-arg PREFETCH_MODELS=0` for a small image that downloads on first request instead.

## GPU

Install the CUDA build of torch (see pytorch.org) instead of the CPU wheel, then:

```sh
VOICE_LOOP_DEVICE=cuda VOICE_LOOP_STT_MODEL=large-v3-turbo python voice_server.py
```

Only STT moves to the GPU; Silero stays on CPU, where it is already faster than real time — that keeps
the GPU free for recognition. `compute_type` defaults to `float16` on CUDA and `int8` on CPU.

## Configuration (all environment variables)

| variable | default | meaning |
|---|---|---|
| `VOICE_LOOP_HOST` | `127.0.0.1` | bind address. **Loopback by default** — see below |
| `VOICE_LOOP_PORT` | `8355` | port |
| `VOICE_LOOP_DEVICE` | `auto` | `auto` / `cuda` / `cpu` |
| `VOICE_LOOP_LANGUAGE` | `ru` | default language when a request does not name one |
| `VOICE_LOOP_STT_MODEL` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3-turbo` … |
| `VOICE_LOOP_COMPUTE_TYPE` | `auto` | faster-whisper compute type |
| `VOICE_LOOP_STT_HINT` | — | lexicon hint: a comma-separated list of names/jargon you want recognized |
| `VOICE_LOOP_TTS_MODEL` | per language | override the Silero model for the default language |
| `VOICE_LOOP_TTS_SPEAKER` | per language | override the default speaker |
| `VOICE_LOOP_STRESS_FILE` | `~/.config/voice-loop/stress.json` | your stress overrides |
| `VOICE_LOOP_ACCENT` | `1` | set `0` to skip automatic accentuation |

### Languages

| code | synthesis (Silero) | default speaker | automatic stress |
|---|---|---|---|
| `en` | `v3_en` | `en_0` | not needed |
| `ru` | `v4_ru` | `baya` | RUAccent |
| `uk` | `v4_ua` | `mykyta` | ukrainian-word-stress |
| `de` | `v3_de` | `eva_k` | — |
| `es` | `v3_es` | `es_0` | — |
| `fr` | `v3_fr` | `fr_0` | — |

Recognition works for every language whisper supports. A `/tts` request for a language not in the
table returns `400` with the supported list — use a cloud TTS backend for those, or add the model to
`SILERO_VOICES` in `voice_server.py`.

### Stress overrides (`stress.json`)

Proper names and homographs the voice gets wrong. Either shape works:

```json
{ "\\bAcme\\b": "+Acme", "\\b([tT])(esto)\\b": "\\1+esto" }
```

The `+` goes immediately **before** the stressed vowel (Silero's notation). Your rules are applied
**before** the automatic accentuator, and the accentuator is deliberately never shown the tokens you
marked — otherwise it re-stresses them from its own dictionary and silently undoes your override. You
can also just type a combining acute in the text you send (`робо́та`); it is converted for you.

## Tests

```sh
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r ../tests/requirements.txt
cd .. && pytest --cov=voice_server --cov-fail-under=100
```

No models, no network, no audio hardware: the recognizer, the voices and the accentuators are loaded
through seams the tests replace with fakes. Coverage is gated at 100% — see
[`../plugins/voice-loop/TESTING.md`](../plugins/voice-loop/TESTING.md) for what that claim covers.

## Reaching it from another machine

The server has **no authentication**. Keep the default loopback bind and tunnel:

```sh
ssh -N -L 8355:127.0.0.1:8355 user@gpu-host
# endpoint on the client stays http://127.0.0.1:8355
```

If you really want it on your network, set `VOICE_LOOP_HOST=0.0.0.0`, keep it behind a firewall you
control, and be aware that anyone who can reach the port can transcribe and synthesize on your
hardware.
