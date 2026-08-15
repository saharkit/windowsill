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
difference between a voice that reads and a voice that stumbles. Two optional engines are one
environment variable away: [XTTS-v2](https://huggingface.co/coqui/XTTS_v2) voice cloning — see
[XTTS engine](#xtts-engine-voice-cloning) — and, for Ukrainian, the dedicated
[robinhad/ukrainian-tts](https://github.com/robinhad/ukrainian-tts) voices — see
[Ukrainian engine](#ukrainian-engine-dedicated-uk-voices). When an engine breaks, synthesis degrades
to another one instead of failing — see [Engine fallback](#engine-fallback).

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

The `127.0.0.1:` prefix on `-p` is the boundary: the server has no authentication, and without it
Docker publishes the port on every interface the host has. `VOICE_LOOP_HOST=0.0.0.0` inside the
image is correct as it stands — inside the container the server must bind every interface or the
mapping forwards to nothing — so never "fix" this by narrowing the ENV; fix the publish.

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
follows `VOICE_LOOP_DEVICE`, Silero and the ukrainian engine are always on the CPU, XTTS goes
wherever it managed to load
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
  the byte cap alone, honestly unmeasured. An upload faster-whisper cannot decode at all is refused
  with a `400`, never a bare `500`.
- **`/stt` therefore also carries a wall clock**, `VOICE_LOOP_STT_TIMEOUT` (900 s), which needs no
  header and so bounds the holding time whatever the codec. Whisper decodes lazily, so the budget is
  checked as segments arrive and a transcription that outruns it is abandoned — `503`, with its slot
  handed straight back to whoever is queued. What can overshoot is one segment's decoding, not one
  file's. Set it to `0` to switch the bound off and let long uploads run as long as they need.

Separately, **every POST body is size-gated before it is parsed**: the cap is read off
`Content-Length` and enforced in middleware, before the multipart/JSON parser spools a byte, so a
huge body is refused (`413`) without being read — and a chunked body (no `Content-Length`) is refused
the same way. The two TTS endpoints carry a further fixed **1 MiB** cap on the raw JSON body
(whitespace and every field counted), also on `Content-Length`, so a request whose `text` is short
but whose other fields are huge is refused before FastAPI decodes it.

The [recolor stage](#rvc-recolor-stage-voice-conversion) is deliberately **outside** all of this: it
is not a model call on this box, so it takes no slot and its wait cannot block a synthesis queued
behind it. Its own bounds are `VOICE_LOOP_RVC_TIMEOUT` per piece and `VOICE_LOOP_MAX_UPLOAD_BYTES`
on what comes back. How many conversions the converter is willing to run at once is **its** admission
control to do, not this server's — what is bounded here is how long any one of them may keep a
request waiting.

## Configuration (all environment variables)

| variable | default | meaning |
|---|---|---|
| `VOICE_LOOP_HOST` | `127.0.0.1` | bind address. **Loopback by default** — see below |
| `VOICE_LOOP_PORT` | `8355` | port |
| `VOICE_LOOP_DEVICE` | `auto` | `auto` / `cuda` / `cpu` |
| `VOICE_LOOP_LANGUAGE` | `en` | default language when a request does not name one |
| `VOICE_LOOP_STT_MODEL` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3-turbo` … |
| `VOICE_LOOP_COMPUTE_TYPE` | `auto` | faster-whisper compute type |
| `VOICE_LOOP_STT_HINT` | — | server-wide lexicon hint (comma-separated names/jargon) fed to faster-whisper as `initial_prompt`; a per-request `?prompt=` from a client's `stt.prompt` **wins over** this default |
| `VOICE_LOOP_TTS_ENGINE` | `silero` | `silero`, `xtts` (XTTS-v2 voice cloning) or `ukrainian` (dedicated uk voices) — both optional dependencies |
| `VOICE_LOOP_TTS_ENGINE_<LANG>` | — | per-language engine override: `VOICE_LOOP_TTS_ENGINE_UK=ukrainian` routes only `uk` requests, everything else stays on the global engine (`-` → `_`, so `zh-cn` is `VOICE_LOOP_TTS_ENGINE_ZH_CN`) |
| `VOICE_LOOP_TTS_FALLBACK_ENGINE` | `silero` when any primary is not `silero`, else `none` | `silero` / `xtts` / `ukrainian` / `none` — engine a failed synthesis retries on, see [Engine fallback](#engine-fallback) |
| `VOICE_LOOP_TTS_MODEL` | per language | override the Silero model for the default language |
| `VOICE_LOOP_TTS_SPEAKER` | per language | override the default speaker |
| `VOICE_LOOP_XTTS_REFERENCE` | — | wav of the voice to clone — the `xtts` engine refuses requests without it |
| `VOICE_LOOP_XTTS_MODEL_DIR` | coqui's cache | load XTTS-v2 from a local directory instead of downloading |
| `VOICE_LOOP_XTTS_MIN_FREE_VRAM_BYTES` | `3221225472` (3 GiB) | minimum free VRAM before XTTS moves to CPU, preserving whisper/RVC GPU tenants |
| `VOICE_LOOP_RVC_URL` | — | recolor every synthesized piece through an RVC voice-conversion service at this `http(s)` URL — see [RVC recolor stage](#rvc-recolor-stage-voice-conversion) |
| `VOICE_LOOP_RVC_TIMEOUT` | `10` | per-piece budget for that call, in seconds; a converter that outruns it leaves the base voice through |
| `VOICE_LOOP_CORPUS_DIR` | — | accumulate the `xtts` engine's own output here as an RVC **training** corpus — see [The training corpus](#the-training-corpus) |
| `VOICE_LOOP_CORPUS_MAX_SECONDS` | `1800` (30 min) | stop recording clips past this much audio |
| `VOICE_LOOP_STRESS_FILE` | `~/.config/voice-loop/stress.json` | your stress overrides |
| `VOICE_LOOP_HOOK_STAMP_FILE` | `~/.local/state/voice-loop/hook-last-fired` | the hook's heartbeat stamp — `/health` reports its age as `hook_last_fired_age_s`; see [the troubleshooting entry](../docs/troubleshooting.md#the-voice-stops-entirely-mid-session-but-everything-works-by-hand) |
| `VOICE_LOOP_ACCENT` | `1` | set `0` to skip automatic accentuation |
| `VOICE_LOOP_MAX_UPLOAD_BYTES` | `26214400` (25 MB) | pre-parse size cap on every POST body, checked on `Content-Length` before the body is parsed — a larger body gets `413` |
| `VOICE_LOOP_MAX_TTS_BODY_BYTES` | `1048576` (1 MiB) | raw JSON-body cap on the two TTS endpoints, checked on `Content-Length` before the body is parsed — a larger body gets `413` |
| `VOICE_LOOP_MAX_STT_SECONDS` | `600` | `/stt` duration cap when the WAV header is parseable — see [Capacity](#capacity) |
| `VOICE_LOOP_STT_TIMEOUT` | `900` | `/stt` wall-clock transcription budget, any codec; `0` disables it — see [Capacity](#capacity) |
| `VOICE_LOOP_MAX_TTS_TEXT` | `20000` | `/tts/stream` text length cap — longer text gets `400` |
| `VOICE_LOOP_MAX_TTS_TEXT_BLOB` | `3000` | `/tts` single-blob text cap — longer text gets `400` pointing at `/tts/stream` |
| `VOICE_LOOP_MODEL_CONCURRENCY` | `1` on a GPU, else up to `2` | concurrent model calls on the primary device — see [Capacity](#capacity) |

`GET /health` also reports a `version` field (`"0.5.0"`). It is for diagnostics and bug reports;
clients should detect features through the capability flags (`"streaming": true` and friends), not
by comparing version strings.

`GET /health` reports whether it has actually **verified** the server rather than merely answered:
`status` is `"verified-healthy"` only when the configured voice engine answered its availability
probe AND the VRAM figure the models need was read; anything short of both is `"unknown"`, with
`reason` naming which check did not answer. The legacy `ok` boolean is `true` exactly when `status`
is `"verified-healthy"` — so a server whose engine cannot be reached, or whose card will not report
free VRAM, stops claiming `ok` instead of answering it without looking. On a CPU-only box there is
no VRAM figure to read, so that half of the check is satisfied vacuously.

`GET /health` also reports the **hook's heartbeat**: `hook_last_fired` (ISO-8601 UTC) and
`hook_last_fired_age_s` (seconds since), read from the stamp the speaking hook rewrites on every
invocation. An age that keeps growing while the session continues means the harness has stopped
calling the hook — see
[the troubleshooting entry](../docs/troubleshooting.md#the-voice-stops-entirely-mid-session-but-everything-works-by-hand).
Both are `null` when the heartbeat is not observable here — the stamp file is unreadable
(corrupt / not yet written / on a different machine the server cannot see — the ssh-tunnel
setup), OR the server is bound to a non-loopback address (`VOICE_LOOP_HOST=0.0.0.0`, a LAN IP,
etc.) and the stamp this machine CAN read is not the one the remote client's hook writes
(#179, defect 2).

### Languages

| code | synthesis (Silero) | default speaker | automatic stress |
|---|---|---|---|
| `en` | `v3_en` | `en_0` | not needed |
| `ru` | `v4_ru` | `baya` | RUAccent |
| `uk` | `v4_ua` | `mykyta` | ukrainian-word-stress |
| `de` | `v3_de` | `eva_k` | — |
| `es` | `v3_es` | `es_0` | — |
| `fr` | `v3_fr` | `fr_0` | — |
| `tr` | XTTS-v2 (`VOICE_LOOP_TTS_ENGINE=xtts`) | cloned reference voice | XTTS prosody |

Recognition works for every language whisper supports. Turkish is routed to XTTS-v2 by default when
its optional dependency and reference voice are configured; on a GPU-less machine configure the cloud
pair `stt.cloud.provider: "deepgram"` and `tts.cloud.provider: "elevenlabs"` instead. A `/tts` request
for a language not in the
table returns `400` with the supported list — use a cloud TTS backend for those, or add the model to
`SILERO_VOICES` in `voice_server.py`. `uk` also has an optional **dedicated engine** with its own
trained voices (the Silero `v4_ua` voice reads Ukrainian with a noticeably Russian accent) — see
[Ukrainian engine](#ukrainian-engine-dedicated-uk-voices).

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

## Ukrainian engine (dedicated uk voices)

The first native-Ukrainian listener verdict on the default uk path was blunt: Silero's
`v4_ua`/`mykyta` is noticeably robotic and the accent is off — the stress pass (fixed in #26) helps,
but the source model is the ceiling, and XTTS-v2 cannot step in (its 17 languages do not include
Ukrainian). The `ukrainian` engine is [robinhad/ukrainian-tts](https://github.com/robinhad/ukrainian-tts):
voices **trained for Ukrainian** — `tetiana` (the default), `mykyta`, `lada`, `oleksa` — with
community-reported natural prosody. It was chosen over Piper's uk voices, which are lighter and
faster but robotic in the same way Silero is; the final call is the same kind the Silero verdict
came from — a native listener rating the new voice against the old one. That A/B is the acceptance
check for this engine; the wiring below is what makes it one environment variable away.

**Install the ordered recipe, not a bare `pip install ukrainian-tts`.** A bare install of
ukrainian-tts (6.0.2) bites twice: it hard-pins `ukrainian-word-stress==1.1.0`, downgrading the
`>=2.0` the server requires and breaking the Silero uk path's dictionary-only stress mode along
with it; and it declares `torchaudio`, which nothing else here needs — resolved off the default
index, that pull arrives CUDA-flavoured, gigabytes of nvidia libraries and all. Same medicine as
XTTS, verified end to end in a clean venv (and re-verified weekly by the `ukrainian-install` job
of the `xtts-install-probe` workflow):

```sh
# CPU — torch AND torchaudio go in FIRST, from the index you want them from, so the second
# command finds them satisfied instead of resolving its own (default-index, CUDA-flavoured) wheels
pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
pip install ukrainian-tts
# then put the stress pin back — pip WARNS that ukrainian-tts wants ==1.1.0; that warning is
# expected. The engine only touches Stressifier/StressSymbol, which 2.x keeps, and its import
# builds that Stressifier — the weekly probe exercises exactly this pairing.
pip install 'ukrainian-word-stress>=2.0'
```

The MIT-licensed voices download from Hugging Face on first use.

Then route Ukrainian at it — per language, which is the shape you want, or globally:

```sh
# Recommended: uk gets the dedicated voices, every other language stays on your configured engine.
# A broken ukrainian engine degrades back to Silero's uk voice — that pairing is the DEFAULT
# fallback once any primary is not silero (see Engine fallback).
VOICE_LOOP_TTS_ENGINE_UK=ukrainian python voice_server.py

# Or make it the global engine: VOICE_LOOP_TTS_ENGINE=ukrainian — but it speaks 'uk' ONLY,
# so every other language then 400s (with a hint pointing back at silero).
```

What to know:

- **Voices**: the request's `speaker` field picks `tetiana` / `mykyta` / `lada` / `oleksa`
  (default `tetiana`). Output is 22050 Hz, synthesized on the **CPU** — the model is small and
  real-time there; the GPU stays free for recognition.
- **Licensing**: unlike the XTTS-v2 weights (CPML, non-commercial), the ukrainian-tts package and
  its voices are **MIT-licensed** per the upstream project — commercial use is fine. They are still
  **never bundled**: the package downloads them from Hugging Face on **your** first request.
- **Import failures** read exactly like the xtts ones: a genuinely absent package hands you the
  ordered recipe above; a package that is installed but cannot import names the exception
  class and the missing dependency, with the message kept in the server log.
- **Stress**: the engine runs its **own** stress pass (ukrainian-word-stress in dictionary mode —
  the same dictionary the Silero path uses), so the Silero stress pipeline and your `stress.json`
  overrides are **skipped**, and `+` markers or combining acutes in the text are stripped — the same
  rule as XTTS. The accentuator comparison to listen for in the A/B is therefore the engine's own
  dictionary stress against Silero-plus-accentuator, which is the honest pairing.

## Engine fallback

A down engine should degrade a voice, not silence it. When the primary engine cannot speak —
`coqui-tts` missing, weights that will not load, a synthesis call that raises — the **same request**
is retried on `VOICE_LOOP_TTS_FALLBACK_ENGINE`, and the response says who spoke:

| response | header / event | meaning |
|---|---|---|
| `/tts` normal | `X-Voice-Loop-Engine: xtts` | the configured engine synthesized it |
| `/tts` fallback | `X-Voice-Loop-Engine: silero (fallback)` | the primary failed; you are hearing the other voice |
| `/tts/stream` | terminal `end` event: `{"chunks": N, "engine": "silero (fallback)"}` | same, in the stream's own contract |
| either, recolored | `silero + rvc`, `silero (fallback) + rvc` | the [recolor stage](#rvc-recolor-stage-voice-conversion) repainted it — it composes with whichever engine spoke |

The default is `silero` behind any non-Silero primary — the global engine or a per-language
override — and `none` behind a silero-everywhere setup: a Silero-primary server has nothing lighter
to fall to, and a fallback that is the request's primary engine, or an unrecognized name, means
`none` as well. `GET /health` reports the **effective** setting behind the global engine as
`tts_fallback_engine` (plus the per-language routing as `tts_engine_overrides`) and a per-process
`tts_fallbacks` counter (how many requests a broken primary handed over).

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
one engine end to end — and its chunks carry that engine's sample rate (48 kHz Silero, 24 kHz XTTS,
22.05 kHz ukrainian).

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
| `chunk` | `{"index": 0, "audio": "<base64>"}` | one **complete, standalone WAV file** (own header, engine sample rate: 48 kHz Silero / 24 kHz XTTS / 22.05 kHz ukrainian). Decode base64, play, done. `index` counts from 0 in order. |
| `end` | `{"chunks": N, "engine": "silero"}` | terminal success — N `chunk` events were sent. `engine` names who synthesized them, `"<engine> (fallback)"` when the primary was broken (see [Engine fallback](#engine-fallback)); it is reported here rather than in a header because the headers leave before the first chunk. With the [recolor stage](#rvc-recolor-stage-voice-conversion) configured it carries one extra field, `"recolored": N`; without it, the event is byte-identical to what it always was |
| `error` | `{"error": "synthesis failed (<ExceptionClass>)", "chunks": N}` | terminal failure **mid-stream** (the `200` already left with the first bytes, so a late failure becomes the last event, never a 500). N chunks were already sent and are valid. The message is deliberately generic — the exception class name at most; the full detail stays in the server log. |

A stream always ends with exactly one `end` **or** one `error` event. Requests refused *before*
synthesis starts (empty text, unsupported language, a misconfigured optional engine) return plain
JSON `400`/`500` exactly like `/tts`. Chunk granularity is the sentence chunker for **every** engine
(XTTS-v2's internal `inference_stream` generator sits below coqui's public API, so it is not used).

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

## RVC recolor stage (voice conversion)

A second way to get a chosen voice, **composed instead of cloned**: synthesize with the fast base
engine and hand each finished piece of audio to an [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
(retrieval-based voice conversion) service, which repaints its *timbre* into a target voice. Set
`VOICE_LOOP_RVC_URL` and it is on; leave it unset and nothing about this server changes.

The trade against the `xtts` engine is a **training cost paid once**, in exchange for a timbre that
is typically steadier — a trained converter holds one voice, where zero-shot cloning drifts a little
between renders — and a pipeline whose slow half (the conversion) is fixed per piece rather than
proportional to how hard the sentence was to clone. Whether it is faster on *your* hardware is a
measurement, not a promise, and this repo has not made it: what it does promise is that both halves
are bounded and that a failure in either one still speaks. What the trade costs is data: **RVC wants
10-30 minutes of the target voice**, where XTTS-v2 clones from a 6-30 second reference. That is what
[the training corpus](#the-training-corpus) below exists to produce.

**The converter is not in this process, and this repo does not ship it.** It wants a GPU of its own,
and keeping it out is exactly what lets the base voice stay cheap and the two scale separately. RVC
itself is MIT; the model you train is yours. What lives here is the client and the contract:

| direction | what it is |
|---|---|
| request | `POST` to `VOICE_LOOP_RVC_URL`, `Content-Type: audio/wav`, body = one complete WAV |
| success | `200` with the recolored audio as a WAV body (its own sample rate — the server passes it through) |
| anything else | the base audio is sent instead, unrecolored |

Any RVC deployment fits behind that with a few lines of adapter. Only `http`/`https` URLs are opened
(a `file://` URL would read this disk and hand the bytes back as "audio"), the call is made with the
system proxy bypassed, and the answer is bounded by `VOICE_LOOP_RVC_TIMEOUT` and by
`VOICE_LOOP_MAX_UPLOAD_BYTES` — the same ceiling `/stt` puts on audio arriving from the network.

**A broken converter degrades the voice, it never silences it** — the rule the engine fallback
already follows. A service that is down, slow, or answers with something that is not a WAV hands the
base audio through, logged (the exception class, never its message) and counted on `GET /health`:

- `rvc` — whether the stage is on. The URL itself is **never** reported: `/health` has no
  authentication and an endpoint is nobody else's business;
- `rvc_recolored` / `rvc_failures` — pieces that made it through the converter, and pieces that
  did not. The second one climbing request-for-request is the signal to fix the converter.

**Where it sits.** On encoded audio, after the model slot is released and after the engine question
is settled — so it recolors whichever voice ended up speaking, fallback included, and it does not
hold the synthesis gate while it waits on the network. `/tts` recolors the finished blob (one call);
`/tts/stream` recolors **each chunk as it leaves**, so the stream stays a stream and the latency
added is one conversion, not one utterance.

There is one honest asymmetry with the engine fallback, and it is audible. A fallback refuses to
change voices mid-stream; the recolor stage cannot make that promise, because when the converter
dies halfway the only alternative to the base voice is silence from that chunk on. So the rest of
the stream comes through in the base voice and the `end` event says how many chunks were repainted
(`{"chunks": 12, "engine": "silero", "recolored": 4}`).

**No retry.** The degrade path costs nothing and is already in hand, while a retry would spend its
backoff inside a request someone is waiting to *hear* — between two chunks of one sentence, on the
streaming path. `rvc_failures` is the signal; a circuit breaker is not needed for a stage whose
failure is this cheap.

### The training corpus

The stage above cannot be trained out of thin air, and 10-30 minutes is a long time to sit in front
of a microphone. `VOICE_LOOP_CORPUS_DIR` is the bootstrap: with the `xtts` engine speaking, **the
cloned voice records its own training data as it speaks**, one clip per sentence chunk. A machine
that has been speaking to you through XTTS-v2 for a while has already produced the corpus that
trains its replacement — the tool below is how you find out whether yours has.

```sh
VOICE_LOOP_TTS_ENGINE=xtts \
VOICE_LOOP_XTTS_REFERENCE=~/voice/reference.wav \
VOICE_LOOP_CORPUS_DIR=~/voice/corpus \
COQUI_TOS_AGREED=1 python voice_server.py
```

What lands there:

```
~/voice/corpus/ru/9f1c4b2ea77d0c31.wav    one sentence chunk, raw — no inter-chunk pause, no recolor
~/voice/corpus/ru/9f1c4b2ea77d0c31.txt    the text that produced it
```

- **Only the `xtts` engine records.** The corpus trains a converter on the *cloned* voice; Silero's
  stock speakers are not it.
- **One directory per language**, which is the shape RVC's own training tools read.
- **Named by the audio's own digest.** Two syntheses running side by side cannot collide on a name,
  and the same sentence spoken twice is stored once — a corpus is worse, not better, for holding the
  same seconds twice.
- **Written atomically** (temp sibling, `fsync`, `os.replace`), so a trainer scanning the directory
  while the server writes it sees a whole clip or none, never a truncated one it would accept as
  valid audio.
- **Bounded** by `VOICE_LOOP_CORPUS_MAX_SECONDS` (30 minutes by default — the top of RVC's useful
  range). The cap counts what is already on disk, so a restart does not start it growing again.
- **Never costs you your voice.** A clip that cannot be written is logged and skipped; the request
  it came from is unaffected.
- `GET /health` reports `corpus_clips` and `corpus_seconds` — `null` for both when no corpus is
  configured, and still `null` while one is configured but nothing has been written yet. `/health`
  reads the counters a write maintains and never scans the directory, so the first thing that turns
  the pair into numbers is a clip being recorded, not a health check.

Then ask whether there is enough yet, from the plugin's `scripts/`:

```sh
python3 ../scripts/rvc_corpus.py --corpus ~/voice/corpus
```

```
corpus: /home/you/voice/corpus

  language    clips    duration   per clip
  ru            412     18m 07s   0.9s - 12.4s

  total         412     18m 07s
  usable        408     17m 51s   (4 outside 1s - 20s)

READY — 17m 51s of usable audio, past the 10m 00s mark.
```

It exits `0` once the corpus has reached `--min-minutes` (default 10) of **usable** audio and `1`
while it has not, so "train when ready" is one `if` around it. Clips under a second carry no usable
pitch contour and clips over twenty are usually a chunker artefact rather than speech; both are
counted in the total and left out of the training set. `--manifest FILE` writes that set as JSONL
(`{"audio": …, "language": …, "seconds": …, "text": …}`, one object per line, `-` for stdout) —
which is where this repo stops and RVC's own training takes over.

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
