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

## Step 0 — probe (one batch)

Run a single command that gathers everything you need:

```sh
uname -s; uname -m; echo "LANG=$LANG LC_ALL=$LC_ALL XDG_SESSION_TYPE=$XDG_SESSION_TYPE XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP"; \
for c in jq curl python3 pw-record arecord aplay paplay ffmpeg wl-copy xclip ydotool wtype xdotool notify-send gsettings sox rec afplay pbcopy osascript say brew nvidia-smi; do \
  command -v "$c" >/dev/null 2>&1 && echo "have $c"; done; \
python3 -c 'import sys; print("python", sys.version.split()[0])' 2>/dev/null; \
(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true); \
cat ~/.config/voice-loop/config.json 2>/dev/null || echo "no existing config"
```

From the output decide: OS branch (Linux / macOS), session type (Wayland / X11), desktop (GNOME /
KDE / other), whether a GPU exists, and what is already installed. Report the findings in two or three
lines — not a wall of text.

## Step 1 — language (ASK FIRST — it shapes every later option)

Derive the default from `$LC_ALL` / `$LANG` (`ru_RU.UTF-8` → `ru`), falling back to `en`. Ask **one**
confirm-style question:

> Voice language: **Russian**? (confirm, or pick another: ru, uk, en, de, es, fr — recognition also
> works for many more languages than local synthesis does)

Local synthesis (Silero) currently covers `ru, uk, en, de, es, fr`; recognition (whisper) is
multilingual. If the user names a language outside the synthesis list, say so plainly and offer: cloud
TTS, or the macOS built-in `say` voice, or recognition-only (dictation without speak-back).

*Advanced, mention only if asked or if the user hints at it:* dictation and speak-back may use
different languages — that is just `stt.language` / `tts.language` next to the top-level `language`.

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

## Step 3 — install dependencies (one batch, user space only)

### Linux

```sh
# packages the user may already have — list what is MISSING and ask once, printing the exact line:
#   sudo apt install jq curl pipewire-utils alsa-utils wl-clipboard libsndfile1   # (or dnf/pacman)
```
This is the one place a `sudo` line may appear — **printed for the user to run**, never executed by you.

Server (only for `local`):

```sh
python3 -m venv ~/.local/share/voice-loop/venv && \
~/.local/share/voice-loop/venv/bin/pip install --quiet --index-url https://download.pytorch.org/whl/cpu torch && \
~/.local/share/voice-loop/venv/bin/pip install --quiet -r "<repo>/server/requirements.txt"
# Ukrainian only: ~/.local/share/voice-loop/venv/bin/pip install "ukrainian-word-stress>=1.0"
```

Start it as a user service (no root):

```sh
mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/voice-loop.service <<'EOF'
[Unit]
Description=voice-loop speech server
[Service]
Environment=VOICE_LOOP_STT_MODEL=small
Environment=VOICE_LOOP_LANGUAGE=ru
ExecStart=%h/.local/share/voice-loop/venv/bin/python %h/.local/share/voice-loop/voice_server.py
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now voice-loop.service
```

(Copy `server/voice_server.py` to `~/.local/share/voice-loop/` first. `systemctl --user` is not root.)

### macOS

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

## Step 4 — write the config

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
    "cloud": { "api_key_env": "VOICE_LOOP_STT_API_KEY", "key_file": "" }
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
    "recorder": "auto",
    "clipboard": "auto",
    "start_sound": "",
    "stop_sound": ""
  }
}
```

Field notes worth telling the user:
- `speak.marker` — only lines starting with it are voiced. Tell the user to ask for it in their
  `CLAUDE.md` if they want it used consistently, e.g. *"start a one-line spoken summary with 🔊"*.
- `speak.player` — `aplay -q` (Linux), `afplay` (macOS), `mpg123 -q` / `ffplay -autoexit -nodisp
  -loglevel quiet` if a cloud provider returns mp3.
- `dictate.paste_key` — must match the target app: terminals usually `ctrl+shift+v` or `shift+insert`,
  macOS `cmd+v`.
- `tts.command` / `stt.command` — the escape hatch for engines that are not HTTP servers (`say`,
  whisper.cpp). `tts.command` receives text on stdin and makes the sound itself; `stt.command`
  receives the WAV path as its last argument and prints the transcript.

## Step 5 — paste behaviour (the permission ladder — DEFAULT IS THE NO-ROOT TIER)

**Tier 1 (default, zero root, works everywhere):** `auto_paste: false`. The transcript lands on the
clipboard and a notification says "copied — press <paste_key>". The user presses their own paste key.
Set this up first and *demonstrate it working* before offering anything else.

**Tier 2 (opt-in, still no root):**
- KDE / wlroots compositors: `wtype` — pure userland. `auto_paste: true` and you are done.
- X11: `xdotool` — pure userland.
- macOS: `osascript` keystroke. Costs **one Accessibility consent** for the terminal app the hotkey
  runs under (System Settings → Privacy & Security → Accessibility). No root.

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

Note for tier 2/3 alike: older `ydotool` only accepts **named key combos** and cannot type non-ASCII,
which is exactly why the scripts paste from the clipboard instead of typing the text.

## Step 6 — hotkey

### GNOME (gsettings, no root)

```sh
KEY=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-loop/
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$KEY']"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY name 'voice-loop dictate'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY command '<PLUGIN_ROOT>/scripts/dictate-toggle.sh send'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY binding 'F9'
```

Read the existing `custom-keybindings` list first and **append** rather than overwrite it. Substitute
the real absolute path — `${CLAUDE_PLUGIN_ROOT}` is not expanded by the desktop.

### Other environments — print instructions, do not guess

- **KDE**: System Settings → Shortcuts → Custom Shortcuts → new → command.
- **Sway/Hyprland**: one config line, print it ready to paste
  (`bindsym F9 exec /path/to/dictate-toggle.sh send`).
- **macOS**: `brew install skhd` (`f9 : /path/to/dictate-toggle.sh send`) or a Shortcuts.app
  Quick Action bound to a key. Both are user-space.

Always tell the user the script is a **toggle**: press to start, press again to stop and transcribe.

## Step 7 — verify (mandatory, this is how the install ends)

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.sh"
```

It renders a phrase through TTS, feeds the audio straight back into STT, and compares the transcript —
no microphone, no speakers, no display. On failure, read its message: it distinguishes "server not
reachable", "not a WAV", "no text", and "transcript does not match".

Then verify the two interactive halves with the user:
1. **speak-back** — end your next reply with a line starting with the marker and ask if they heard it.
2. **dictation** — ask them to press the hotkey, say a sentence, press again, and confirm the text
   arrived (pasted, or on the clipboard in tier 1).

Report at the end: language, both backends, paste tier, hotkey, selftest result. If anything is left
undone (e.g. the user declined the ydotool step), say exactly what and how to finish it later.
