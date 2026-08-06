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
| `openai` (STT) | `whisper-1` | a second or two for a short clip; the whole clip uploads before work starts | list price **$0.006/min** | ~99 languages, **Russian ✅ Ukrainian ✅** | audio leaves the machine; retention follows the account's API data policy |
| `elevenlabs` (STT) | `scribe_v1` | a second or two; accuracy-first rather than latency-first | ≈**$0.0067/min** (≈$0.40/hour) on the paid tiers | 99 languages, **Russian ✅ Ukrainian ✅**; auto-detected, so `stt.language` does not reach it ([#93](https://github.com/saharkit/windowsill/issues/93)) | audio leaves the machine; zero-retention is an account/enterprise setting |
| `deepgram` (STT) | `nova-3` | the quickest of the three on short clips | ≈**$0.0043/min** pre-recorded; new accounts start with a **$200 credit** | **Russian ✅** via nova-3 multilingual (set `stt.language: "multi"`); **Ukrainian ⚠️** — nova-3 multilingual does not cover it, use `stt.model: "nova-2"` and check the vendor's model/language matrix | audio leaves the machine; a self-hosted deployment is offered, and `stt.cloud.endpoint` points at one |

## Text-to-speech — `tts.cloud.provider`

| provider | default model | latency | cost | language coverage | privacy posture |
|---|---|---|---|---|---|
| `openai` (TTS) | `tts-1` | under a second to first byte for a short line | list price **$15 per 1M characters** | multilingual, **Russian ✅ Ukrainian ✅** — in an English-accented voice | text leaves the machine; retention follows the account's API data policy |
| `elevenlabs` (TTS) | `eleven_multilingual_v2` | around a second to first byte; the flash models trade quality for speed | credit-based; the character rate depends on the plan tier | 29+ languages, **Russian ✅ Ukrainian ✅**; voice cloning through `/voice-design` | text leaves the machine; voices you design live in your ElevenLabs account |
| `deepgram` (TTS) | `aura-2-thalia-en` | the lowest first-byte latency of the three | ≈**$0.030 per 1k characters**; the $200 new-account credit covers it too | **English only** on Aura-2 (plus Spanish) — **Russian ❌ Ukrainian ❌** | text leaves the machine; a self-hosted deployment is offered |

**If your contour speaks Russian or Ukrainian, Deepgram is an STT choice and not a TTS one.** That
asymmetry is the reason the two directions are configured independently — `stt.cloud.provider:
"deepgram"` beside `tts.cloud.provider: "elevenlabs"` is a perfectly ordinary config.

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
