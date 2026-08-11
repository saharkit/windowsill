# Choosing a speech provider

voice-loop talks to a speech provider through a **registry**: one entry per provider, per
direction, in [`scripts/providers.py`](scripts/providers.py). This page is the human half of that
registry — what a user actually picks from. A test (`tests/test_providers.py`) fails if a provider
is on the shelf without a row here, so the two cannot drift apart.

> **The numbers below are list prices and published capabilities, transcribed on 2026-08-06.**
> Vendors reprice, rename models, and change which languages a model covers. Check the vendor's own
> pricing and model pages before you rely on a figure here — this table exists to tell you which
> *axes* differ and roughly where each provider sits, not to be a billing oracle.

## The choice before the choice: cloud at all?

| | where the audio goes | what it costs | what it needs |
|---|---|---|---|
| `local` / `lan` (whisper + Silero) | **nowhere — it never leaves your machine or your network** | free | a machine with the RAM (and ideally a GPU) to run it, and ~0.5–1.5 GB of model download |
| `cloud` | to the provider named below | per-use billing | an API key, and a network round trip per clip |

Local is the **privacy** option and it is a real one: the clip never leaves the machine. It is also
the option that asks you to provision and tune a model. If you are on a thin client without a GPU,
the cloud rows below are the path that works today.

## Speech-to-text — `stt.cloud.provider`

| provider | default model | latency | cost per minute | language coverage | privacy posture |
|---|---|---|---|---|---|
| `openai` (STT) | `whisper-1` | a second or two for a short clip; the whole clip uploads before work starts | list price **$0.006/min** | ~99 languages, **Russian ✅ Ukrainian ✅ Turkish ✅** | audio leaves the machine; retention follows the account's API data policy |
| `elevenlabs` (STT) | `scribe_v1` | a second or two; accuracy-first rather than latency-first | ≈**$0.0067/min** (≈$0.40/hour) on the paid tiers | 99 languages, **Russian ✅ Ukrainian ✅ Turkish ✅**; `stt.language` rides as Scribe's `language_code` (the plugin defaults it to `en`), and an explicitly empty value asks it to auto-detect | audio leaves the machine; zero-retention is an account/enterprise setting |
| `deepgram` (STT) | `nova-3` | the quickest of the three on short clips | ≈**$0.0043/min** pre-recorded; new accounts start with a **$200 credit** | **Russian ✅ Turkish ✅** via nova-3 multilingual (set `stt.language: "multi"`); **Ukrainian ⚠️** — nova-3 multilingual does not cover it, use `stt.model: "nova-2"` and check the vendor's model/language matrix | audio leaves the machine; a self-hosted deployment is offered, and `stt.cloud.endpoint` points at one |

### Jargon priming — `stt.prompt`

A free-text lexicon in `stt.prompt` biases the recogniser toward your recurring vocabulary — the
technical terms, product names and jargon a bare model transcribes wrong. It is the documented fix
for mixed speech (a Russian sentence carrying English terms): measured on real operator audio, a
bare `whisper-1 ru` wrote *"… Sighted, 16-bit, Little, Indian."*; primed with the neighbouring terms
it wrote *"… signed, 16-bit, little endian."* — **exact**. The priming even generalises: it recovered
*signed* verbatim though that word was not in the prompt list.

One key reaches two paths, and no provider gets a promise that was not measured:

| path | `stt.prompt` | how |
|---|---|---|
| `openai` (STT) | ✅ sent as the API's `prompt` field | the measured win above; truncated to a conservative token-safe budget (see below) |
| `local` / `lan` (whisper) | ✅ sent per request as faster-whisper's `initial_prompt` | the `?prompt=` query parameter **wins over** the server-wide `VOICE_LOOP_STT_HINT`, so a local user sets it in `config.json` rather than hand-editing the server's systemd unit |
| `elevenlabs` (Scribe) | — not sent, **not needed** | measured: Scribe transcribed *"Signed sixteen bit little endian"* correctly with no lever at all |
| `deepgram` (STT) | — not sent | Deepgram's keyterm prompting is model-specific and is **not wired here**; do not assume it |

