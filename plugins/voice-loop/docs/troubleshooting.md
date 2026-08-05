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

## The transcript went into the wrong app entirely

Not a bug — **paste-at-focus**. Auto-paste presses your paste key into whatever is focused when you
*stop* the recording, so switching windows mid-sentence sends the text to the new window. Dictate
into Claude Code, alt-tab to a chat while still speaking, stop: the transcript is pasted into the
chat, and in `send` mode it is sent. This is the same property that makes the hotkey useful anywhere,
so it is not going away by default.

**The seatbelt**, if you want it, is `dictate.paste_target: "same-window"`: the focused window is
remembered when the recording starts, and if focus moved by stop-time nothing is pasted — the text
stays on the clipboard and the notification says *"focus moved — text is in the clipboard"*.

**It is not protecting me:** check `~/.local/state/voice-loop/dictate.log` for the `focus at start:`
line each guarded recording writes.

- `focus at start: unknown …` — this desktop cannot be asked what is focused, so the guard degrades
  to `"any"` and pastes. **Wayland is the whole class**: no portable query exists, `xdotool` under
  XWayland only sees X clients and goes stale the moment you switch to a native Wayland window, and a
  wrong identity would suppress the paste exactly when focus did *not* move. X11 needs `xdotool`
  installed and a `$DISPLAY`; macOS needs `osascript`.
- no `focus at start:` line at all — the guard was off for that recording: it applies only to
  `auto_paste: true` (on the clipboard tier you choose the window yourself), and it reads the config
  as it was when the recording **started**.
- on macOS the identity is the frontmost **application**, not the window — moving between two windows
  of the same app is not a move the guard can see.

**It suppresses a paste I wanted:** you switched windows (or, on X11, the window id changed under
you). Press your paste key — the text is on the clipboard — or set `paste_target` back to `"any"`.
Note that a typo in the value resolves to `"same-window"`, not to the default: only somebody who
wanted the guard writes that key at all. `dictate.log` says so if it happened.

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

**Handled by design:** a re-fire within `dictate.debounce_ms` (750 ms by default) of the previous
fire is dropped before either branch is chosen, and logged as `toggle ignored — key repeat`. Each
dropped fire restarts the window, so a key held for ten seconds is still one toggle and the guard
clears one window after you let go. So the fix is nothing — tap or hold, one press is one toggle.
Set `debounce_ms` to `0` only if you genuinely want the raw behaviour back.

The suppression covers *both* directions, which is worth knowing for the one asymmetric case: a
press meant as **stop** that lands inside the window is dropped too, so the microphone is still
recording. Press once more, past the window, and it stops normally — nothing is sent anywhere in
between, but the recording is genuinely still running until you do.

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

## The voice lags a couple of replies behind, or skips lines entirely

**Cause:** the hook arrived while the *previous* line was still playing and found nothing new to
say yet. It waits out the clip in front (up to 20 s) and speaks the line late rather than dropping
it — but a line written after that ceiling, or while nothing is playing at all, is genuinely gone.

`speak.log` now says which happened, and the wording is the diagnosis:

- `queued, not dropped` — the line waited behind a playing clip and was then spoken. Working as
  intended; if the wait itself is the complaint, shorten the spoken lines so each clip is shorter;
- `gave up with nothing new in the transcript` — the ladder ran out with nothing playing behind it.
  The line never reached the transcript in time and was **lost**;
- `still playing … waiting no longer` — a clip outlasted the 20 s ceiling. Usually a wedged player:
  check `speak.player` actually exits when the file ends (`aplay -q`, `afplay`, `mpg123 -q`);
- `dropped a read identical to the last spoken line (dedup)` — the reply repeated the previous
  turn's line verbatim; see the section above.

If the log has **no entry at all** for a turn, the hook never ran: check that it is registered, and
that `speak.enabled` is not false.

## The voice stops entirely mid-session, but everything works by hand

**Signature:** an hour or so into one long Claude Code session, nothing is spoken any more, and
`speak.log` simply stops growing while the conversation continues — no error, no `dropped`, no
`gave up`, just nothing after a certain line. And yet the whole plugin chain is healthy: firing the
hook by hand synthesizes and plays fine:

```sh
printf '%s' '{"transcript_path": "/path/to/your/transcript.jsonl"}' \
  | bash "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh"
```

**Cause:** the harness itself has stopped invoking the Stop hook. That is above the plugin's pay
grade to fix — the hook cannot fire itself — and it is exactly the case the sections above cannot
tell apart from ordinary silence, because their evidence all lives in a log the dead hook no longer
writes.

**The heartbeat** is what tells the two apart. Every hook invocation — even one that speaks
nothing — rewrites `~/.local/state/voice-loop/hook-last-fired`, and the server reports its age:

