---
name: voice-setup
description: Install and configure the voice-loop contour on this machine — probe the OS and hardware, pick a language and speech backends (local, LAN, or cloud), install dependencies in user space, write ~/.config/voice-loop/config.json, wire a push-to-talk hotkey, and prove it works with the hardware-free loopback selftest. Use when the user asks to set up voice, dictation, speak-back, text-to-speech or speech-to-text for Claude Code.
argument-hint: "[local|lan|cloud] [language]"
allowed-tools: [Bash, Read, Write, Edit, Glob, AskUserQuestion]
---

# voice-setup — install the voice contour

You are installing a two-way voice loop for Claude Code:

- **out**: a Stop hook speaks the assistant's marker-tagged lines (default marker `🔊`);
- **in**: a push-to-talk script records, transcribes, and pastes the text into the focused window.

Both read one config file: `~/.config/voice-loop/config.json`. Your job is to produce that file, make
the pieces it names actually exist, and finish with a **passing selftest**. Scripts live at
`${CLAUDE_PLUGIN_ROOT}/scripts/`.

## Operating rules

1. **Announce the plan first, then batch.** The user may be in default permission mode where every
   Bash call is a prompt. Target **≤3 permission prompts** for the whole install: one probe batch, one
   install batch, one verify batch. Chain commands with `&&` inside a single call instead of issuing
   many small ones.
2. **No root by default.** Everything below runs in user space. The only steps that need `sudo` are
   optional luxuries — for those you **print the exact command and let the user run it** (or approve
   it explicitly). Never slip a `sudo` into a batch.
3. **Never write a secret into the config.** Cloud keys go in a file the config *points at*
   (`key_file`) or in an environment variable the config *names* (`api_key_env`).
4. **Ask, do not assume, but pre-answer.** Every question below carries a derived default. Present the
   default and let the user confirm with one keystroke.
5. **Every step writes a durable checkpoint.** The install ledger at
   `~/.local/state/voice-loop/install.ledger` is the ground truth of what has been done. After every
   step that completes successfully, write a checkpoint marker — so an interruption never loses
   progress. Before a step with side effects (installing packages, writing config, binding a hotkey),
   mark the step as *in flight* so re-entry knows it might be partial and re-runs it from scratch
   rather than trusting half-done work.

## Entry — check for existing install (deterministic lifecycle)

Before anything else, check whether an install was already started or completed:

```sh
LEDGER_CMD="${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py"
python3 "$LEDGER_CMD" check
```

The exit code and JSON tell you the state:

**Exit 0, `{"state": "none"}`** — fresh install. Create the ledger and proceed:

```sh
python3 "$LEDGER_CMD" start
```

Proceed to Step 0.

**Exit 1, `{"state": "in_progress", "completed_steps": [...], "next_step": "...", "current_step": "..."}`** — a previous install was interrupted. Present the three choices:

> A previous install was interrupted.
> Steps completed: <names>. Step in flight: <name>.
>
> 1. **Resume** — continue from the next pending step (`next_step`). Steps already done are skipped.
> 2. **Restart** — clean slate. First undo what the completed steps did (reverse order: remove
>    hotkey bindings, remove the CLAUDE.md line, remove config, disable service, delete copied
>    server files — the list of completed steps in the ledger is your cleanup guide; undo while
>    the ledger still exists so you know exactly what to revert), then run `reset` to delete the
>    ledger, then run `start` to create a fresh ledger and proceed from Step 0.
> 3. **Cancel** — leave everything as-is and exit. Run `python3 "$LEDGER_CMD" cancel` and stop.

If they pick **resume**: look at `completed_steps` and `next_step`. Skip every step whose id is in
`completed_steps`. Start at `next_step`. Do NOT run `start` — the existing ledger stays. If
`current_step` is set (a step was begun but not finished), re-run that step from the beginning
because it might be in a partial state (half-installed packages, half-written config).

If they pick **restart**: undo the completed work using the list you just read (the ledger is
your cleanup guide — undo while it still exists), then run `python3 "$LEDGER_CMD" reset` to
delete the ledger, then run `python3 "$LEDGER_CMD" start` and proceed from Step 0.

If they pick **cancel**: run `python3 "$LEDGER_CMD" cancel` and stop. Say that re-running
`/voice-setup` will offer the same three choices.

**Exit 0, `{"state": "complete"}`** — install is already done. Verify idempotently:

- `jq . ~/.config/voice-loop/config.json` — valid config?
- If local backend on Linux: `systemctl --user is-active voice-loop.service` — running?
- If everything is in order: say *"voice-loop is already installed and running — nothing to do"* and
  skip to the verification check at Step 8. This is a no-op diff, not a re-do.
- If something is missing: name it and ask whether to repair (re-run only the affected steps).

**Exit 0, `{"state": "cancelled"}`** — the user previously chose cancel. Cancelled is terminal
and carries no `next_step`, so **Resume is not on the menu** — offer **start fresh** only: run
`python3 "$LEDGER_CMD" start` (it auto-restarts from cancelled — a fresh ledger, no `reset`
needed) and proceed from Step 0. The old run's completed steps are discarded, not resumed, so
verify the environment idempotently as you go rather than trusting the old ledger.

**If `install_ledger.py` is not found** (the checkout predates the ledger): say so and proceed
without checkpoints. The install still works — it just won't survive an interruption.

### Per-step checkpoint discipline

After every step that completes (including on resume), write the checkpoint marker:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done <step-id>
```

Before a step that has irreversible side effects (3, 4, 5, 6, 7), mark it as in flight:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-begin <step-id>
```

The step ids match the flow below: `step-0-probe`, `step-1-language`, `step-2-backends`,
`step-3-install-deps`, `step-4-write-config`, `step-5-paste-behaviour`, `step-6-hotkey`,
`step-7-speak-convention`, `step-8-verify`.

