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
custom synthetic voice afterwards: `/voice-design`. To take the whole contour back off:
[`/voice-remove`](#uninstall).

Supported platforms: Linux and macOS. Windows/WSL is untested — we would rather say so than guess.

**On macOS, one trap is worth knowing about even though setup now handles it.** python.org-installer
Python ships an **empty certificate store**, so until `/Applications/Python 3.x/Install
Certificates.command` has been run once, every https call from it dies with `CERTIFICATE_VERIFY_FAILED
— unable to get local issuer certificate`: `pip install`, the model download, the cloud TTS request.
`/voice-setup` probes for this before it installs anything and offers to run that command for you;
`scripts/tls-probe.sh` is that probe if you want to ask by hand (`--fix` runs the repair and
re-probes). Details, and the cases that look the same but are not, in
[troubleshooting](docs/troubleshooting.md#macos-certificate_verify_failed-on-every-https-call).

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

### Cloud providers — a registry, not a switch

The `cloud` backend sends your audio (dictation) or your text (synthesis) to a hosted API. Which
one is a **registry entry**, chosen by `stt.cloud.provider` and `tts.cloud.provider` — the two
directions are independent, and mixing them is ordinary:

| provider | speech-to-text | text-to-speech | default models |
|---|---|---|---|
| `openai` | ✅ OpenAI-compatible (the default both ways) | ✅ | `whisper-1` / `tts-1` |
| `elevenlabs` | ✅ [Scribe](https://elevenlabs.io/docs/api-reference/speech-to-text) | ✅ + `/voice-design` voice cloning | `scribe_v1` / `eleven_multilingual_v2` |
| `deepgram` | ✅ Nova | ✅ Aura — **English (and Spanish) only** | `nova-3` / `aura-2-thalia-en` |

**[`PROVIDERS.md`](PROVIDERS.md) is the table to pick from** — latency, cost per minute, language
coverage (including Russian and Ukrainian) and privacy posture, side by side. The short version:
Deepgram is the cheapest and quickest for recognition and the `$200` new-account credit covers an
evaluation, but its synthesis does not speak Russian or Ukrainian; ElevenLabs and OpenAI do both.

**ElevenLabs STT reuses your existing ElevenLabs API key.** If you already configured
`/voice-design` (or set `VOICE_LOOP_TTS_API_KEY`), dictation works without a second key. When
`stt.cloud.provider` is `elevenlabs`, the script looks for a key in this order:

1. Your configured STT key (`stt.cloud.api_key_env` or `stt.cloud.key_file`)
2. The TTS key (`VOICE_LOOP_TTS_API_KEY`) — one credentials home, not a second one

That shared-key rule is ElevenLabs' alone, because it is the one provider covering both directions
with one account here: a `deepgram` STT config is never handed an ElevenLabs key.

**Privacy note: with any cloud `stt.cloud.provider`, your recorded audio clips leave your machine
and are sent to that provider's servers for transcription** — that is the trade the `cloud` row
above states. If you would rather keep your audio local, `stt.backend: "lan"` (the default)
transcribes on your own hardware.

**Adding a provider is one entry** in [`scripts/providers.py`](scripts/providers.py) plus its row
in `PROVIDERS.md` — no dispatch path in the plugin compares a provider name against a literal, and
a test enforces that.

### Degrade — what happens when the cloud is down

When the cloud backend fails — a network error, an expired key, a quota limit — dictation does
not go silent. The failure is **logged** (so you can see what happened), and the script
**degrades to the local whisper server** at `http://127.0.0.1:8355` for that clip. The next
clip tries the cloud again — the degrade is one-shot, not a permanent switch.

This means: kill your network mid-session, and your next dictation still transcribes through
local whisper with a log line marking the fallback. Nothing about your config changes, and you
never stare at a dead microphone wondering why.

One hop, deliberately. A *cascade* across several cloud providers before the local fallback is its
own feature with its own config schema, and it is not implemented — see the degrade section of
[`PROVIDERS.md`](PROVIDERS.md).

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

## Watching the contour — `contour-poll.sh`

A voice contour is resident services sharing a GPU, and its worst failure is the quiet one: a
service that demoted itself off the GPU keeps serving — correctly, an order of magnitude slower —
and nothing breaks loudly. `scripts/contour-poll.sh` is the small monitoring substrate for that:
one poll per run of every configured service's `/health`, free VRAM from `nvidia-smi`, and the
results written atomically to `~/.local/state/voice-loop/contour.json`. The speaking hook reads
that file at the end of every turn and **voices an active alert once** — a page, not a dashboard.
No Prometheus, no root.

```json
{
  "contour": {
    "services": [
      { "name": "speech", "health": "http://127.0.0.1:8355/health" },
      { "name": "converter", "health": "http://192.0.2.10:8358/health",
        "expect_device": "gpu" }
    ],
    "vram": { "min_free_mib": 200 }
  }
}
```

- **Alerts.** A service that does not answer (or reports `ok: false`); a service serving on a
  device other than its `expect_device` — set that key exactly when a client depends on the fast
  path, because that dependency is yours to declare and the alert means "the fast path is gone";
  free VRAM under `vram.min_free_mib` (default 200); an `oom_overflows` counter that **changed**
  (a rise, or a drop — these are per-process counters, so a counter that went backwards is a
  service that restarted and is already overflowing again; a steady counter does not re-page); and,
  read by the hook rather than the poller, **a status file nobody has refreshed** (see `max_age`).
  `ok` is a service's *optional* self-assessment: `false` alerts, and **absent does not** — a
  third-party `/health` with its own vocabulary (the `converter` above) is answering, not down.
- **Exit codes, and the vocabulary is closed.** `0` quiet, `1` an alert is active, `64` everything
  else — called wrong, a config that will not parse, a knob that is not a number, a `services`
  value that is not a list, an entry with no fetchable URL, two entries resolving to one name, or
  a poll that failed. **`1` never means anything but "page"**, so a cron line or scheduler can
  branch on it — and, for the same reason, a poll that found an alert and then could not *write*
  its status file still exits `1`: the diagnosis outranks the broken monitor, and the exit code is
  the only channel it has left. Every other `64` still tries to write the status file, carrying a
  `poller-error` alert, so a broken poller is heard rather than leaving the hook to read a stale
  "all quiet".
- **How long a poll takes, as a bound.** `(services + 1) × contour.timeout`, polled one after
  another, with no other wait in the path — at the default `timeout: 5`, ten seconds for one
  service and under a minute for ten, against a five-minute cadence.
- **Numbers are numbers.** Every numeric knob must be a JSON number: `"5"` is refused exactly like
  `"5s"`. Accepting the string that happens to parse and refusing the one that does not is a rule
  no operator can predict, and it is the accepted one that goes on being written.
- **`max_age`** (default 900 s — three missed polls at a five-minute cadence) is written into
  `contour.json`, and the hook pages when the file is older than it. Remove the cron entry and the
  status file freezes at its last green poll; without this bound "the contour is fine" and "nobody
  looked" are the same silence. A stale file's own alerts are dropped with it — a reading nobody
  refreshed says nothing about now.
- **What is in the file: the alerts and the last sample, nothing accumulated.** There is no latency
  history and no p95. There was — a week-long per-service window and a p95 split by device — and
  nothing read it: no alert rule, no SLO, no caller in this repository. Meanwhile the hook re-parsed
  the whole file on every tool call (967 KB and 6.3 ms for three services) to reach an `alerts` key
  that is almost always empty. The window comes back with the SLO that consumes it; today the file
  stays a few hundred bytes and the read is free.
- **`contour.status_path`** relocates `contour.json` (default
  `~/.local/state/voice-loop/contour.json`) — **for both halves at once**, because the hook reads
  the same key. `--status` overrides it for a one-off probe or a test; do **not** use it in a
  scheduled command, because the hook cannot see a command-line flag and a poller writing where
  nobody reads pages nobody.
- **`vram.command: false`** disables the GPU probe (a Mac, a GPU-less box) — an empty string
  cannot, because an empty config value means "unset" everywhere in this plugin.
- **`contour.alerts: false`** opts out of the spoken page; the poller keeps writing the file.

### Scheduling it

The poller is one shot; running it every few minutes is yours to arrange. `/voice-setup` does not
do it for you — the contour is optional and the cadence is a judgement about your machine — but
**`/voice-remove` knows how to undo the two shapes below**, so if you write your own, tell it.

A `systemd --user` timer (Linux), the shape `/voice-remove` looks for:

```sh
plugin=~/.claude/plugins/voice-loop     # wherever your checkout lives
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/voice-loop-contour.service <<EOF
[Unit]
Description=voice-loop contour poll
[Service]
Type=oneshot
ExecStart=/bin/bash $plugin/scripts/contour-poll.sh
EOF
cat > ~/.config/systemd/user/voice-loop-contour.timer <<'EOF'
[Unit]
Description=poll the voice contour every five minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now voice-loop-contour.timer
```

Or a cron line — keep the trailing comment, it is what `/voice-remove` matches on:

```cron
*/5 * * * * /bin/bash "$HOME/.claude/plugins/voice-loop/scripts/contour-poll.sh" >/dev/null 2>&1  # voice-loop contour
```

The default config (no `contour` key at all) polls just the local speech server on
`127.0.0.1:8355` — no host is ever baked in.

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
  scripts/report-bug.sh       bug-report launcher (stable entry point for /report-bug)
  scripts/report_bug.py       the collector: diagnostics -> redaction -> one bundle -> a transport
  scripts/tls-probe.sh/.py    "do https certificates verify from this python?" — names the fix, --fix runs it
  scripts/contour-poll.sh/.py the contour poller: /health + VRAM -> the alert rules -> contour.json
  skills/voice-setup/         the agent installer
  skills/voice-design/        voice casting
  skills/voice-remove/        the symmetric uninstaller (service, hotkey, config, caches, convention)
  skills/report-bug/          consent-first bug reporting
  skills/conformance/         the versioned acceptance pass — walks the checklist, fills every verdict
  CONFORMANCE.md              the checklist itself, pinned to this plugin's version
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
   Accessibility consent for `osascript` — see the macOS note below.
3. **Auto-paste on GNOME/Wayland — one root step.** Mutter exposes no virtual-keyboard protocol, so
   this needs the `ydotool` daemon. `/voice-setup` **prints** that command for you to run rather than
   running it, and says plainly what it grants.

`/voice-setup` is written for default permission mode: it announces its plan, batches its work into a
few coarse actions, and never hides a `sudo`.

### macOS Accessibility prompt — what it says, what it costs

The first time you dictate with auto-paste enabled, macOS shows a system dialog:

> **"Claude wants to control this computer"** (or the name of your terminal app)

That is the `osascript` keystroke injection requesting the Accessibility permission — **not** a
machine takeover. `/voice-setup` explains this BEFORE the dialog appears, and the dialog fires at
the first actual dictation, not during install: when it appears, you know exactly why.

- **Allow** → keystrokes work and paste is hands-free from then on.
- **Decline** → dictation still works. The text stays on your clipboard (press **Cmd+V** yourself),
  the script detects the denial in the osascript error output, and **stops retrying the keystroke
  path** — the dialog will not reappear. Switching back to auto-paste later is just removing
  `~/.local/state/voice-loop/dictate-paste-denied` (or re-granting the permission in System Settings
  → Privacy & Security → Accessibility) and the next toggle retries.

The notification the script shows on denial: *"accessibility permission denied — text is on the
clipboard"*. Every toggle after that falls back to clipboard silently — the denial is explained once,
not on every dictation.

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

## Uninstall

`/plugin uninstall voice-loop@windowsill` removes **the plugin directory and its hook
registrations** — and nothing else. Everything `/voice-setup` put *outside* the plugin stays where
it is, because it lives in your own config, state and cache directories:

| left behind by a plugin uninstall | what it is |
|---|---|
| `~/.config/voice-loop/` | `config.json`, `stress.json`, and any cloud `*.key` file |
| `~/.local/state/voice-loop/` | `speak.log`, `dictate.log`, the spoken-ledger, the recorder PID |
| `~/.local/share/voice-loop/` | the server's venv, its copy of the server, whisper.cpp models, voice previews |
| `~/.cache/torch/hub/*silero*`, `~/.cache/huggingface/hub/models--*faster-whisper*`, `~/.local/share/tts/` | downloaded model weights — in caches **shared** with other tools, so nothing wholesale-deletes them |
| `voice-loop.service` (systemd **user** unit) | the local speech server, still starting at every login |
| the dictation hotkey | a GNOME custom keybinding or an `skhd` line, now pointing at a deleted script |
| the 🔊 line in your `CLAUDE.md` | the speak convention setup offered to add |

So run **`/voice-remove` first, then the plugin uninstall** — that order matters, because
uninstalling the plugin takes the removal skill away with it. `/voice-remove` stops and disables the
user service, unbinds the hotkey, lists each cache with its size and asks per group before deleting
anything (your **key files are kept unless you say otherwise**, and the shared model caches are
never removed wholesale), takes the convention line back out of `CLAUDE.md`, and finishes by
printing what it deliberately left — shared packages like `jq`/`sox`/`skhd`, the root-installed
`ydotoold` daemon if you opted into tier 3 (its removal command is printed for you to run), and
macOS Accessibility consents you revoke by hand.

Switching backends is not an uninstall: re-run `/voice-setup`, and if you move off `local` it offers
to disable the now-idle service while keeping the venv and the weights, so switching back costs no
download.

Deeper reading: [architecture](docs/architecture.md) ·
[troubleshooting](docs/troubleshooting.md) · [FAQ](docs/faq.md).

## When it misbehaves — `/report-bug`

```text
/report-bug dictation pastes nothing since this morning
```

Claude does the log archaeology instead of you. It collects one bundle — plugin and server versions,
OS and hardware class, your config, the tails of `dictate.log` and `speak.log`, `/health` for the
endpoints your config names, the state-dir sizes and ages, and the last 20 job states — then **shows
you every byte of it in chat and asks before anything is sent**, naming where it would go.

What the collector strips before you ever see it:

- **keys and tokens** — by shape, and by config key name (`api_key` goes; `api_key_env` and
  `key_file` stay, because "which variable was consulted" is half the diagnosis);
- **you** — your username and home paths, and every host except loopback;
- **what was said.** A log line carrying speech keeps its event and loses its words:
  `transcript: <redacted 30 chars>`. The length is a diagnostic; the sentence is yours. Third-party
  output appended to the same log (a recorder's stderr, a `whisper.cpp` transcript) is withheld
  whole, and a line the collector's table does not recognise is cut rather than trusted.

Then you pick a transport: **`gh issue create`** if you have the CLI (issues are where the fixes
live), a **pre-filled new-issue URL** you submit yourself if you have a GitHub account but no CLI, or
a **mailto:** if you have neither — that last one waits on a project mailbox that does not exist yet,
and `/report-bug` says so rather than sending mail into a void. The bundle file stays in
`~/.local/state/voice-loop/` either way; if you decline, it never leaves your machine.

The collector runs standalone too, if you would rather read the bundle before Claude does:

```sh
bash plugins/voice-loop/scripts/report-bug.sh collect --summary "no sound after an update"
```

## Author

**Sahar** — AI engineer at saharkit. Designed and built live on 2026-08-01, generalized from a working
deployment.

## License

MIT — see [LICENSE](../../LICENSE).
