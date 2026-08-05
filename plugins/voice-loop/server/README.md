# voice-loop speech server

One small FastAPI app:

| endpoint | what it does |
|---|---|
| `POST /stt` | multipart `audio=@file.wav`, query `?language=ru` → `{"text": ..., "language": ..., "duration": ...}` |
| `POST /tts` | JSON `{"text": ..., "language": "ru", "speaker": "baya"}` → `audio/wav` (one blob), `X-Voice-Loop-Engine` naming the engine that spoke |
| `POST /tts/stream` | same JSON → `text/event-stream` of WAV segments as they are synthesized — see [Streaming synthesis](#streaming-synthesis-ttsstream) |
| `GET /health` | device, models, engine (and its fallback), what is loaded, per-device queue depth, server `version` |

STT is [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (multilingual). TTS is
[Silero](https://github.com/snakers4/silero-models) by default, with automatic stress marking for
Russian ([RUAccent](https://github.com/Den4ikAI/ruaccent)) and Ukrainian
([ukrainian-word-stress](https://github.com/lang-uk/ukrainian-word-stress)) — that stress pass is the
difference between a voice that reads and a voice that stumbles. An optional second engine,
[XTTS-v2](https://huggingface.co/coqui/XTTS_v2) voice cloning, is one environment variable away — see
[XTTS engine](#xtts-engine-voice-cloning). When an engine breaks, synthesis degrades to the other
one instead of failing — see [Engine fallback](#engine-fallback).

## Requirements

**Python >= 3.10** (the server checks at startup and exits with a clear message on anything older).
The unit tests run on 3.10, 3.11, 3.12 and 3.13 in CI; the loopback lanes run 3.12.

## Run it bare

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch   # CPU-only torch, much smaller
pip install -r requirements.txt

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

## Capacity

Every model call — an `/stt` transcription, a `/tts` blob, each `/tts/stream` chunk — takes a slot
from a bounded executor before it runs. **There is one executor per device, not one per server**,
because the GPU and the CPU are disjoint hardware: Silero synthesis is pinned to the CPU precisely so
it does not compete with recognition on the card, and a single global queue would hand that
separation straight back — one user dictating would block another user's playback for no physical
reason. So a call queues on the device it actually runs on, and the two queues never touch. Whisper
follows `VOICE_LOOP_DEVICE`, Silero is always on the CPU, XTTS goes wherever it managed to load
(a card too small for it drops it to the CPU, and its queue follows).

`VOICE_LOOP_MODEL_CONCURRENCY` sizes the queue of the device this server runs its models on. The
default is **1** on a GPU — the resident models already hold most of the VRAM, and concurrent calls
stack per-call activation memory into an OOM — and up to **2** on CPU (by core count), where the
models release the GIL in native code and a second slot buys real parallelism. The other queue is
the incidental one (Silero's CPU slot on a GPU box) and keeps its own default, so raising the
primary never quietly widens it. Excess requests queue.

`/health` reports both what is running and what is **queued behind it**:

```json
"model_concurrency": 1, "model_in_flight": 1, "model_waiting": 3,
"model_queues": {"gpu": {"limit": 1, "in_flight": 1, "waiting": 3},
                 "cpu": {"limit": 2, "in_flight": 0, "waiting": 0}}
```

`model_in_flight` alone cannot tell **busy** from **saturated** — at the limit it reads the same
either way. `model_waiting` is the number that says whether anybody is paying for the wait, and
`model_queues` says on which hardware.

Three caps keep any single request from monopolizing an executor:

- **`/tts` renders its whole blob in one executor hold**, so it carries the small cap
  (`VOICE_LOOP_MAX_TTS_TEXT_BLOB`, 3000 characters). Longer texts belong on `/tts/stream`, which
  takes and **releases** the executor per sentence chunk and keeps the big cap
  (`VOICE_LOOP_MAX_TTS_TEXT`, 20 000).
- **`/stt` uploads are byte-capped at 25 MB — but bytes are not time**: 25 MB of compressed audio
  can decode to hours of transcription. Uploads whose WAV header is cheaply parseable are
  additionally duration-capped at `VOICE_LOOP_MAX_STT_SECONDS` (600 s). Compressed codecs reveal
  their duration only by decoding — the very work the cap exists to avoid — so they pass through on
  the byte cap alone, honestly unmeasured.
- **`/stt` therefore also carries a wall clock**, `VOICE_LOOP_STT_TIMEOUT` (900 s), which needs no
  header and so bounds the holding time whatever the codec. Whisper decodes lazily, so the budget is
  checked as segments arrive and a transcription that outruns it is abandoned — `503`, with its slot
  handed straight back to whoever is queued. What can overshoot is one segment's decoding, not one
  file's. Set it to `0` to switch the bound off and let long uploads run as long as they need.

## Configuration (all environment variables)

| variable | default | meaning |
|---|---|---|
| `VOICE_LOOP_HOST` | `127.0.0.1` | bind address. **Loopback by default** — see below |
| `VOICE_LOOP_PORT` | `8355` | port |
| `VOICE_LOOP_DEVICE` | `auto` | `auto` / `cuda` / `cpu` |
| `VOICE_LOOP_LANGUAGE` | `en` | default language when a request does not name one |
| `VOICE_LOOP_STT_MODEL` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3-turbo` … |
| `VOICE_LOOP_COMPUTE_TYPE` | `auto` | faster-whisper compute type |
| `VOICE_LOOP_STT_HINT` | — | lexicon hint: a comma-separated list of names/jargon you want recognized |
| `VOICE_LOOP_TTS_ENGINE` | `silero` | `silero` or `xtts` (XTTS-v2 voice cloning, optional dependency) |
| `VOICE_LOOP_TTS_FALLBACK_ENGINE` | `silero` when the engine is `xtts`, else `none` | `silero` / `xtts` / `none` — engine a failed synthesis retries on, see [Engine fallback](#engine-fallback) |
| `VOICE_LOOP_TTS_MODEL` | per language | override the Silero model for the default language |
| `VOICE_LOOP_TTS_SPEAKER` | per language | override the default speaker |
| `VOICE_LOOP_XTTS_REFERENCE` | — | wav of the voice to clone — the `xtts` engine refuses requests without it |
| `VOICE_LOOP_XTTS_MODEL_DIR` | coqui's cache | load XTTS-v2 from a local directory instead of downloading |
| `VOICE_LOOP_STRESS_FILE` | `~/.config/voice-loop/stress.json` | your stress overrides |
| `VOICE_LOOP_HOOK_STAMP_FILE` | `~/.local/state/voice-loop/hook-last-fired` | the hook's heartbeat stamp — `/health` reports its age as `hook_last_fired_age_s`; see [the troubleshooting entry](../docs/troubleshooting.md#the-voice-stops-entirely-mid-session-but-everything-works-by-hand) |
| `VOICE_LOOP_ACCENT` | `1` | set `0` to skip automatic accentuation |
| `VOICE_LOOP_MAX_UPLOAD_BYTES` | `26214400` (25 MB) | `/stt` upload size cap — a larger clip gets `413` |
| `VOICE_LOOP_MAX_STT_SECONDS` | `600` | `/stt` duration cap when the WAV header is parseable — see [Capacity](#capacity) |
| `VOICE_LOOP_STT_TIMEOUT` | `900` | `/stt` wall-clock transcription budget, any codec; `0` disables it — see [Capacity](#capacity) |
| `VOICE_LOOP_MAX_TTS_TEXT` | `20000` | `/tts/stream` text length cap — longer text gets `400` |
| `VOICE_LOOP_MAX_TTS_TEXT_BLOB` | `3000` | `/tts` single-blob text cap — longer text gets `400` pointing at `/tts/stream` |
| `VOICE_LOOP_MODEL_CONCURRENCY` | `1` on a GPU, else up to `2` | concurrent model calls on the primary device — see [Capacity](#capacity) |

`GET /health` also reports a `version` field (`"0.4.1"`). It is for diagnostics and bug reports;
clients should detect features through the capability flags (`"streaming": true` and friends), not
by comparing version strings.

`GET /health` also reports the **hook's heartbeat**: `hook_last_fired` (ISO-8601 UTC) and
`hook_last_fired_age_s` (seconds since), read from the stamp the speaking hook rewrites on every
invocation. An age that keeps growing while the session continues means the harness has stopped
calling the hook — see
[the troubleshooting entry](../docs/troubleshooting.md#the-voice-stops-entirely-mid-session-but-everything-works-by-hand).
Both are `null` when the stamp is not readable here — which includes a server running on a
different machine than the client (the ssh-tunnel setup), since the stamp lives in the *client's*
state dir.

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
nothing — TV end-credits, «Спасибо за просмотр», "Thank you for watching". The patterns live in
`stt_hallucinations.txt` next to `voice_server.py`, one per line, `#` comments allowed; a pattern
fires where the normalized text equals it or extends it at a word boundary, so genuine speech that
merely contains a phrase mid-sentence is never touched. Extend the file freely.

A pattern removes one of two amounts:

| the match is | what goes | why |
|---|---|---|
| the **whole** transcript | all of it — `/stt` returns `""` | the clip was silence and the model filled it |
| the **last sentence** | that sentence only, the speech before it kept | a closing caption appended to a complete dictation on a silent tail |

The tail-strip needs a **sentence break** in front of the caption. Real speech that simply ends in a
blocklisted phrase («напиши ему спасибо за просмотр») keeps its last words — quietly losing them
would be a worse bug than the addition this removes.

Nothing goes invisibly. Every hit is logged at INFO with the exact text removed (the drop and the
strip say which they were), and counted in `GET /health`:

- `stt_hallucinations_dropped` — transcripts dropped whole;
- `stt_hallucination_tails_stripped` — closing captions peeled off real speech;
- `stt_hallucinations_by_pattern` — pattern → hits, the corpus of what *your* microphone
  hallucinates. What shows up here is what belongs in the file.

## XTTS engine (voice cloning)

`VOICE_LOOP_TTS_ENGINE=xtts` switches synthesis from Silero to
[XTTS-v2](https://huggingface.co/coqui/XTTS_v2) — zero-shot voice cloning from a short reference
recording, fully local. Silero **remains the default**; nothing changes unless you opt in.

**Install the pinned set, not `pip install coqui-tts`.** A bare install of coqui-tts (0.27.5) does
not work: it declares neither torch nor torchaudio yet imports torchaudio, an unresolved
`transformers` lands on 5.x — which removed `isin_mps_friendly` — and `torch>=2.9` pulls in
torchcodec, whose wheels want the CUDA libraries or a system ffmpeg. This is the set verified end to
end in a clean venv (and re-verified weekly by the `xtts-install-probe` workflow):

```sh
# CPU
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.8.0 torchaudio==2.8.0
pip install 'transformers<5' coqui-tts

# CUDA — same thing from the cu index matching your driver (see pytorch.org for the variant)
pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 torchaudio==2.8.0
pip install 'transformers<5' coqui-tts
```

Order matters: torch and torchaudio go in **first**, from the index you want them from, so the
second command finds them satisfied instead of resolving its own wheel. Then:

```sh
VOICE_LOOP_TTS_ENGINE=xtts \
VOICE_LOOP_XTTS_REFERENCE=~/voice/reference.wav \
COQUI_TOS_AGREED=1 python voice_server.py
```

The XTTS-v2 weights land in `~/.local/share/tts/` (coqui's own cache) no matter what `HF_HOME` or
`TORCH_HOME` say — those steer whisper and Silero, not this download; `VOICE_LOOP_XTTS_MODEL_DIR`
is what points the server somewhere else.

**Licensing — read this before you opt in.** The server code stays MIT, but the XTTS-v2 **weights
are licensed under the [Coqui Public Model License](https://coqui.ai/cpml)** (personal /
non-commercial use). They are **never bundled with this repo**: [coqui-tts](https://pypi.org/project/coqui-tts/)
downloads them on **your** first request, after you accept the license (`COQUI_TOS_AGREED=1`, or
answer its interactive prompt). If your use is commercial, XTTS is not for you — stay on Silero.

What to know:

- **Reference wav** (`VOICE_LOOP_XTTS_REFERENCE`): a clean 6–30 second recording of the voice to
  clone. Required — a request on the `xtts` engine without it (or with the file missing) gets a
  clear `500`; the server still boots and `/stt` keeps working regardless.
- **Import failures**: the import is lazy — nothing else pays for the dependency — and an `xtts`
  request whose import fails gets a `500` naming the **shape** of the failure: the exception class,
  plus the module that was not found when there is one (`xtts import failed: ModuleNotFoundError in
  dependency 'torchaudio'`). A genuinely absent coqui-tts says so and hands you the install hint; a
  coqui-tts that is installed but cannot import points at the pinned set above instead of at a
  reinstall that will not help. The exception's own **message stays in the server log** (one line,
  no traceback) — it can carry local paths and config, which have no business on the wire.
- Both of those `500`s are what you see with `VOICE_LOOP_TTS_FALLBACK_ENGINE=none`. With a fallback
  configured — and on the `xtts` engine there is one **by default** — the request is served by the
  fallback voice instead and marked as such; see [Engine fallback](#engine-fallback).
- **Hardware**: on an RTX-class GPU expect **~2–2.5 GB of VRAM**; if the model does not fit, the
  load falls back to CPU automatically (it works, just slower than real time). Output is 24 kHz.
- **Languages**: XTTS-v2 is multilingual on its own (`en ru de es fr it pt pl tr nl cs ar hu ko ja
  hi zh-cn` — note: no `uk`); `language` comes from the request as usual. The Silero stress
  pipeline (RUAccent, `stress.json`, `+` markers) is **skipped** — XTTS has its own prosody — and
  any `+` markers or combining acutes already in the text are stripped before synthesis.
- **Model dir** (`VOICE_LOOP_XTTS_MODEL_DIR`): point at a directory containing the downloaded model
  (`config.json` next to the weights) to load from disk instead of coqui's cache.

## Engine fallback

A down engine should degrade a voice, not silence it. When the primary engine cannot speak —
`coqui-tts` missing, weights that will not load, a synthesis call that raises — the **same request**
is retried on `VOICE_LOOP_TTS_FALLBACK_ENGINE`, and the response says who spoke:

| response | header / event | meaning |
|---|---|---|
| `/tts` normal | `X-Voice-Loop-Engine: xtts` | the configured engine synthesized it |
| `/tts` fallback | `X-Voice-Loop-Engine: silero (fallback)` | the primary failed; you are hearing the other voice |
| `/tts/stream` | terminal `end` event: `{"chunks": N, "engine": "silero (fallback)"}` | same, in the stream's own contract |

The default is `silero` when the engine is `xtts`, and `none` otherwise — a Silero-primary server
has nothing lighter to fall to, and a fallback that is the primary engine, or an unrecognized name,
means `none` as well. `GET /health` reports the **effective** setting as `tts_fallback_engine` plus
a per-process `tts_fallbacks` counter (how many requests a broken primary handed over).

What it is deliberately *not*: a router. A `400` refusal is the request's problem and no engine can
fix it, so it is never retried — including an unsupported language, even when the other engine
happens to speak it (`uk` on the `xtts` engine stays a `400`, though Silero has a voice for it).
Such a refusal does point at the fix, though: when the other engine speaks the requested language,
the `400` carries `"hint": "switch VOICE_LOOP_TTS_ENGINE=silero to serve 'uk'"` — which voices this
server has is the operator's setting, not a per-request decision. No engine speaks it, no hint.
Only an engine-level failure hands over. And if the fallback cannot serve that particular request
either — `ja` and `zh-cn` are XTTS-v2 languages Silero has no model for — you get that engine's
ordinary refusal, so it is clear why nothing here could speak. The primary's failure, with its
traceback, always stays in the server log.

**On the stream, a fallback only happens before the first chunk.** Once a chunk is on the wire the
`200` and the first audio have left, and a client mid-playback is not handed a different voice in
the middle of a sentence: a later failure ends the stream with today's terminal `error` event,
exactly as before. A restart re-synthesizes the whole text from chunk `0`, so one stream is always
one engine end to end — and its chunks carry that engine's sample rate (48 kHz Silero, 24 kHz XTTS).

Fallback synthesis takes the same one model slot the primary took — sequentially, after the failed
attempt released it (see [Capacity](#capacity)). A retry never doubles the concurrency.

**A persistently broken primary is paid for on every request.** There is no circuit breaker: each
request tries the primary, fails, and only then synthesizes on the fallback — so an engine that
stays down for hours costs every request that failed attempt (a synthesis that raises, or a model
load that dies) on top of the real one. That is the deliberate trade — no state to reset, so an
engine that comes back is used again immediately — but it is not free, and it is not a substitute
for fixing the primary. `tts_fallbacks` on `GET /health` climbing request-for-request is the
signal: fix the engine, or set `VOICE_LOOP_TTS_ENGINE` to the one that works.

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
| `end` | `{"chunks": N, "engine": "silero"}` | terminal success — N `chunk` events were sent. `engine` names who synthesized them, `"<engine> (fallback)"` when the primary was broken (see [Engine fallback](#engine-fallback)); it is reported here rather than in a header because the headers leave before the first chunk |
| `error` | `{"error": "synthesis failed (<ExceptionClass>)", "chunks": N}` | terminal failure **mid-stream** (the `200` already left with the first bytes, so a late failure becomes the last event, never a 500). N chunks were already sent and are valid. The message is deliberately generic — the exception class name at most; the full detail stays in the server log. |

A stream always ends with exactly one `end` **or** one `error` event. Requests refused *before*
synthesis starts (empty text, unsupported language, misconfigured xtts) return plain JSON `400`/`500`
exactly like `/tts`. Chunk granularity is the sentence chunker for **both** engines (XTTS-v2's
internal `inference_stream` generator sits below coqui's public API, so it is not used).

**Pacing.** `/tts` inserts a short silence (0.4 s) between sentence chunks; the stream carries the
**same** silence so back-to-back playback of the chunks sounds identical, not rushed. It is placed
at the **head** of every chunk after the first — leading rather than trailing silence keeps the
first chunk's latency untouched and needs no lookahead for "is this the last chunk". Play the
chunks back to back and the pacing matches `/tts`.

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

The suite lives next door in `../tests/` and runs from the plugin directory (that is where
`pytest.ini` and `.coveragerc` are). From here:

```sh
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r ../tests/requirements.txt
cd .. && pytest --cov=voice_server --cov-fail-under=100
```

No models, no network, no audio hardware: the recognizer, the voices and the accentuators are loaded
through seams the tests replace with fakes. Coverage is gated at 100% — see
[`../TESTING.md`](../TESTING.md) for what that claim covers.

## Reaching it from another machine

The server has **no authentication**. Keep the default loopback bind and tunnel:

```sh
ssh -N -L 8355:127.0.0.1:8355 user@gpu-host
# endpoint on the client stays http://127.0.0.1:8355
```

If you really want it on your network, set `VOICE_LOOP_HOST=0.0.0.0`, keep it behind a firewall you
control, and be aware that anyone who can reach the port can transcribe and synthesize on your
hardware. Binding to `0.0.0.0` logs a loud startup warning (expected inside Docker, where it is the
image default — the container's port mapping is the real boundary).

### Cross-site browser requests are refused

A loopback bind protects you from the network, not from your own browser: a multipart POST is a
CORS-"simple" request, so any web page you visit could quietly fire real requests at
`http://127.0.0.1:8355` and burn your CPU/GPU on transcription. The server therefore returns `403`
to any request a browser labels as coming from another site — `Sec-Fetch-Site: cross-site`, or an
`Origin` header naming anything other than a loopback host (`127.x`, `localhost`, `[::1]`) or
`null`. Non-browser clients (curl, the plugin scripts) send neither header and are unaffected.
Related request caps: `/stt` uploads are limited by `VOICE_LOOP_MAX_UPLOAD_BYTES` (25 MB default),
by `VOICE_LOOP_STT_TIMEOUT` (900 s of transcription, whatever the codec) and, for parseable WAV, by
`VOICE_LOOP_MAX_STT_SECONDS`; `/tts` text by `VOICE_LOOP_MAX_TTS_TEXT_BLOB`
(3000 default) and `/tts/stream` text by `VOICE_LOOP_MAX_TTS_TEXT` (20 000 default) — see
[Capacity](#capacity) for why the two TTS paths differ.
