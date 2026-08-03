# Troubleshooting

Seeded from the failures that actually happened while building this, grouped by class. Logs first:

```sh
tail -n 40 ~/.local/state/voice-loop/speak.log      # speak-back
tail -n 40 ~/.local/state/voice-loop/dictate.log    # dictation
# is the speech path alive at all? in a Claude Code session (marketplace install):
bash "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.sh"
# from a manual checkout of the repo it is the repo-relative path instead:
#   bash plugins/voice-loop/scripts/selftest.sh
```

## Nothing is pasted, only copied

**Cause:** your compositor exposes no virtual-keyboard protocol, so no userland tool can synthesize
the keystroke. GNOME's Mutter on Wayland is the common case — `wtype` simply cannot work there.

**What to do**, in order of how much privilege it costs:

1. Nothing. This is the default tier and it works everywhere: the text is on your clipboard, press
   your own paste key. The notification tells you which key is configured.
2. KDE / wlroots (sway, Hyprland): install `wtype` — pure userland, no daemon, no root. Set
   `dictate.auto_paste: true`.
3. X11: install `xdotool`. Same, pure userland.
4. GNOME/Wayland only: `ydotool` + its daemon on `/dev/uinput` — **one root step**, and it grants a
   background daemon the ability to synthesize input. `/voice-setup` prints the command instead of
   running it. Declining costs you nothing but the convenience.

**Pasted into the wrong place / nothing appears:** `dictate.paste_key` must match the focused app.
Terminals usually want `ctrl+shift+v` or `shift+insert`; GUI apps and macOS want `cmd+v`.

**Older `ydotool` types garbage for non-Latin text:** it only accepts *named key combos* and cannot
type non-ASCII. That is precisely why the scripts paste from the clipboard instead of typing.

## The last word of my sentence is missing

**Cause:** the recorder flushes its tail *after* it stops accepting samples. Killing it and reading
the file immediately truncates the recording.

The script already sends `SIGINT` first (so `sox`/`ffmpeg` can finalize the WAV header), waits for
the process to exit, then settles ~0.2 s. If you still lose the tail on slow hardware, the file to
look at is `~/.local/state/voice-loop/dictate-last.wav` — play it: if the audio itself is short, it
is a recorder-flush problem; if the audio is complete but the transcript is short, it is recognition
(try a larger `VOICE_LOOP_STT_MODEL`).

**A recording seems stuck:** the PID file is stale. Press the hotkey once more — the script clears a
dead PID and starts fresh.

## The log is full of "clip too short", nothing is ever transcribed

Look at the timestamps in `~/.local/state/voice-loop/dictate.log`: several fires inside the same
second is not you pressing quickly, it is **key repeat**. Holding the hotkey instead of tapping it
makes the OS deliver it 4+ times a second, and every second fire stops a recording milliseconds old.

**Handled by design:** a re-fire within `dictate.debounce_ms` (500 ms by default) of the previous
toggle is dropped before either branch is chosen, and logged as `toggle ignored — key repeat`. So
the fix is nothing — tap or hold, one press is one toggle. Set `debounce_ms` to `0` only if you
genuinely want the raw behaviour back.

## The hotkey does nothing at all on my Mac

**Cause:** on a **Touch Bar** MacBook the function row is a virtual strip, not keys. Unless *Keyboard
→ Function Keys → "F1, F2, etc. keys always shown in Touch Bar"* is enabled, `F9` exists only while
`fn` is held — so a binding on plain `f9` may never receive a keycode.

Bind a **physical chord** instead. With `skhd` (`brew install skhd`), in `~/.config/skhd/skhdrc`:

```
cmd - i : /path/to/plugins/voice-loop/scripts/dictate-toggle.sh send
```

then `skhd --restart-service`. Any chord works — `cmd + shift - d`, `ctrl + alt - d`; ⌘I collides
with *Get Info* in Finder and *italic* in editors, and skhd takes it globally, so pick another if you
use those. To keep the F-row anyway, the binding must be `fn - f9`.

