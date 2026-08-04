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
for c in jq curl python3 pw-record arecord aplay paplay ffmpeg wl-copy xclip ydotool wtype xdotool notify-send gsettings sox rec afplay pbcopy osascript say brew skhd nvidia-smi; do \
  command -v "$c" >/dev/null 2>&1 && echo "have $c"; done; \
pgrep -qx TouchBarServer 2>/dev/null && echo "have touchbar"; \
python3 -c 'import sys; print("python", sys.version.split()[0])' 2>/dev/null; \
(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true); \
cat ~/.config/voice-loop/config.json 2>/dev/null || echo "no existing config"
```

From the output decide: OS branch (Linux / macOS), session type (Wayland / X11), desktop (GNOME /
KDE / other), whether a GPU exists, and what is already installed. Report the findings in two or three
lines — not a wall of text.

`have touchbar` means this Mac's function row is the virtual Touch Bar strip (`TouchBarServer` runs
on that hardware and nowhere else) — it decides the hotkey in Step 6. It is a positive signal only:
its absence on macOS means "probably not, ask" rather than "definitely not".

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

**Whenever you turn tier 2 or 3 on, say what auto-paste actually does**, in one sentence and without
drama: the text is pasted into whatever window is focused when they stop the recording, so switching
windows mid-sentence sends it there instead — useful (it works in every app) and occasionally
expensive (a private line into a public channel). Then mention `dictate.paste_target: "same-window"`
as available, and leave the choice with them; the default stays `"any"`. On a Wayland session, say
that the guard cannot work there rather than offering it.

Note for tier 2/3 alike: older `ydotool` only accepts **named key combos** and cannot type non-ASCII,
which is exactly why the scripts paste from the clipboard instead of typing the text.

## Step 6 — hotkey

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
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY command '<PLUGIN_ROOT>/scripts/dictate-toggle.sh send'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY binding 'F9'
```

Substitute the real absolute path — `${CLAUDE_PLUGIN_ROOT}` is not expanded by the desktop.

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
printf 'cmd - i : %s\n' '<PLUGIN_ROOT>/scripts/dictate-toggle.sh send' >> ~/.config/skhd/skhdrc && \
skhd --start-service
```

Substitute the real absolute path — `${CLAUDE_PLUGIN_ROOT}` is not expanded outside the session.
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
  (`bindsym F9 exec /path/to/dictate-toggle.sh send`).

Always tell the user the script is a **toggle**: press to start, press again to stop and transcribe.
Tapping is the gesture — *holding* the key does not queue up toggles, because a re-fire within
`dictate.debounce_ms` (750 ms) of the previous fire is ignored, and each ignored fire restarts that
window: a key held for ten seconds is still one toggle.

## Step 7 — the speak convention (the line that makes the model speak)

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

## Step 8 — verify (mandatory, this is how the install ends)

How the install ends depends on the backend shape — say which proof applies **before** running it:

- **HTTP endpoints configured (local server / lan / cloud):** the ending is the loopback selftest —

  ```sh
  bash "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.sh"
  ```

  It renders a phrase through TTS, feeds the audio straight back into STT, and compares the
  transcript — no microphone, no speakers, no display. On failure, read its message: it
  distinguishes "server not reachable", "not a WAV", "no text", and "transcript does not match".
- **Direct-command backends only (e.g. macOS `tts.command: "say …"`, no HTTP endpoint):** there is
  nothing for the loopback to loop through — `selftest.sh` will say so and exit without testing.
  The install ends with the **ear-check** below instead; do not promise a green selftest here.

Then verify the two interactive halves with the user (for a command-only setup this IS the proof):
1. **speak-back** — end your next reply with a line starting with the marker and ask if they heard it.
2. **dictation** — ask them to press the hotkey, say a sentence, press again, and confirm the text
   arrived (pasted, or on the clipboard in tier 1).

Report at the end: language, both backends, paste tier, hotkey, and the verification result
(loopback selftest or ear-check, whichever applied). If anything is left
undone (e.g. the user declined the ydotool step), say exactly what and how to finish it later.