If any step fails and you cannot recover, **do not write its checkpoint**. The ledger stays at the
last completed step, and the next run will pick up from there. Never write `step-done` for work
that did not succeed — a false positive in the ledger is worse than redoing the step.

### Esc / Ctrl-C during a prompt

If the user presses Esc (or Ctrl-C) during an interactive prompt (AskUserQuestion), the prompt's
step did not complete. Do not write its checkpoint. The ledger stays at the last safe step. If the
step was marked `step-begin`, leave it — re-entry will see `current_step` and re-run it from
scratch, which is the safe default for a step that might have been half-done.

## Step 0 — probe (one batch)

Run a single command that gathers everything you need:

```sh
uname -s; uname -m; echo "LANG=$LANG LC_ALL=$LC_ALL XDG_SESSION_TYPE=$XDG_SESSION_TYPE XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP"; \
for c in jq curl python3 pw-record arecord aplay paplay ffmpeg wl-copy xclip ydotool wtype xdotool notify-send gsettings sox rec afplay pbcopy osascript say brew skhd nvidia-smi; do \
  command -v "$c" >/dev/null 2>&1 && echo "have $c"; done; \
pgrep -qx TouchBarServer 2>/dev/null && echo "have touchbar"; \
python3 -c 'import sys; print("python", sys.version.split()[0], "from", sys.base_prefix)' 2>/dev/null; \
(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true); \
cat ~/.config/voice-loop/config.json 2>/dev/null || echo "no existing config"
```

From the output decide: OS branch (Linux / macOS), session type (Wayland / X11), desktop (GNOME /
KDE / other), whether a GPU exists, and what is already installed. Report the findings in two or three
lines — not a wall of text.

A `base_prefix` under `/Library/Frameworks/Python.framework/` is **python.org-installer Python**, and
it comes with an empty certificate store — see the TLS probe at the top of the macOS branch of Step 3.

`have touchbar` means this Mac's function row is the virtual Touch Bar strip (`TouchBarServer` runs
on that hardware and nowhere else) — it decides the hotkey in Step 6. It is a positive signal only:
its absence on macOS means "probably not, ask" rather than "definitely not".

**Checkpoint** — after the probe batch succeeds:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-0-probe
```

## Step 1 — language (ASK FIRST — it shapes every later option)

Derive the default from `$LC_ALL` / `$LANG` (`ru_RU.UTF-8` → `ru`), falling back to `en`.

Ask **two** questions in one batch — speech pattern FIRST (it changes what `stt.language` Step 4
will write, so it must be known before the language picker; Step 2's backend then determines the
value). State the speech-pattern options in the **user's** terms; do not name vendor tokens:

**Q1 — Speech pattern:**

> - **Single-language** — mostly one language, you stay in it.
> - **Mixed: mostly LANG with English terms** — code, technical jargon, names, product names.
>   Anyone who codes and does not speak English natively dictates in this register.

**Q2 — Voice language:**

> Voice language: **Russian**? (confirm, or pick another: ru, uk, tr, en, de, es, fr — recognition
> also works for many more languages than local synthesis does)

### What "mixed" actually writes (the per-backend translation)

Step 1 runs BEFORE the backend is chosen, so the user states an **intent**; Step 4 translates it to
`stt.language` per the backend Step 2 selected. The user never types a vendor token — the skill
maps it. Honest about where the mapping is solid and where it is not:

| Backend (Step 2) | Mixed-speech `stt.language` value | Verified how |
|---|---|---|
| **Local whisper** (`local` / `lan`) | `""` (empty) — the local server passes an empty `language` to faster-whisper, which then auto-detects per segment. For recurring jargon, set `stt.prompt` (see the seeding offer below) — the client sends it as `?prompt=`, which the server feeds to faster-whisper as `initial_prompt`; the server-wide `VOICE_LOOP_STT_HINT` is the fallback when a client sends none, so a local user sets it in `config.json` rather than editing a systemd unit. | **VERIFIED** — `server/voice_server.py` reads `VOICE_LOOP_LANGUAGE` and passes it to faster-whisper; an empty value is the documented auto-detect path; the `/stt` `?prompt=` query wins over `VOICE_LOOP_STT_HINT` as `initial_prompt` (`tests/test_api.py`). |
| **ElevenLabs Scribe** (`cloud`) | `""` (empty) — Scribe's `language_code` form field is dropped, which asks Scribe to auto-detect. | **VERIFIED** — `scripts/providers.py` omits the field when `s["language"]` is falsy; `tests/test_providers.py::test_an_empty_language_leaves_scribe_to_auto_detect` asserts `language_code` is absent from the body. |
| **Deepgram nova-3** (`cloud`) | `"multi"` — Deepgram's nova-3 multilingual code-switching mode. **Trade-off for Ukrainian:** nova-3 multilingual does NOT cover Ukrainian; Ukrainian needs `stt.model: "nova-2"`, which then loses mixed-speech support. State that plainly if the user is Ukrainian, and point at local whisper or ElevenLabs as alternatives. | **VERIFIED** — `PROVIDERS.md` documents the `"multi"` token and the `nova-2` Ukrainian fallback; the request builder sends whatever `stt.language` is as a query parameter. |
| **OpenAI whisper-1** (`cloud`) | Keep `stt.language` as the dominant language (no special value) AND seed `stt.prompt` with the user's English terms (see the seeding offer below). Whisper-1 with `language=ru` is reasonably robust to occasional English terms, and `stt.prompt` is the documented jargon-priming lever — now wired: priming with the neighbouring terms recovered *signed* and *little endian* verbatim where the bare model wrote *Sighted* / *Little Indian*. | **VERIFIED** — `scripts/providers.py::_openai_stt` sends `stt.prompt` as the `prompt` form field (omitted when empty, truncated to a token-safe budget); `tests/test_providers.py` pins the builder and `tests/test_dictate.py` pins it through `resolve_settings`. |

**Cost (say it in one sentence):** A multilingual model is usually slightly weaker on pure
single-language speech than a pinned one. The choice is informed, not free — the plugin will write
what you asked for, not what it guesses.

### Seeding `stt.prompt` (only when the user picked Mixed)

The user just said "I mix in English terms" — that is the exact moment to ask which terms, in the
**user's** register (not the vendor's):

> You mentioned you mix in English terms. Want to list the ones you use most — product names,
> commands, jargon — so recognition leans toward them? Optional; free text, comma-separated, and
> you can edit it later.

Write their answer to `stt.prompt`. One key reaches both paths, and no provider gets a promise that
was not measured:

- **OpenAI cloud** — rides as the API's `prompt` field (the measured jargon lever).
- **Local / LAN whisper** — the client sends it as `?prompt=`, which the server feeds to
  faster-whisper's `initial_prompt`. No systemd edit: `VOICE_LOOP_STT_HINT` is now the server-wide
  *fallback*, not the only way in.
- **ElevenLabs Scribe** — **not sent**; Scribe was measured to handle mixed speech correctly without
  it, so do not promise an effect (the key is simply ignored on this backend).
- **Deepgram** — **not sent**; keyterm prompting is model-specific and is not wired here.

Keep it optional: an empty answer writes nothing (the key stays absent). Truncation to the OpenAI
API's token cap is automatic — do not ask the user to count. Say in one line that the list lives in
`config.json` under `stt.prompt` and can be edited any time.

### Recognition — the old line was misleading

The previous Step 1 said *"Recognition (whisper) is multilingual."* That is true on the **local**
whisper server (faster-whisper auto-detects when `stt.language` is empty) and **misleading on
cloud** — every cloud provider here pins recognition to `stt.language`, so the choice matters
more on the cloud path, and mixed speech needs saying so. The table above is the replacement.

Local synthesis (Silero) currently covers `ru, uk, en, de, es, fr`; Turkish uses the local XTTS-v2
cloned-voice engine when configured. Recognition still works for many more languages than synthesis
does. If the user names a language outside the synthesis list, say so plainly and offer: cloud TTS,
or the macOS built-in `say` voice, or recognition-only (dictation without speak-back).

*Advanced, mention only if asked or if the user hints at it:* dictation and speak-back may use
different languages — that is just `stt.language` / `tts.language` next to the top-level `language`.

**Checkpoint** — after the language choice is confirmed:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-1-language
```