Two checks before blaming the key: skhd needs one **Accessibility** consent (System Settings →
Privacy & Security → Accessibility), and `~/.local/state/voice-loop/dictate.log` staying empty on a
press means the script was never run at all — that is the binding, not the script.

## It speaks the previous answer, or speaks twice

**Cause:** the Stop hook can fire *before* the final assistant message reaches the transcript file.
Reading it naively gets the previous turn.

Handled by design: the hook (`speak.py`) retries only on a flush-race read — an empty extract, or
one identical to the last spoken line — and drops a read identical to the previous turn. If you see
repeats anyway:

- check `~/.local/state/voice-loop/last-spoken` — it is the dedup memory; delete it to reset;
- if you genuinely want the same sentence spoken twice in a row, vary it by a word;
- confirm only one Stop hook is registered (a manual copy in `settings.json` *and* the plugin's will
  both fire).

**Nothing is spoken at all:** does the reply contain a line that *starts* with the marker? A marker
in the middle of a line is not spoken. Ask for it in your `CLAUDE.md` if you want it consistently.

## The voice sounds robotic / mangles names

Two different causes, and the common one is not synthesis quality.

**Russian and Ukrainian: it is usually stress.** Check `GET /health` → `accentuated_languages`. If
your language is not there, the accentuation package is not installed (`ruaccent`,
`ukrainian-word-stress`). Then add the words it still gets wrong to
`~/.config/voice-loop/stress.json`, or type a combining acute in the text (`Ка́тя`) and the server
converts it. Your rules run *before* the automatic accentuator and are never overridden by it.

**Cloud voices: it is usually settings.** Counter-intuitively, breathy voices go metallic at *low*
stability. Raise `stability` to 0.6–0.75, keep `style` ≤ 0.15, `similarity_boost` 0.75–0.85,
`use_speaker_boost: true`. Keep each request short — long blocks drift in tone toward the end. If
artifacts survive the settings, regenerate a new preview rather than tuning further.

## The server is on another machine and unreachable

The server binds `127.0.0.1` by default and has **no authentication** — that is deliberate. Reach it
over ssh instead of opening a port:

```sh
ssh -N -L 8355:127.0.0.1:8355 user@gpu-host
# the client's endpoint stays http://127.0.0.1:8355
```

If you really must expose it, `VOICE_LOOP_HOST=0.0.0.0` behind a firewall you control — anyone who
can reach the port can transcribe and synthesize on your hardware.

**Corporate proxy swallowing the request:** the Python scripts already bypass proxies (urllib with
an empty `ProxyHandler`), and `selftest.sh` passes `curl --noproxy '*'`; if you call the API by hand
with curl, do the same.

## selftest fails

The message distinguishes the cases on purpose:

| message | meaning |
|---|---|
| TTS request returned http=000 / 0 bytes | the server is not running or not reachable — `curl $ENDPOINT/health` |
| returned N bytes that are not a WAV | the endpoint answered with an error document; the first 200 bytes are printed |
| STT returned no text | audio was produced but recognized as silence — usually a wrong `language`, or a voice that does not speak it |
| transcript does not match | recognition works but is inaccurate: model too small, or language mismatch between `stt` and `tts` |

`--keep` leaves the generated WAV on disk so you can listen to what the recognizer was given.

## Everything works, but only when I run it by hand

The desktop does not expand `${CLAUDE_PLUGIN_ROOT}` — a hotkey needs the **absolute path** to
`dictate-toggle.sh`. And a hotkey launched by the desktop gets a minimal environment: if your config
references tools installed in a shell-only `PATH` (Homebrew, `~/.local/bin`), use absolute paths in
`config.json`.

### Dictation echoes the assistant back

You pressed the hotkey while a spoken line was still playing, and the microphone transcribed the speakers. The dictation script now stops in-flight speech playback when a recording starts (the echo guard). If you still catch echo (external speakers, high volume), start recording after the line finishes, or use headphones.