The OpenAI API caps `prompt` at 224 tokens; the plugin truncates to a conservative character budget
on a term boundary (see `truncate_stt_prompt` in [`scripts/providers.py`](scripts/providers.py)) so
the cap is never exceeded and the API never has to cut silently — no term is split, and the leading
most-relevant terms are kept.

### Streaming speech-to-text — `stt.cloud.streaming`

A batch dictation makes a long one pay twice: you speak for a minute, then wait at the end while
the whole clip uploads and transcribes. A provider whose entry carries a **streaming variant** can
be fed the recording *while the microphone is open*, so by the time you stop, the transcript is
already assembled and the only wait left is the server flushing its last words.

| provider | streaming variant | what turning it on does |
|---|---|---|
| `openai` (STT) | — | nothing: the setting is ignored, with a line in `dictate.log` saying so |
| `elevenlabs` (STT) | — | the same |
| `deepgram` (STT) | **yes** — the live `/v1/listen` websocket, interim + final results | the recording is forwarded as raw 16 kHz mono PCM while you speak; the finals are assembled in order and pasted at stop-time |

```json
{ "stt": { "backend": "cloud", "cloud": { "provider": "deepgram", "streaming": true } } }
```

Everything else is unchanged, and deliberately so:

- the **WAV is still written** and still lands in `dictate-last.wav` — the socket tails the file, it
  does not stand between the recorder and the disk;
- **any** failure — no key, a socket that will not open, an auth refusal, a server that hangs up
  mid-recording, a worker that misses its bound, a stream that carried nothing — logs its reason and
  falls back to the ordinary record → POST flow. A recording is never lost to the live path;
- the **model and language are the same axes** as the batch call (`stt.model`, `stt.language`), so a
  Russian contour that set `stt.model: "nova-2"` streams with nova-2;
- billing is Deepgram's streaming rate rather than its pre-recorded one — check the vendor's page;
  the socket is closed as soon as the recording ends (and by the worker's own evidence if the stop
  toggle never comes), so an idle hotkey cannot hold a metered connection open.

Every dictation logs `dictation latency stop_to_paste_ms=… via=stream|batch`, which is how the two
paths are compared on your own machine rather than on a claim in this table.

## Text-to-speech — `tts.cloud.provider`

| provider | default model | latency | cost | language coverage | privacy posture |
|---|---|---|---|---|---|
| `openai` (TTS) | `tts-1` | under a second to first byte for a short line | list price **$15 per 1M characters** | multilingual, **Russian ✅ Ukrainian ✅ Turkish ✅** — in an English-accented voice | text leaves the machine; retention follows the account's API data policy |
| `elevenlabs` (TTS) | `eleven_multilingual_v2` | around a second to first byte; the flash models trade quality for speed | credit-based; the character rate depends on the plan tier | 29+ languages, **Russian ✅ Ukrainian ✅ Turkish ✅**; voice cloning through `/voice-design` | text leaves the machine; voices you design live in your ElevenLabs account |
| `deepgram` (TTS) | `aura-2-thalia-en` | the lowest first-byte latency of the three | ≈**$0.030 per 1k characters**; the $200 new-account credit covers it too | **English and Spanish only** on Aura-2 — **Russian ❌ Ukrainian ❌ Turkish ❌** | text leaves the machine; a self-hosted deployment is offered |

**If your contour speaks Russian or Ukrainian, Deepgram is an STT choice and not a TTS one.** That
asymmetry is the reason the two directions are configured independently — `stt.cloud.provider:
"deepgram"` beside `tts.cloud.provider: "elevenlabs"` is a perfectly ordinary config. Deepgram's
voice is the model name (`tts.cloud.model`); `tts.speaker` and `tts.cloud.voice_id` do not select an
Aura voice. When switching providers, point `tts.cloud.api_key_env` at that provider's own key —
the ElevenLabs key-sharing rule applies to ElevenLabs STT only.

### Streaming text-to-speech — `tts.cloud.streaming`

A batch cloud line pays the TLS+websocket dial on every turn — ~300-420 ms, more than the synthesis
itself (the live probe measured ~170 ms warm). A provider whose entry carries a **streaming
variant** is reached over a websocket a **resident holder keeps open across turns**, so the dial is
paid once per session, not once per line, and a held socket renders at first-sound ≈ 200-250 ms. It
is the voice-back counterpart of streaming dictation, and closes the loop it opened: sub-second
both ways.