```sh
curl -s http://127.0.0.1:8355/health | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["hook_last_fired"], h["hook_last_fired_age_s"])'
```

If `hook_last_fired_age_s` keeps **growing while you chat** — minutes old, then tens of minutes —
the harness is no longer calling the hook, and no plugin-side setting will bring the voice back.
Both fields are `null` when the server cannot see the stamp: the hook never fired on this machine,
or the server runs on another one (the ssh-tunnel setup) and the client's state dir is not there
to read.

**Remedy:** restart the Claude Code session — hooks re-initialize on startup, and the voice comes
back. Nothing else needs reinstalling or reconfiguring; the stamp and the logs survive the restart
and pick up where they left off.

If you hit this and can reproduce it, it is a harness bug worth reporting upstream to Claude Code:
the evidence to attach is the speak.log tail (the line the firings stop after), the growing
`hook_last_fired_age_s`, and the fact that a manual invocation of the same script still plays.

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

## macOS: `CERTIFICATE_VERIFY_FAILED` on every https call

**Cause:** you are on python.org-installer Python, whose bundled OpenSSL ships with an **empty
certificate store**. The installer drops a script to populate it and nothing runs that script for
you, so until you do, *every* https call from that interpreter fails the same way — the cloud TTS
request, `torch.hub`'s model download, and `pip install` itself:

```
SSL: CERTIFICATE_VERIFY_FAILED — unable to get local issuer certificate
```

It is not your key, not your network and not the plugin. Ask the probe:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/tls-probe.sh"   # 0 verified · 1 certificate · 2 unreachable · 64 called wrong
```

It names the exact fix for *this* machine, and with `--fix` it runs the runnable one and probes
again (so a green is a re-verified green, not a hope). The fix for the python.org case is the
installer's own command — no root, and it touches nothing but that Python:

```sh
"/Applications/Python 3.12/Install Certificates.command"     # substitute your X.Y
```

`/voice-setup` now runs this probe on macOS **before** it installs anything, which is the point:
the failure used to surface hours later as a silent hook.

A few neighbours that look identical and are not:

| the probe says | it means |
|---|---|
| `env-override` — `SSL_CERT_FILE` / `SSL_CERT_DIR` is set | an override beats every store below it; an empty or stale one fails exactly like the trap. Unset it and probe again |
| `homebrew-certifi` | not python.org Python, so the empty-store trap does not apply — an intercepting proxy's own CA is the usual cause here |
| `system-trust-store` (Linux) | missing/stale `ca-certificates`, or a proxy MITM |
| `UNKNOWN … could not be reached` | the network, not TLS. Nothing to fix here |
| exit **64** with a usage message | the probe was called wrong (a typo'd flag, a missing value, an `http://` URL). Not a diagnosis of anything — fix the invocation |

The probe deliberately **bypasses proxies**, like the rest of the scripts — it is asking about this
interpreter's own store. A corporate proxy that intercepts TLS is the case above it, and its CA
belongs in the trust store rather than in a probe flag. That is also why a green under a configured
`HTTPS_PROXY` prints a note saying so: `pip` and the model download *do* go through the proxy, and
its CA has to be trusted separately — the probe having stepped around it proves nothing about them.

Note this is per-interpreter, not per-machine: a **venv inherits the store of the python it was
built from**. If the server's venv has a different base than the `python3` on your `PATH`, probe
that one too — `~/.local/share/voice-loop/venv/bin/python .../scripts/tls-probe.py`.

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

## The voice said "Voice contour: …"

That is the poller's page, working as intended — read `~/.local/state/voice-loop/contour.json` for
which alert fired. The four shapes: a service not answering (check the process, then the URL in
`contour.services`), a service **serving on a device other than its `expect_device`** (it demoted
itself — usually VRAM pressure from a neighbour; `nvidia-smi` says who holds the card), free VRAM
under the floor, and `oom_overflows` rising (the card is oversubscribed). The page repeats only
when the condition clears and returns; `contour.alerts: false` silences the voice while the poller
keeps writing the file. If the alert text is wrong about the device a client needs, the
`expect_device` key is the thing to fix — or remove, if nothing depends on the fast path.

## None of the above — report it

`/report-bug` collects the evidence for you: versions, config with the secrets stripped, both log
tails, `/health`, and the last job states, in one bundle it shows you in full before asking whether
to send it and where. Nothing about your machine — and nothing you said or heard — leaves without
that yes. See [the README section](../README.md#when-it-misbehaves--report-bug) for exactly what is
stripped, and run the collector by hand if you would rather read the bundle first:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh" collect --summary "what went wrong"
```
