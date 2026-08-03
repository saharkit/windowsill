# voice-loop

[![selftest](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml/badge.svg)](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml)
[![coverage: 100% (gated)](https://img.shields.io/badge/coverage-100%25%20%28gated%29-brightgreen)](TESTING.md)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](../../LICENSE)
[![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](server/README.md#requirements)

> The coverage badge is a **gate** (`--cov-fail-under=100` on the server's Python, statements and
> branches, on 3.10–3.13), not a drifting number; the shell scripts are held to shellcheck plus a
> real Stop-hook invocation in CI instead. [TESTING.md](TESTING.md) spells out both.

Talk to Claude Code, and hear it answer.

A Claude Code plugin that closes the voice loop in both directions:

- **out** — a `Stop` hook speaks the lines your assistant marks with `🔊`. Only marked lines are
  voiced, so you hear the summary and read the detail. Nothing else about your session changes.
  (In a long tool-heavy turn, [eager mode](#eager-mode--hear-a-line-when-it-is-written-not-when-the-turn-ends)
  speaks them as they are written instead of at the end.)
- **in** — a push-to-talk script: press a hotkey, speak, press again. The audio is transcribed and
  the text lands wherever your cursor is when you stop — **any app**, not just Claude Code (or on
  your clipboard, which is the default no-permissions path). That reach is the feature and the
  footgun in one: see [Where the text lands](#where-the-text-lands--and-the-one-way-it-bites).

Speech runs where you choose: **on this machine**, on **a box on your network**, or in the **cloud**.
Setup is not a document you follow — it is `/voice-setup`, a skill that probes your machine, installs
what is missing, writes your config, wires a hotkey, and proves the result with a loopback test.

## Quickstart

In your shell:

```sh
claude plugin marketplace add saharkit/windowsill
```

then, **inside a Claude Code session** (these are slash commands, not shell commands):

```text
/plugin install voice-loop@windowsill
/voice-setup
```

(If you skipped the shell step, `/plugin marketplace add saharkit/windowsill` in the session does
the same thing.)

Answer two or three questions (language, where speech should run) and the install ends with its
proof: with HTTP speech endpoints (local server, LAN, cloud) that is the green **loopback selftest**;
with a direct-command backend (e.g. macOS `tts.command: "say …"`, which has no endpoint to loop
through) it is the **ear-check** — the agent speaks a line and you confirm you heard it. To pick a
custom synthetic voice afterwards: `/voice-design`.

Supported platforms: Linux and macOS. Windows/WSL is untested — we would rather say so than guess.

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
`say` voice. Details in [`server/README.md`](server/README.md).

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

**Design in the cloud, then drop the key — this path is live.** Design the voice in the cloud as
above, mint a reference recording from the voice you chose, and run **XTTS-v2 on your own machine or
LAN GPU** with that reference (`VOICE_LOOP_TTS_ENGINE=xtts` on the bundled server) — after which the
cloud key is no longer needed and synthesis is local again. Setup, licensing caveats and the
reference-wav contract are in
[`server/README.md` — XTTS engine](server/README.md#xtts-engine-voice-cloning). Same ethics
rule applies to the reference: your own voice, or one you have explicit rights to.

## Latency — speak-back streams

The Stop hook does not synthesize the whole message before you hear anything. The marked text is
split into **sentence chunks** (tiny sentences are merged so a chunk is at least ~40 characters);
the first chunk starts playing the moment *it* is synthesized, and the next chunk synthesizes
while the previous one plays. What you wait for is one short synthesis, not the whole line. A
fresher turn still wins: a new hook invocation stops the playing chain (precisely — by the PIDs it
recorded, nothing pattern-matched) and speaks the new line instead.

The hook also reads the transcript **immediately** and retries only on the actual flush-race
signatures (an empty extract, or one identical to the last spoken line), with adaptive backoff —
an already-flushed transcript costs zero sleep. If that backoff runs out while a previous line is
**still playing**, the hook keeps looking until the clip ends (bounded at 20 s) rather than giving
up: a line written during a long clip is **queued, not dropped**. And a hook that does end up
speaking nothing always writes the reason to `speak.log` — a line you never hear is never a line
nothing can account for.

Every spoken run logs its own before/after evidence to `~/.local/state/voice-loop/speak.log`:

```
timings extract_ms=450 first_audio_ms=583 total_ms=1581
```

All three are measured from one clock started when the hook begins. `extract_ms` is the transcript
read including any flush-race retries; **`first_audio_ms` is from hook start to the moment the
first player process is spawned** — the real time-to-first-sound, so it counts everything you wait
through before hearing anything: the transcript read, the `/health` probe, opening the stream and
the first chunk's synthesis. `total_ms` is the whole run. On a multi-sentence line, `first_audio_ms`
sitting far below `total_ms` *is* the streaming, visible in numbers.

## Eager mode — hear a line when it is written, not when the turn ends

The `Stop` hook fires **when a turn ends**. In a short turn that is the same moment; in a long
tool-heavy one it is minutes later, and the 🔊 line you were meant to hear at the top of the work
arrives after all of it. Eager mode fixes that by also listening to `PostToolUse`: every tool call
becomes a chance to speak the marked lines that have appeared so far, so a long turn narrates itself
as it goes.

It is **opt-in and off by default** — it speaks during the turn rather than after it, which is a
different feel, and nobody should get it without asking:

```json
{ "speak": { "eager": true } }
```

in `~/.config/voice-loop/config.json`. With it off, **nothing on this page happens to you**: the
`PostToolUse` registration exits before it reads anything (no transcript, no state, nothing per tool
call), and the `Stop` hook keeps exactly the behaviour it has always had — it dedups against the
line it spoke immediately before, and nothing else. Everything below is machinery that switches on
with `speak.eager`, ledger included.

What makes it safe to have two hooks reading the same transcript is a small **spoken-ledger** at
`~/.local/state/voice-loop/spoken.ledger`: `sha1(transcript_path + message index + line)` of every
line either path speaks, consulted by both before speaking. So a line is voiced **exactly once**,
whichever hook reached it first — `Stop` stays silent about what was already narrated, and says
whatever landed after the last tool call. The message index is in the key because a session says
"Done." many times, and the fifth one is a new line rather than an echo of the first. The ledger is
a rolling window (the last few hundred lines), not a journal.

Two more behaviours worth knowing:

- **A session is never recited back at you.** The first time either hook sees a transcript, every
  marked line already in it is written off as history — silently. Turning eager on mid-session
  therefore starts speaking from that point, instead of replaying the whole conversation.
- **One speaker at a time, and nobody waits in line.** Reading the ledger, claiming a line and
  speaking it all happen under one exclusive lock (`speaking.lock`), so two firings can neither
  claim the same line nor talk over each other. The lock is taken **without blocking**: an eager
  firing that loses the race exits immediately rather than queueing behind the speaker — it claimed
  nothing, so its line is simply picked up by the next tool call. Only `Stop` waits, and only for a
  fraction of a second, because it is the turn's last chance; after that it takes over (the
  supersede from the *Latency* section) and tries once more.

## What is in here

Everything this plugin is lives under its own directory — the repository root belongs to the shelf,
not to any one plugin.

```
plugins/voice-loop/
  .claude-plugin/plugin.json  the plugin manifest (name, version, description)
  hooks/hooks.json            registers the Stop hook (and the opt-in eager PostToolUse one)
  scripts/speak.sh            the speaking hook's launcher (stable entry point; never fails a turn)
  scripts/speak.py            the speaking hook: extract marked lines -> chunk -> synthesize -> stream-play
  scripts/dictate-toggle.sh   push-to-talk launcher (stable hotkey entry point)
  scripts/dictate.py          the toggle: record -> transcribe -> clipboard/paste-into-prompt
  scripts/selftest.sh         hardware-free loopback proof (TTS -> STT -> compare)
  skills/voice-setup/         the agent installer
  skills/voice-design/        voice casting
  server/                     the self-hostable speech server (FastAPI + faster-whisper + Silero), Dockerfile
  tests/                      the server's unit tests (no models, no network) — 100% gated in CI
  docs/                       architecture, troubleshooting, FAQ
  pytest.ini, .coveragerc     this plugin's own test run (invoked from this directory)
  TESTING.md                  the human acceptance checklist
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

## Where the text lands — and the one way it bites

Dictation is not wired into Claude Code. It is a hotkey that fills your clipboard and — on the
auto-paste tier — presses your paste key, so **the text lands wherever your cursor is when you stop:
any app**. A commit message, a browser search box, a colleague's chat window. That is system-wide
dictation, for free, and it is the reason the script is worth binding at all.

> **The same sentence, said plainly: one wrong window switch and words meant for the agent land
> somewhere else.** Start dictating in Claude Code, alt-tab to Slack while you are still speaking,
> stop the recording — the transcript is pasted into Slack. In `send` mode it is *sent*. Worst case
> that is something private in a public channel, and nothing about the paste knows the difference.

If you would rather trade the reach for a seatbelt:

```json
{ "dictate": { "paste_target": "same-window" } }
```

The guard remembers which window was focused when the recording **started**. If focus moved by the
time you stop, nothing is pasted anywhere: the text sits on your clipboard and a notification says
*"focus moved — text is in the clipboard"*, so you paste it yourself, in the window you meant. The
default is `"any"` — the power behaviour above — and the guard only applies on the auto-paste tier,
because on the clipboard tier you were always the one choosing the window.

What "the same window" can mean depends on what your desktop will tell us, and it is not the same
everywhere:

| session | what is compared | so it catches |
|---|---|---|
| macOS | the frontmost **application** (`osascript` / System Events) | app-to-app switches; *not* two windows of the same app |
| X11 | the active **window** id (`xdotool getactivewindow`) | any window switch, same app or not |
| Wayland | nothing — no portable query exists, and each compositor answers differently or not at all | **nothing: the guard degrades to `"any"`** and the warning above is your only protection |

That last row is the honest one. Under Wayland (including XWayland, where `xdotool` would answer for
X clients only and go stale the moment you switch to a native window) there is no answer we trust, so
the paste goes ahead rather than being suppressed on a guess. Every unknown degrades the same way —
a missing `xdotool`, a probe that timed out, a first recording started before the setting was on: a
guard that cannot see focus must not become a dictation that never pastes. `dictate.log` records what
it saw at start (`focus at start: …`) and why it suppressed a paste, so the guard is never silent
about which of the two happened.

## Verify it yourself

Inside a Claude Code session (where `${CLAUDE_PLUGIN_ROOT}` points at the installed plugin):

```text
bash "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.sh" --endpoint http://127.0.0.1:8355
```

From a manual checkout it is the repo-relative path instead:

```sh
git clone https://github.com/saharkit/windowsill && cd windowsill
bash plugins/voice-loop/scripts/selftest.sh --endpoint http://127.0.0.1:8355
```

Synthesizes a known phrase, feeds the audio straight back into recognition, and compares the
transcript (case, punctuation and stress marks ignored). No microphone, no speakers, no display — it
runs in a bare container, and it is what CI runs on Linux and macOS on every commit.

The server's own Python is unit-tested with a hard 100% coverage gate on Python 3.10–3.13, and the
Stop hook is invoked for real in CI. The parts a machine cannot check for you — the hotkey, a real
microphone, whether you actually hear it — are a written checklist. Both halves, and what the
coverage number honestly claims, are in [TESTING.md](TESTING.md).

**Uninstalling:** removing the plugin does not remove the 🔊 line from your `CLAUDE.md`, the
config/state dirs (`~/.config/voice-loop/`, `~/.local/state/voice-loop/`), downloaded models under
`~/.local/share/voice-loop/`, or a local server service you enabled — a proper uninstall story is
tracked in [issue #17](https://github.com/saharkit/windowsill/issues/17).

Deeper reading: [architecture](docs/architecture.md) ·
[troubleshooting](docs/troubleshooting.md) · [FAQ](docs/faq.md).

## Author

**Sahar** — AI engineer at saharkit. Designed and built live on 2026-08-01, generalized from a working
deployment.

## License

MIT — see [LICENSE](../../LICENSE).