| provider | streaming variant | what turning it on does |
|---|---|---|
| `openai` (TTS) | — | nothing: the setting is ignored |
| `elevenlabs` (TTS) | **yes** — the live `/v1/text-to-speech/{voice}/stream-input` websocket, `eleven_flash_v2_5`, `pcm_22050` out | a resident holder holds one socket across turns (whitespace keepalive every ≤15 s against the vendor's ~20 s idle close, a throwaway priming frame on connect); the hook streams each line's audio as it is synthesized, and the local server/`tts.command` remains the fallback chain |
| `deepgram` (TTS) | — | nothing: Aura has no stream-input variant here |

```json
{ "tts": { "backend": "cloud", "cloud": { "provider": "elevenlabs", "streaming": true, "voice_id": "your-voice", "model": "eleven_flash_v2_5", "voice_settings": { "speed": 0.9 } } } }
```

Everything else is unchanged, and deliberately so:

- the **voice, model and output format are the same axes** as the batch call (`tts.cloud.voice_id`,
  `tts.cloud.model`), with one addition: `tts.cloud.voice_settings.speed` (ElevenLabs accepts
  ~0.7-1.2; default 1.0). A voice_settings edit is a **reconnect trigger** — the holder respawns
  with the new settings, because a change on a held socket may need a fresh stream;
- the streaming path asks for `pcm_22050` (raw s16le) — **no decoder in the critical path**, the
  holder wraps each fragment in a WAV header so the player queue plays it unchanged. `eleven_flash_v2`
  (no `.5`) is **English-only** — use `eleven_flash_v2_5` for the 32-language model;
- **budget is the real constraint, not latency** — Flash is ~0.25 credits/char on this account. On a
  **401/quota** the line degrades to the local Silero path (or `tts.command`), never silence;
- any failure — no key, a socket that will not open, a server that hangs up mid-line — degrades the
  line to the blob path for that one turn; the next turn tries the held socket again.

## The audio container, and why your player matters

`tts.cloud.output_format` means something different to each provider, because each vendor spells it
differently — which is exactly why its default lives on the registry entry:

| provider | `output_format` is… | default | plays with |
|---|---|---|---|
| `openai` | not used (the API is asked for WAV directly) | — | `afplay`, `aplay -q` |
| `elevenlabs` | one opaque vendor token, e.g. `mp3_44100_128` | `mp3_44100_128` | `afplay` on macOS; on Linux `mpg123` or `ffplay` — **`aplay` cannot play mp3** |
| `deepgram` | a raw query fragment, e.g. `encoding=linear16&container=wav` | `encoding=linear16&container=wav` | `afplay`, `aplay -q` |

## Degrade — one hop, and it says why

When the configured cloud STT provider fails — no key, a network error, an expired key, a quota
limit, an error document — dictation does not go silent: the failure is logged with its reason and
the clip is transcribed by the **local whisper server** at `http://127.0.0.1:8355` instead. The
degrade is **one-shot**: cloud → local, once, for that clip. The next clip tries the cloud again.

A multi-provider *cascade* (cloud → another cloud → local, with per-hop timeouts and a total
deadline) is deliberately **not** implemented — it is its own ticket, because it needs a config
schema (`stt.cloud.providers` as an ordered list) that propagates into `README.md`, the
`voice-setup` skill, `doctor.py` and `CONFORMANCE.md`.

## Adding a provider

One entry in `scripts/providers.py` — a row in `STT_PROVIDERS` or `TTS_PROVIDERS` plus its request
builder — and one row in this file. **No dispatch path learns its name.** The entry owns all seven
axes that vary: default model, request build (host, path, body encoding, where `language` goes),
auth header, response parse, credential resolution, error-document reading, and the remote default
host. `tests/test_providers.py` greps `scripts/` for a provider compared against a literal and
fails if one comes back.

A **streaming variant** is an eighth axis and lives on the same entry (`streaming=…`): the live
URL, the auth header, the message parser and the two control messages. A provider without one
carries `streaming=None`, and that — never a name comparison — is what the dictation path branches
on. The socket itself is `scripts/wsclient.py`, a stdlib-only RFC 6455 client written here rather
than taken as a dependency, because everything under `scripts/` installs by being copied.
