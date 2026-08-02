# voice-loop speech server

One small FastAPI app:

| endpoint | what it does |
|---|---|
| `POST /stt` | multipart `audio=@file.wav`, query `?language=ru` → `{"text": ..., "language": ..., "duration": ...}` |
| `POST /tts` | JSON `{"text": ..., "language": "ru", "speaker": "baya"}` → `audio/wav` (one blob) |
| `POST /tts/stream` | same JSON → `text/event-stream` of WAV segments as they are synthesized — see [Streaming synthesis](#streaming-synthesis-ttsstream) |
| `GET /health` | device, models, engine, what is loaded |

STT is [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (multilingual). TTS is
[Silero](https://github.com/snakers4/silero-models) by default, with automatic stress marking for
Russian ([RUAccent](https://github.com/Den4ikAI/ruaccent)) and Ukrainian
([ukrainian-word-stress](https://github.com/lang-uk/ukrainian-word-stress)) — that stress pass is the
difference between a voice that reads and a voice that stumbles. An optional second engine,
[XTTS-v2](https://huggingface.co/coqui/XTTS_v2) voice cloning, is one environment variable away — see
[XTTS engine](#xtts-engine-voice-cloning).

## Requirements

**Python >= 3.10** (the server checks at startup and exits with a clear message on anything older).
The unit tests run on 3.10, 3.11, 3.12 and 3.13 in CI; the loopback lanes run 3.12.

## Run it bare

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch   # CPU-only torch, much smaller
pip install -r requirements.txt
pip install "ukrainian-word-stress>=2.0"                             # only if you want Ukrainian

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
| `VOICE_LOOP_TTS_ENGINE` | `silero` | `silero` or `xtts` (XTTS-v2 voice cloning, optional dependency) |
| `VOICE_LOOP_TTS_MODEL` | per language | override the Silero model for the default language |
| `VOICE_LOOP_TTS_SPEAKER` | per language | override the default speaker |
| `VOICE_LOOP_XTTS_REFERENCE` | — | wav of the voice to clone — the `xtts` engine refuses requests without it |
| `VOICE_LOOP_XTTS_MODEL_DIR` | coqui's cache | load XTTS-v2 from a local directory instead of downloading |
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

### Hallucination blocklist (`stt_hallucinations.txt`)

Whisper on a near-silent clip sometimes invents a well-known junk transcript instead of returning
nothing — TV end-credits, «Спасибо за просмотр», "Thank you for watching". `/stt` drops a transcript
whose **full** normalized text equals (or extends, at a word boundary) an entry in
`server/stt_hallucinations.txt` — one pattern per line, `#` comments allowed; genuine speech that
merely contains a phrase is never touched. Extend the file freely; every drop is logged and counted
in `GET /health` → `stt_hallucinations_dropped`.

## XTTS engine (voice cloning)

`VOICE_LOOP_TTS_ENGINE=xtts` switches synthesis from Silero to
[XTTS-v2](https://huggingface.co/coqui/XTTS_v2) — zero-shot voice cloning from a short reference
recording, fully local. Silero **remains the default**; nothing changes unless you opt in.

```sh
pip install coqui-tts                      # optional dependency, NOT in requirements.txt

VOICE_LOOP_TTS_ENGINE=xtts \
VOICE_LOOP_XTTS_REFERENCE=~/voice/reference.wav \
COQUI_TOS_AGREED=1 python voice_server.py
```

**Licensing — read this before you opt in.** The server code stays MIT, but the XTTS-v2 **weights
are licensed under the [Coqui Public Model License](https://coqui.ai/cpml)** (personal /
non-commercial use). They are **never bundled with this repo**: [coqui-tts](https://pypi.org/project/coqui-tts/)
downloads them on **your** first request, after you accept the license (`COQUI_TOS_AGREED=1`, or
answer its interactive prompt). If your use is commercial, XTTS is not for you — stay on Silero.

What to know:

- **Reference wav** (`VOICE_LOOP_XTTS_REFERENCE`): a clean 6–30 second recording of the voice to
  clone. Required — a request on the `xtts` engine without it (or with the file missing) gets a
  clear `500`; the server still boots and `/stt` keeps working regardless.
- **Missing package**: if `coqui-tts` is not installed, an `xtts` request gets a `500` telling you
  to `pip install coqui-tts`. The import is lazy — nothing else pays for the dependency.
- **Hardware**: on an RTX-class GPU expect **~2–2.5 GB of VRAM**; if the model does not fit, the
  load falls back to CPU automatically (it works, just slower than real time). Output is 24 kHz.
- **Languages**: XTTS-v2 is multilingual on its own (`en ru de es fr it pt pl tr nl cs ar hu ko ja
  hi zh-cn` — note: no `uk`); `language` comes from the request as usual. The Silero stress
  pipeline (RUAccent, `stress.json`, `+` markers) is **skipped** — XTTS has its own prosody — and
  any `+` markers or combining acutes already in the text are stripped before synthesis.
- **Model dir** (`VOICE_LOOP_XTTS_MODEL_DIR`): point at a directory containing the downloaded model
  (`config.json` next to the weights) to load from disk instead of coqui's cache.

## Streaming synthesis (`/tts/stream`)

`POST /tts/stream` takes **exactly the same JSON body as `/tts`** but answers with audio as it is
synthesized, so playback can start after the first sentence chunk instead of after the whole text.
`/tts` is unchanged; clients detect the endpoint via `GET /health` → `"streaming": true`.

The response is `200` with `Content-Type: text/event-stream` (server-sent events). The framing is
deliberately strict so a stdlib line-by-line reader can parse it — every event is exactly:

```
event: <name>\n
data: <one line of JSON>\n
\n
```

Three event types, in this order:

| event | data | meaning |
|---|---|---|
| `chunk` | `{"index": 0, "audio": "<base64>"}` | one **complete, standalone WAV file** (own header, engine sample rate: 48 kHz Silero / 24 kHz XTTS). Decode base64, play, done. `index` counts from 0 in order. |
| `end` | `{"chunks": N}` | terminal success — N `chunk` events were sent |
| `error` | `{"error": "synthesis failed (<ExceptionClass>)", "chunks": N}` | terminal failure **mid-stream** (the `200` already left with the first bytes, so a late failure becomes the last event, never a 500). N chunks were already sent and are valid. The message is deliberately generic — the exception class name at most; the full detail stays in the server log. |

A stream always ends with exactly one `end` **or** one `error` event. Requests refused *before*
synthesis starts (empty text, unsupported language, misconfigured xtts) return plain JSON `400`/`500`
exactly like `/tts`. Chunk granularity is the sentence chunker for **both** engines (XTTS-v2's
internal `inference_stream` generator sits below coqui's public API, so it is not used).

Minimal stdlib consumer:

```python
import base64, json, urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:8355/tts/stream",
    data=json.dumps({"text": "Первое предложение. Второе."}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as response:
    event = None
    for raw in response:
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
            if event == "chunk":
                play_wav(base64.b64decode(data["audio"]))   # your player here
            elif event == "error":
                raise RuntimeError(data["error"])
```

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
