# Architecture

Two independent paths that share one config file. Neither knows about the other; either can be used
alone.

```
                     ┌──────────────────────────────────────────────┐
                     │        ~/.config/voice-loop/config.json       │
                     │  language · stt{} · tts{} · speak{} · dictate{}│
                     └───────────────┬──────────────┬───────────────┘
                                     │ read         │ read
   DICTATION (in)                    │              │                 SPEAK-BACK (out)
   ─────────────────                 │              │                 ────────────────
   hotkey ─▶ dictate-toggle.sh ──────┘              └────── speak.sh ◀── Stop hook
                │                                              │         (hooks.json)
                │ 1. toggle: start recorder                     │ 1. read transcript_path
                │    (pw-record/arecord | sox/ffmpeg)           │ 2. take lines starting with 🔊
                │ 2. toggle again: stop, settle, read WAV       │ 3. retry until the line differs
                ▼                                               ▼    from the last spoken one
          ┌───────────┐                                   ┌───────────┐
          │  STT      │                                   │   TTS     │
          │ local/lan │                                   │ local/lan │
          │ or cloud  │                                   │ or cloud  │
          └─────┬─────┘                                   └─────┬─────┘
                │ text                                          │ audio
                ▼                                               ▼
        clipboard (always)                                 player
        + optional paste keystroke                         (aplay/afplay/…)
                │
                ▼
         focused window ──▶ Claude Code prompt
```

## The speak path

1. Claude Code fires the **Stop** hook (registered by `hooks/hooks.json`, `async: true`,
   `timeout: 90`) and passes JSON on stdin containing `transcript_path`.
2. `speak.sh` reads the transcript, takes the **last assistant message**, and keeps only the lines
   whose first non-space character is the marker (default `🔊`). Everything else stays text — the
   model decides what is worth hearing.
3. Two behaviours exist because of a real race, not out of caution:
   - **retry**: Stop can fire *before* the final assistant message is flushed to the transcript, so
     the script retries (6 × 0.7 s) until it sees a line **different** from the last one spoken;
   - **dedup**: a read identical to the previous turn's *is* the stale previous turn, so it is
     dropped rather than spoken twice.
4. The text goes to the configured TTS backend; the audio goes to the configured player. A fresher
   line kills a still-playing older one.
5. Every failure path exits 0. A voice problem must never fail a turn.

## The dictation path

1. A global hotkey (per desktop — `gsettings`, KDE shortcuts, sway/Hyprland config, `skhd`) runs
   `dictate-toggle.sh`. It is a **toggle**: first press records, second press stops.
2. Stopping sends `SIGINT` first (so `sox`/`ffmpeg` finalize the WAV header), waits for the process
   to actually exit, then settles briefly — the recorder flushes its tail *after* it stops taking
   samples, and skipping that wait truncates the last word.
3. The WAV goes to the configured STT backend; the transcript comes back as text.
4. The text is **always** put on the clipboard. Auto-paste is an opt-in extra, and when it is not
   available the script says "copied — press `<paste_key>`" instead of failing.

## The three backends

Configured **per direction** — `stt` and `tts` are independent, so local recognition with cloud
synthesis is a normal setup.

| | how it is reached | notes |
|---|---|---|
| `local` | HTTP on `127.0.0.1`, or a direct command | `server/` run on this machine; or `stt.command` / `tts.command` for engines that are not servers (`say`, whisper.cpp) |
| `lan` | HTTP to another host, or through an ssh tunnel | `ssh -N -L 8355:127.0.0.1:8355 user@host` keeps the endpoint `127.0.0.1` and the server unexposed |
| `cloud` | HTTPS to a provider | OpenAI-compatible or ElevenLabs; the key lives in a `key_file` or a named env var, never in the config |

The scripts speak two request shapes: the server's own (`POST /stt` multipart, `POST /tts` JSON) and
the provider's. Nothing else in the system changes when you switch backends.

## The server

`server/voice_server.py` is a single FastAPI file: `POST /stt` (faster-whisper), `POST /tts` (Silero
+ per-language accentuation), `GET /health`. Models load lazily on first use, through seams that the
unit tests replace with fakes — which is why the whole file is testable without a model on disk.

Language is a **request field** (`?language=` / `{"language": ...}`), defaulting to the server's
`VOICE_LOOP_LANGUAGE`. Recognition is multilingual; synthesis is limited to the languages Silero
ships a voice for, and an unsupported code returns `400` with the supported list rather than a
stack trace.

## Where state lives

| path | what |
|---|---|
| `~/.config/voice-loop/config.json` | the only configuration; written by `/voice-setup` |
| `~/.config/voice-loop/stress.json` | optional stress overrides for the synthesizer |
| `~/.config/voice-loop/*.key` | optional cloud key files (mode 600) |
| `~/.local/state/voice-loop/` | logs, the last spoken line, the recorder PID, the last WAV |
| `~/.local/share/voice-loop/` | optional: the venv, models, voice previews |

Nothing is written into the repo, and nothing outside these paths is touched.