## Step 2 — backends (one question, defaults pre-picked)

Each direction independently is `local`, `lan`, or `cloud`:

| | what it is | cost | privacy | latency |
|---|---|---|---|---|
| `local` | models on this machine | free, uses your CPU/GPU | audio never leaves the machine | depends on hardware |
| `lan` | a `voice-loop` server on another box you own | free | stays on your network | fast if that box has a GPU |
| `cloud` | a hosted speech API | per-use billing | **audio leaves your machine** | fast |

Default recommendation:
- a discrete GPU or Apple Silicon → `local` for both;
- a thin laptop and a GPU box on the network → `lan` for both (offer the ssh-tunnel form:
  `ssh -N -L 8355:127.0.0.1:8355 user@host`, endpoint stays `http://127.0.0.1:8355`);
- neither, and the user accepts the tradeoff → `cloud`, **stated explicitly**: "your microphone audio
  and the spoken text will be sent to <provider>".

**Honest resource note to give the user for `local`:** whisper `small` needs roughly 2 GB of RAM and
transcribes a short phrase in a couple of seconds on a modern CPU; `base`/`tiny` are faster and less
accurate. Silero TTS is CPU-friendly (near real-time). First run downloads roughly 0.5–1.5 GB of
models. Say this **before** installing, not after.

**If an ElevenLabs key is already configured** (the probe printed an existing config with
`tts.cloud.provider: "elevenlabs"` or the env var `VOICE_LOOP_TTS_API_KEY` is set): when
dictation goes cloud, offer ElevenLabs Scribe as the STT provider (`stt.cloud.provider:
"elevenlabs"`). The same key covers both TTS and STT — no second key needed. Say plainly that
cloud STT sends recorded audio clips to ElevenLabs' servers (the privacy row above covers it),
and that if the cloud call fails the script degrades to local whisper automatically (the
microphone never goes dead).

### Switching away from `local` on a re-run (clean up behind the old choice)

Step 0 printed the existing config, so you know what the previous run chose. If a direction *was*
`local`, **neither** direction is `local` now, and the probe found the user unit
(`systemctl --user is-enabled voice-loop.service`), then that server keeps starting at every login
and holding its models in RAM for nothing. Say that plainly and offer, with disabling as the
default:

```sh
systemctl --user disable --now voice-loop.service
```

Stop **and** disable — a stop alone comes back at the next login. Leave the unit file, the venv and
the model caches where they are: they are the expensive part, and switching back later is then one
`systemctl --user enable --now voice-loop.service` rather than another 0.5–1.5 GB download. Tell the
user that is what you left, and that `/voice-remove` is what deletes it.

