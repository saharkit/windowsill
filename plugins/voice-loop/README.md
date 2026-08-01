# voice-loop

[![selftest](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml/badge.svg)](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml)
[![coverage: 100% (gated)](https://img.shields.io/badge/coverage-100%25%20%28gated%29-brightgreen)](TESTING.md)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](../../LICENSE)
[![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](../../server/README.md#requirements)

> The coverage badge is a **gate** (`--cov-fail-under=100` on the server's Python, statements and
> branches, on 3.10–3.13), not a drifting number; the shell scripts are held to shellcheck plus a
> real Stop-hook invocation in CI instead. [TESTING.md](TESTING.md) spells out both.

Talk to Claude Code, and hear it answer.

A Claude Code plugin that closes the voice loop in both directions:

- **out** — a `Stop` hook speaks the lines your assistant marks with `🔊`. Only marked lines are
  voiced, so you hear the summary and read the detail. Nothing else about your session changes.
- **in** — a push-to-talk script: press a hotkey, speak, press again. The audio is transcribed and
  lands in the focused window (or on your clipboard, which is the default no-permissions path).

Speech runs where you choose: **on this machine**, on **a box on your network**, or in the **cloud**.
Setup is not a document you follow — it is `/voice-setup`, a skill that probes your machine, installs
what is missing, writes your config, wires a hotkey, and proves the result with a loopback test.

## Quickstart

```
claude marketplace add saharkit/windowsill
/plugin install voice-loop@windowsill
```

then, in a session:

```
/voice-setup
```

Answer two or three questions (language, where speech should run) and it finishes with a green
selftest. To pick a custom synthetic voice afterwards: `/voice-design`.

To hear something, the model must be *asked* to speak — one line in your `CLAUDE.md` is enough, and
`/voice-setup` now offers to add it for you (globally or per-project). If you skipped that offer,
add it yourself:

> End each reply with a one-sentence spoken summary on its own line, starting with 🔊.

## The three backends

Each direction is configured independently — recognition local and synthesis cloud is a perfectly
reasonable mix.

| | where it runs | cost | privacy | notes |
|---|---|---|---|---|
| `local` | your machine | free | audio never leaves it | whisper `small` ≈ 2 GB RAM, a second or two per phrase on a modern CPU; Silero TTS is near real time. First run downloads ~0.5–1.5 GB of models |
| `lan` | another box you own, over HTTP or an ssh tunnel | free | stays on your network | the honest sweet spot if you have a GPU machine — `server/` is that server |
| `cloud` | a hosted speech API | per-use billing | **your audio and text leave your machine** | off by default; keys live in a file the config points at, never in the config |

## Languages

Recognition (whisper) is multilingual. Local synthesis ships the Silero voices below; **English is a
first-class language, not a fallback** — it has its own selftest phrase and its own CI loopback lane
(the macOS one).

| language | synthesis model | default speaker | automatic stress marking |
|---|---|---|---|
| `en` English | `v3_en` | `en_0` | not needed |
| `ru` Russian | `v4_ru` | `baya` | RUAccent |
| `uk` Ukrainian | `v4_ua` | `mykyta` | ukrainian-word-stress |
| `de` German | `v3_de` | `eva_k` | — |
| `es` Spanish | `v3_es` | `es_0` | — |
| `fr` French | `v3_fr` | `fr_0` | — |

Any other language: recognition still works; for synthesis use a cloud backend or the macOS built-in
`say` voice. Details in [`server/README.md`](../../server/README.md).

## A voice of your own

`/voice-design` casts a custom synthetic voice: you describe the timbre you want in your own words, it
auditions candidates through ElevenLabs text-to-voice, and the one you pick is written into your
config. It will not imitate a real, identifiable person — generalized timbre descriptions only.

**If it sounds robotic, it is usually not the model.** Two levers, in order. *Cloud:* raise
`stability` first (0.6–0.75 — breathy voices go metallic at LOW stability, not high), keep `style`
≤ 0.15, `similarity_boost` 0.75–0.85, `use_speaker_boost` on; keep each request short; and if
artifacts survive the settings, regenerate a new preview instead of tuning further. *Local ru/uk:*
what sounds robotic is nearly always **wrong stress** — install the accentuation package for your
language and add proper names to `stress.json` (or type a combining acute, `Ка́тя`, and the server
converts it). The full recipe lives in the `voice-design` skill.

**v0.2 roadmap — design in the cloud, then drop the key.** The planned path is: design the voice in the
cloud as above, mint a reference recording from the voice you chose, and run **XTTS-v2 on your own LAN
GPU** with that reference — after which the cloud key is no longer needed and synthesis is local
again. Same ethics rule applies to the reference: your own voice, or one you have explicit rights to.

## Latency — speak-back streams

The Stop hook does not synthesize the whole message before you hear anything. The marked text is
split into **sentence chunks** (tiny sentences are merged so a chunk is at least ~40 characters);
the first chunk starts playing the moment *it* is synthesized, and the next chunk synthesizes
while the previous one plays. What you wait for is one short synthesis, not the whole line. A
fresher turn still wins: a new hook invocation stops the playing chain (precisely — by the PIDs it
recorded, nothing pattern-matched) and speaks the new line instead.

The hook also reads the transcript **immediately** and retries only on the actual flush-race
signatures (an empty extract, or one identical to the last spoken line), with adaptive backoff —
an already-flushed transcript costs zero sleep.

Every spoken run logs its own before/after evidence to `~/.local/state/voice-loop/speak.log`:

```
timings extract_ms=450 first_audio_ms=583 total_ms=1581
```

`extract_ms` is the transcript read including any flush-race retries, `first_audio_ms` is when
sound actually started, `total_ms` is the whole run. On a multi-sentence line, `first_audio_ms`
sitting far below `total_ms` *is* the streaming, visible in numbers.

## What is in here

```
plugins/voice-loop/
  hooks/hooks.json        registers the Stop hook
  scripts/speak.sh        the Stop hook's launcher (stable entry point; never fails a turn)
  scripts/speak.py        the Stop hook: extract marked lines -> chunk -> synthesize -> stream-play
  scripts/dictate-toggle.sh   push-to-talk toggle: record -> transcribe -> clipboard/paste
  scripts/selftest.sh     hardware-free loopback proof (TTS -> STT -> compare)
  skills/voice-setup/     the agent installer
  skills/voice-design/    voice casting
  TESTING.md              the human acceptance checklist
server/                   the self-hostable speech server (FastAPI + faster-whisper + Silero), Dockerfile
```

## Permissions — the ladder, and why the default rung is the low one

Hooks run without permission prompts, so speak-back works under any permission mode with no extra
grants. Dictation is where privileges can creep in, so it is tiered and **starts at the bottom**:

1. **Default — no root, no consent dialogs, works everywhere.** The transcript goes to your clipboard
   and you press your own paste key. This is fully functional; everything below is convenience.
2. **Auto-paste, no root.** `wtype` (KDE/wlroots) or `xdotool` (X11) on Linux; on macOS a single
   Accessibility consent for `osascript`.
3. **Auto-paste on GNOME/Wayland — one root step.** Mutter exposes no virtual-keyboard protocol, so
   this needs the `ydotool` daemon. `/voice-setup` **prints** that command for you to run rather than
   running it, and says plainly what it grants.

`/voice-setup` is written for default permission mode: it announces its plan, batches its work into a
few coarse actions, and never hides a `sudo`.

## Verify it yourself

```sh
bash plugins/voice-loop/scripts/selftest.sh --endpoint http://127.0.0.1:8355
```

Synthesizes a known phrase, feeds the audio straight back into recognition, and compares the
transcript (case, punctuation and stress marks ignored). No microphone, no speakers, no display — it
runs in a bare container, and it is what CI runs on Linux and macOS on every commit.

The server's own Python is unit-tested with a hard 100% coverage gate on Python 3.10–3.13, and the
Stop hook is invoked for real in CI. The parts a machine cannot check for you — the hotkey, a real
microphone, whether you actually hear it — are a written checklist. Both halves, and what the
coverage number honestly claims, are in [TESTING.md](TESTING.md).

Deeper reading: [architecture](../../docs/architecture.md) ·
[troubleshooting](../../docs/troubleshooting.md) · [FAQ](../../docs/faq.md).

## Author

**Sahar** — AI engineer at saharkit. Designed and built live on 2026-08-01, generalized from a working
deployment.

## License

MIT — see [LICENSE](../../LICENSE).