Nothing else about a backend switch needs cleaning: a `cloud` → `local` move leaves the key file
alone (it is the user's secret, and `/voice-remove` is where it gets removed on purpose), and `lan`
leaves nothing on this machine at all. On macOS there is no unit to disable — the server, if the
user ran one, was started by hand.

**Checkpoint** — after backends are confirmed:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-2-backends
```

## Step 3 — install dependencies (one batch, user space only)

**Begin step** — this step has side effects (packages, venv, service). Mark it in flight:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-begin step-3-install-deps
```

### Linux

```sh
# packages the user may already have — list what is MISSING and ask once, printing the exact line:
#   sudo apt install jq curl pipewire-utils alsa-utils wl-clipboard libsndfile1   # (or dnf/pacman)
```
This is the one place a `sudo` line may appear — **printed for the user to run**, never executed by you.

Server (only for `local`) — needs **Python >= 3.10**; check `python3 --version` from the probe first
and, if the system python is older, use a newer interpreter explicitly (`python3.12 -m venv …`).

The server lives **inside the plugin directory** (`plugins/voice-loop/server/`), so on a marketplace
install its files are already on this machine — copy them, no download:

```sh
mkdir -p ~/.local/share/voice-loop && \
cp "${CLAUDE_PLUGIN_ROOT}/server/voice_server.py" \
   "${CLAUDE_PLUGIN_ROOT}/server/requirements.txt" \
   "${CLAUDE_PLUGIN_ROOT}/server/stt_hallucinations.txt" ~/.local/share/voice-loop/
```

If that path is not there (no plugin root — e.g. you are working from a bare checkout), take the
files from the repo instead. Either clone it:

```sh
git clone --depth 1 https://github.com/saharkit/windowsill ~/.local/share/voice-loop/src && \
cp ~/.local/share/voice-loop/src/plugins/voice-loop/server/voice_server.py \
   ~/.local/share/voice-loop/src/plugins/voice-loop/server/stt_hallucinations.txt ~/.local/share/voice-loop/
```

or fetch the three files by raw URL:

```sh
# REF=main — this repo's layout is plugin-scoped and stable; substitute a tag or a commit sha here
# if you want a byte-exact pin of the server you install.
REF=main
mkdir -p ~/.local/share/voice-loop && \
curl -fsSL "https://raw.githubusercontent.com/saharkit/windowsill/$REF/plugins/voice-loop/server/voice_server.py" \
  -o ~/.local/share/voice-loop/voice_server.py && \
curl -fsSL "https://raw.githubusercontent.com/saharkit/windowsill/$REF/plugins/voice-loop/server/requirements.txt" \
  -o ~/.local/share/voice-loop/requirements.txt && \
curl -fsSL "https://raw.githubusercontent.com/saharkit/windowsill/$REF/plugins/voice-loop/server/stt_hallucinations.txt" \
  -o ~/.local/share/voice-loop/stt_hallucinations.txt
```

Then build the venv (with the clone,
`-r ~/.local/share/voice-loop/src/plugins/voice-loop/server/requirements.txt`):

```sh
python3 -m venv ~/.local/share/voice-loop/venv && \
~/.local/share/voice-loop/venv/bin/pip install --quiet --index-url https://download.pytorch.org/whl/cpu torch && \
~/.local/share/voice-loop/venv/bin/pip install --quiet -r ~/.local/share/voice-loop/requirements.txt
```

Start it as a user service (no root):

```sh
mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/voice-loop.service <<'EOF'
[Unit]
Description=voice-loop speech server
[Service]
Environment=VOICE_LOOP_STT_MODEL=small
Environment=VOICE_LOOP_LANGUAGE=<language>
ExecStart=%h/.local/share/voice-loop/venv/bin/python %h/.local/share/voice-loop/voice_server.py
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now voice-loop.service
```

Substitute `<language>` with the code the user chose in Step 1 (it sets the server's default;
requests can still override per call). `systemctl --user` is not root.

### macOS

**First, before anything that talks HTTPS — probe TLS.** A Mac running python.org-installer Python
has an **empty certificate store** until `/Applications/Python 3.x/Install Certificates.command` has
been run once, and nothing runs it for the user. Until then every https call from that interpreter
dies with `SSL: CERTIFICATE_VERIFY_FAILED — unable to get local issuer certificate`: `pip install`,
the model download, and the cloud TTS request the speak hook makes. The failure is loud and names no
fix, so a perfectly good install reads as a broken plugin. Probe for it *first*, because everything
below it needs the network:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/tls-probe.sh"
```

Exit 0 means certificates verify — say one line and move on. Exit 1 is the trap, and the probe's own
message already names the exact fix. Exit 2 means the host was unreachable, which is a network
problem, not a certificate one — do not "fix" it. Exit **64** means the probe was called wrong (a
typo'd flag, a missing value, a non-https URL): correct the command, and do not report it to the
user as a certificate problem — it is not a diagnosis of their machine at all.

On exit 1, when the fix is the installer's own command (the probe says so, and says it is runnable),
**ask before running it** — it is a one-line AskUserQuestion, not a silent action:

> Your Python's certificate store is empty (python.org installer). Run its own
> `Install Certificates.command` now? It needs no root and only affects this Python.

On yes, re-run with `--fix`: it runs that command and **probes again**, so what you report is a
verified green rather than "we ran something".

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/tls-probe.sh" --fix
```

On no — or when the probe printed a fix it will not run for you (a shell pipeline, a package
manager) — print its command verbatim, say the install cannot finish until it is run, and stop
there rather than starting a `pip install` that is going to fail the same way.

Two things the probe does not cover, and both are one line to the user rather than a silent
assumption. It checks the `python3` on `PATH`, which is what the hooks run under: if you build a
**venv** for the local server (Step 3 below), that venv inherits its base interpreter's store, so
when the venv's base is a *different* python, probe it too by calling the `.py` with that
interpreter. And a green while a proxy is configured is a green for *this* probe only — it bypasses
proxies on purpose, `pip` and the model download do not, and the probe says so in that case.

```sh
~/.local/share/voice-loop/venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/tls-probe.py" --fix
```

Most of the contour is built in: `afplay` (play), `pbcopy` (clipboard), `osascript` (keystroke),
`say` (a decent built-in TTS fallback: `say -v Milena` for Russian, `say -v Lesya` for Ukrainian —
check availability with `say -v '?'`). Missing pieces come from Homebrew, in user space:

```sh
brew install jq sox            # sox gives you `rec` for recording
```

For `local` on Apple Silicon, prefer **whisper.cpp with Metal** over faster-whisper — it is markedly
faster on this hardware (`brew install whisper-cpp`, then set `stt.command` to a whisper.cpp
invocation that prints the transcript, e.g.
`whisper-cli -m ~/.local/share/voice-loop/ggml-small.bin -l ru -nt -f`). For TTS the zero-dependency
path is `tts.command: "say -v Milena"`; the Silero server path also works if the user wants the same
voice as their Linux machines.

**Checkpoint** — after all dependencies are installed and the service (if local) is running:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-3-install-deps
```

### Step 3b — acoustic echo cancellation (Linux / PipeWire, no root)

On a full-duplex dictation loop the assistant's TTS leaks speaker→mic into the transcript verbatim —
the raw mic has no echo protection, so the assistant's own replies are dictated back into the prompt.
The fix is local: PipeWire's `libpipewire-module-echo-cancel` (WebRTC AEC).

This step is **Linux-only** and **idempotent** — running it over an existing config leaves the right
thing. Probe first: if `pw-cli` is not on PATH, PipeWire is not the audio system (or `pipewire-utils`
is not installed). In that case skip to Step 4. Otherwise:

```sh
# Write the module config fragment — idempotent, one file.
mkdir -p ~/.config/pipewire/pipewire.conf.d
cat > ~/.config/pipewire/pipewire.conf.d/voice-loop-echo-cancel.conf <<'PIPEWIRE_EOF'
# voice-loop: acoustic echo cancellation — subtract the assistant's TTS from the mic signal.
# Loaded live by PipeWire on restart; creates Echo-Cancel Sink and Echo-Cancel Source nodes
# auto-linked to the default sink and mic.  The speak hook routes TTS into the sink; dictation
# records from the source.  Remove this file and restart PipeWire to disable.
context.modules = [
    {   name = libpipewire-module-echo-cancel
        args = {
            aec.method = "webrtc"
            source.props = {
                node.name = "Echo-Cancel Source"
                node.description = "voice-loop Echo-Cancel Source (AEC)"
            }
            sink.props = {
                node.name = "Echo-Cancel Sink"
                node.description = "voice-loop Echo-Cancel Sink (AEC)"
            }
        }
    }
]
PIPEWIRE_EOF
```

Verify the module loads (does not require a PipeWire restart — `pw-cli -m load-module` loads it
live, but the persistent config above takes effect on the next restart):

```sh
pw-cli -m load-module libpipewire-module-echo-cancel 2>&1 || true
```

A "No such module" or "File not found" is an old PipeWire or a distro that ships the module
separately (`pipewire-audio-client-libraries` on some apt distros). Say what was missing and that
the config file is already written — it takes effect when the module becomes available.

If the load succeeds, verify the nodes appeared:

```sh
pw-cli list-objects 2>/dev/null | grep -A5 'Echo-Cancel'
```

Then set the config keys that route the hooks into the cancel path:

```jsonc
// Add to ~/.config/voice-loop/config.json:
"speak": { "sink": "Echo-Cancel Sink" },
"dictate": { "source": "Echo-Cancel Source" }
```

Use `jq` to merge these keys into the existing config file rather than overwriting it — the config
may already exist at this point (a re-run, a repair). If Step 4 has not yet written the file, write
these keys into the config that Step 4 will produce instead.

The degrade paths are all built in: `pw-record --target` on a missing node falls back to the default
source (the AEC module absent does not block dictation), and the speak hook's sink routing degrades
to the default device when the named sink is not present.

Tell the user in one line: *Echo cancellation is configured. Restart PipeWire to activate it
(`systemctl --user restart pipewire`), or just reboot — the config loads on every start.*  The live
module load above is temporary and resets on restart; the config file makes it permanent.

## Step 4 — write the config

**Begin step** — writing config is a side effect (the file is the product). Mark it in flight:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-begin step-4-write-config
```

Write `~/.config/voice-loop/config.json` (create the directory; `chmod 600` if a `key_file` is
referenced). Full schema — omit what you do not need, the scripts have defaults for everything:

```json
{
  "language": "ru",
  "stt": {
    "backend": "lan",
    "endpoint": "http://127.0.0.1:8355",
    "language": "ru",
    "model": "whisper-1",
    "command": "",
    "timeout": 60,
    "cloud": {
      "provider": "openai",
      "endpoint": "",
      "api_key_env": "VOICE_LOOP_STT_API_KEY",
      "key_file": ""
    }
  },
  "tts": {
    "backend": "lan",
    "endpoint": "http://127.0.0.1:8355",
    "language": "ru",
    "speaker": "",
    "command": "",
    "cloud": {
      "provider": "elevenlabs",
      "voice_id": "",
      "model": "eleven_multilingual_v2",
      "output_format": "mp3_44100_128",
      "voice_settings": { "stability": 0.7, "similarity_boost": 0.8, "style": 0.1, "use_speaker_boost": true },
      "api_key_env": "VOICE_LOOP_TTS_API_KEY",
      "key_file": "~/.config/voice-loop/elevenlabs.key"
    }
  },
  "speak": {
    "enabled": true,
    "marker": "🔊",
    "player": "aplay -q",
    "max_chars": 600,
    "timeout": 60
  },
  "dictate": {
    "mode": "send",
    "paste_key": "ctrl+shift+v",
    "auto_paste": false,
    "paste_target": "any",
    "recorder": "auto",
    "clipboard": "auto",
    "debounce_ms": 750,
    "start_sound": "",
    "stop_sound": ""
  }
}
```

Field notes worth telling the user:
- `speak.marker` — only lines starting with it are voiced. Step 7 offers to add the matching
  one-line convention to the user's `CLAUDE.md`, so it gets used consistently — and if they picked a
  different marker, the convention line uses theirs.
- `speak.player` — `aplay -q` (Linux), `afplay` (macOS), `mpg123 -q` / `ffplay -autoexit -nodisp
  -loglevel quiet` if a cloud provider returns mp3.
- `dictate.paste_key` — must match the target app: terminals usually `ctrl+shift+v` or `shift+insert`,
  macOS `cmd+v`.
- `dictate.paste_target` — `"any"` (default) or `"same-window"`. Auto-paste lands the text in
  whatever window is focused when the recording **stops** — that is system-wide dictation, and it is
  also how a sentence meant for the agent ends up in a chat window if the user switches mid-speech.
  Say both halves when you set up tier 2/3; do not talk anyone into the guard. `"same-window"`
  remembers the window focused at **start** and, if focus moved, pastes nothing — the text stays on
  the clipboard with a "focus moved" notification. It applies only when `auto_paste` is true, the
  identity is the frontmost *application* on macOS (`osascript`) and the active *window* on X11
  (`xdotool`), and **on Wayland there is no portable query at all**, so it degrades to `"any"` — do
  not offer it there as if it worked. An unrecognised value resolves to `"same-window"`, on the
  grounds that only somebody asking for the guard writes the key in the first place.
- `dictate.debounce_ms` — the key-repeat guard. A *held* hotkey autorepeats, and without this every
  second fire would stop a recording milliseconds old ("clip too short" in the log, nothing ever
  transcribed). The window restarts on every fire, dropped ones included, so a hold is ONE toggle
  however long it lasts and clears one window after release. 750 ms is the default — chosen to
  exceed the longest common key-repeat *delay* (X11's 660 ms; GNOME 500, macOS 375), because a
  window shorter than that delay lets the first repeat through — and there is rarely a reason to
  touch it. `0` (or any negative value) turns the guard off. Raise it only for a keyboard whose
  repeat delay is longer still; a value that large starts eating deliberate quick taps.
- `tts.command` / `stt.command` — the escape hatch for engines that are not HTTP servers (`say`,
  whisper.cpp). `tts.command` receives text on stdin and makes the sound itself; `stt.command`
  receives the WAV path as its last argument and prints the transcript.
- `stt.cloud.provider` / `tts.cloud.provider` — which hosted API each direction talks to. Today:
  `"openai"` (the default both ways, OpenAI-compatible), `"elevenlabs"` (Scribe for STT, and the
  provider `/voice-design` casts voices with), `"deepgram"` (Nova for STT, Aura for TTS). The two
  directions are independent — recognition through one provider and synthesis through another is an
  ordinary config. **Do not memorise this list**: it is a registry, and
  `plugins/voice-loop/PROVIDERS.md` is the current table with latency, cost per minute, language
  coverage and privacy posture — read it with the user rather than quoting figures from here.
  - The model default follows the provider (`whisper-1`, `scribe_v1`, `nova-3`), so leave
    `stt.model` unset unless the user wants a specific one.
  - **Language first, price second.** Deepgram's *synthesis* is English (and Spanish) only — never
    offer `tts.cloud.provider: "deepgram"` to a user whose `language` is `ru` or `uk`. Its
    *recognition* covers Russian (with `stt.language: "multi"`), and Ukrainian only on
    `stt.model: "nova-2"`.
  - **`stt.language` for mixed speech (the Step 1 mapping).** If the user picked **mixed** in Step 1,
    write `stt.language` per the table there — `""` (empty) for `local` / `lan` / ElevenLabs
    Scribe; `"multi"` for Deepgram nova-3; the dominant language for OpenAI whisper-1 (no special
    value). Then write the user's terms to **`stt.prompt`** if they gave any (see the Step 1 seeding
    offer) — one key primes OpenAI's `prompt` field and the local whisper `initial_prompt` alike.
    The top-level `language` (TTS voice selection) stays as the dominant language either way. Say the
    value you wrote and why, in one line, so a user who later switches backend knows to revisit this.
  - When the user already has an ElevenLabs key configured for TTS (`VOICE_LOOP_TTS_API_KEY`),
    offer `elevenlabs` for STT as the natural choice — the same key covers both directions with no
    extra setup. That shared-key rule is ElevenLabs' STT rule alone; every other provider needs its
    own key, and switching `tts.cloud.provider` means pointing `tts.cloud.api_key_env` at that
    provider's own key.
  - Say plainly that cloud STT sends the recorded audio clip to the provider's servers — that is
    the privacy trade every cloud backend makes, and it is stated explicitly in the plugin README.
  - If the cloud call fails (network down, quota, expired key) dictation **degrades to the local
    whisper server** with a log line rather than going dead — nothing about the config changes, and
    the next clip tries the cloud again. One hop, not a cascade across providers.
  - A provider name the plugin does not know falls back to `"openai"` and says so in the log — so
    if dictation is quietly OpenAI-shaped when the user asked for something else, check the spelling
    in `~/.local/state/voice-loop/dictate.log` first.

**Checkpoint** — after the config file is written and valid (`jq .` parses it):

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-4-write-config
```

## Step 5 — paste behaviour (the permission ladder — DEFAULT IS THE NO-ROOT TIER)

**Begin step** — configuring paste behaviour has side effects (config file is modified). Mark it in flight:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-begin step-5-paste-behaviour
```

**Tier 1 (default, zero root, works everywhere):** `auto_paste: false`. The transcript lands on the
clipboard and a notification says "copied — press <paste_key>". The user presses their own paste key.
Set this up first and *demonstrate it working* before offering anything else.

**Tier 2 (opt-in, still no root):**
- KDE / wlroots compositors: `wtype` — pure userland. `auto_paste: true` and you are done.
- X11: `xdotool` — pure userland.
- macOS: `osascript` keystroke. Costs **one Accessibility consent** for the terminal app the hotkey
  runs under (System Settings → Privacy & Security → Accessibility). No root.

**On macOS, ask about auto-paste explicitly — and explain the dialog BEFORE it appears.** The
question to ask, in its own turn so the user sees it before any consent dialog fires:

> Auto-paste sends keystrokes through System Events, which requires the **Accessibility** permission.
> The first time you dictate, macOS will show **"Claude wants to control this computer"** (or the
> name of your terminal app) — that is *this* feature, not a machine takeover. The dialog appears at
> the FIRST actual dictation, not during setup. Do you want auto-paste?
>
> - **Allow** in that dialog → keystrokes work and paste is hands-free.
> - **Decline** → dictation still works perfectly; the text stays on your clipboard and you press
>   **Cmd+V** yourself. The script detects the denial and stops retrying the keystroke path, so the
>   dialog does not reappear.
>
> The default is clipboard-only — say "no" and you never see the dialog at all.

When the user says yes, set `auto_paste: true` and say one sentence more — "the prompt will appear
the first time you actually dictate, not now" — so they are not startled when the dialog pops up
mid-sentence later. When they say no, keep the default tier 1 and move on.

**Tier 3 (opt-in, needs root ONCE — GNOME/Mutter on Wayland only):** Mutter exposes no
virtual-keyboard protocol, so `wtype` cannot work there; `ydotool` needs its daemon on
`/dev/uinput`. Print these for the user to run themselves, and say what they do:

```sh
sudo apt install ydotool
sudo tee /etc/systemd/system/ydotoold.service >/dev/null <<'EOF'
[Unit]
Description=ydotool daemon
[Service]
ExecStart=/usr/bin/ydotoold --socket-path=/tmp/.ydotool_socket --socket-own=$(id -u):$(id -g)
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now ydotoold
```

State the tradeoff honestly: this grants a background daemon the ability to synthesize input events.
If the user declines, tier 1 remains fully functional — that is the point of the ladder.

**Whenever you turn tier 2 or 3 on, say what auto-paste actually does**, in one sentence and without
drama: the text is pasted into whatever window is focused when they stop the recording, so switching
windows mid-sentence sends it there instead — useful (it works in every app) and occasionally
expensive (a private line into a public channel). Then mention `dictate.paste_target: "same-window"`
as available, and leave the choice with them; the default stays `"any"`. On a Wayland session, say
that the guard cannot work there rather than offering it.

Note for tier 2/3 alike: older `ydotool` only accepts **named key combos** and cannot type non-ASCII,
which is exactly why the scripts paste from the clipboard instead of typing the text.

**Checkpoint** — after paste behaviour is configured:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-5-paste-behaviour
```

## Step 6 — hotkey

**Begin step** — binding a hotkey is a side effect (the user's desktop config is modified). Mark it in flight:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-begin step-6-hotkey
```

One rule before the per-desktop recipes: **on macOS, bind a physical chord, not an F-row key.** See
the macOS subsection below for why and for the question to ask. On Linux the F-row is a real key and
`F9` remains the default.

### GNOME (gsettings, no root)

```sh
KEY=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-loop/
# read the existing list and APPEND — a plain `set` would clobber the user's other custom keybindings
CUR=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)
NEW=$(python3 -c "
import ast, sys
cur, key = sys.argv[1], sys.argv[2]
lst = [] if cur.strip() in ('@as []', '[]') else ast.literal_eval(cur)
if key not in lst: lst.append(key)
print(lst)" "$CUR" "$KEY")
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$NEW"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY name 'voice-loop dictate'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY command "$HOME/.local/bin/voice-loop-dictate send"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY binding 'F9'
```

Install the stable launcher before binding the key. It resolves the current version from Claude Code's
installed-plugin registry at each press, so the binding never contains a version-scoped cache path:

```sh
mkdir -p "$HOME/.local/bin" && \
install -m 755 "${CLAUDE_PLUGIN_ROOT}/scripts/voice-loop-dictate" "$HOME/.local/bin/voice-loop-dictate"
```

The launcher is a Python entry point; the desktop invokes it through the shebang, so no shell
expansion or plugin-root variable is needed. It is deliberately installed outside the plugin cache. Do not replace it with a glob over
`~/.claude/plugins/cache`: sorting version directories is not a safe resolver. The registry is refreshed
by Claude Code during `/plugin update`, and the launcher fails closed if it cannot identify exactly one
current `voice-loop` install. If a previous binding contains an absolute versioned
`dictate-toggle.sh` path, replace that command with the stable launcher during this step; this migrates
existing installs in place rather than waiting for the next update to break them.

### macOS — ASK, and default to a physical chord

The function row is not a reliable binding target on a Mac. On a **Touch Bar** model it is a virtual
strip: unless *Keyboard → Function Keys → "F1, F2, etc. keys always shown in Touch Bar"* is on, `F9`
only exists while `fn` is held, so a plain `f9` binding can receive **no keycode at all** and the
hotkey silently does nothing. A chord like ⌘I is a real key combination on every Mac.

Take the default from the Step 0 probe (`have touchbar` → chord, no question needed beyond a
confirm) and ask **one** question either way — the probe only sees the machine you are running on:

> Dictation hotkey: **⌘I** (a physical chord — works on every Mac, Touch Bar or not)? Or an F-row key
> (`F9`), which on a Touch Bar Mac needs `fn` held?

`skhd` — Homebrew, user space, no root:

```sh
brew install skhd && \
mkdir -p ~/.config/skhd && \
printf 'cmd - i : %s\n' "$HOME/.local/bin/voice-loop-dictate send" >> ~/.config/skhd/skhdrc && \
skhd --start-service
```

Install the same stable launcher before adding the line (the command is identical on macOS):

```sh
mkdir -p "$HOME/.local/bin" && \
install -m 755 "${CLAUDE_PLUGIN_ROOT}/scripts/voice-loop-dictate" "$HOME/.local/bin/voice-loop-dictate"
```

The launcher resolves the current plugin version from Claude Code's installed-plugin registry at each
press. It fails closed when the registry is missing or ambiguous; it never guesses by globbing cache
folders. If `skhdrc` already contains a version-scoped absolute `dictate-toggle.sh` command, replace
that line with the stable launcher while migrating the existing install.

`skhd --restart-service` after any later edit of `skhdrc`. skhd costs **one Accessibility consent**
(System Settings → Privacy & Security → Accessibility) — the same class of grant as the tier-2 paste
in Step 5, and still no root.

Notes to give the user:
- ⌘I is *Get Info* in Finder and *italic* in editors, and skhd grabs it globally. If that bothers
  them, any other chord is the same line with a different left side — `cmd + shift - d`,
  `ctrl + alt - d`, `fn - space`.
- If they insist on the F-row on a Touch Bar Mac, the binding is `fn - f9`, not `f9` — and it fires
  only while the Touch Bar shows the function row.
- The Homebrew-free alternative is a Shortcuts.app Quick Action bound to a key. Same Fn caveat.

### Other environments — print instructions, do not guess

- **KDE**: System Settings → Shortcuts → Custom Shortcuts → new → command.
- **Sway/Hyprland**: one config line, print it ready to paste
  (`bindsym F9 exec ~/.local/bin/voice-loop-dictate send`).

Always tell the user the script is a **toggle**: press to start, press again to stop and transcribe.
Tapping is the gesture — *holding* the key does not queue up toggles, because a re-fire within
`dictate.debounce_ms` (750 ms) of the previous fire is ignored, and each ignored fire restarts that
window: a key held for ten seconds is still one toggle.

**Checkpoint** — after the hotkey is bound:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-6-hotkey
```

## Step 7 — the speak convention (the line that makes the model speak)

**Begin step** — appending to CLAUDE.md is a side effect (the user's own file is modified). Mark it in flight:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-begin step-7-speak-convention
```

The hook voices marked lines, but nothing yet tells the model to *write* them — without this line in
a `CLAUDE.md`, the plugin sits silent. Offer to add it now (AskUserQuestion, three options):

1. **Global `~/.claude/CLAUDE.md`** *(recommended default)* — every project speaks.
2. **This project's `CLAUDE.md`** — voice only here.
3. **Skip** — the user adds it themselves; point them at the Quickstart section of the plugin
   README, which carries the same one-liner.

The line, appended verbatim as its own paragraph — but substitute the marker the user actually
configured in Step 4 if they changed `speak.marker` from `🔊`:

> End each reply with a one-sentence spoken summary on its own line, starting with 🔊.

On append: create the file if it does not exist. **Never append a duplicate** — first check whether
an equivalent line is already there (grep the target file loosely for the marker and "spoken
summary"); if one exists, say so and leave the file alone.

Whichever option they pick, note that the next step's speak-back check now proves the **whole**
loop — convention included, not just the plumbing.

**Checkpoint** — after the CLAUDE.md line is appended (or confirmed already present):

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-7-speak-convention
```

## Step 8 — verify (mandatory, this is how the install ends)

How the install ends depends on the backend shape — say which proof applies **before** running it:

- **HTTP endpoints configured (local server / lan / cloud):** the ending is the loopback selftest —

  ```sh
  bash "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.sh"
  ```

  It renders a phrase through TTS, feeds the audio straight back into STT, and compares the
  transcript — no microphone, no speakers, no display. On failure, read its message: it
  distinguishes "server not reachable", "not a WAV", "no text", and "transcript does not match".
  If the endpoint is **https** and the failure mentions a certificate, that is the Step 3 TLS trap
  reaching this far — run `scripts/tls-probe.sh` and follow what it names, do not re-run the
  selftest hoping.
- **Direct-command backends only (e.g. macOS `tts.command: "say …"`, no HTTP endpoint):** there is
  nothing for the loopback to loop through — `selftest.sh` will say so and exit without testing.
  The install ends with the **ear-check** below instead; do not promise a green selftest here.

Then verify the two interactive halves with the user (for a command-only setup this IS the proof):
1. **speak-back** — end your next reply with a line starting with the marker and ask if they heard it.
2. **dictation** — ask them to press the hotkey, say a sentence, press again, and confirm the text
   arrived (pasted, or on the clipboard in tier 1).

Report at the end: language, both backends, paste tier, hotkey, and the verification result
(loopback selftest or ear-check, whichever applied). If anything is left
undone (e.g. the user declined the ydotool step), say exactly what and how to finish it later. Close
by naming the way back out: `/voice-remove` undoes everything this install touched — the service,
the hotkey, the config, the caches and the `CLAUDE.md` line.

**Checkpoint** — after verification passes:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" step-done step-8-verify
```

**Mark the install complete** — the ledger records that all steps finished:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_ledger.py" finish
```

A re-run of `/voice-setup` from here will see `{"state": "complete"}` and report "already installed"
rather than re-doing work. This is the idempotence guarantee: running the installer over a completed
install is a no-op diff.
