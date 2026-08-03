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
                │ 2. toggle again: stop, settle, read WAV       │ 3. retry only on a flush-race
                ▼                                               ▼    read (empty / same-as-last)
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
   `timeout: 90`) and passes JSON on stdin containing `transcript_path`. The registered entry point
   is `speak.sh` — a thin launcher whose only job is a stable registration surface; the logic is
   `scripts/speak.py` (stdlib-only Python).
2. `speak.py` reads the transcript **immediately**, takes the **last assistant message**, and keeps
   only the lines whose first non-space character is the marker (default `🔊`). Everything else
   stays text — the model decides what is worth hearing.
3. Two behaviours exist because of a real race, not out of caution:
   - **retry**: Stop can fire *before* the final assistant message is flushed to the transcript. A
     retry happens only on the two real race signatures — an **empty** extract, or one **identical
     to the last spoken line** — with adaptive backoff (0.15 → 1.0 s), so an already-flushed
     transcript costs zero sleep;
   - **queue**: that ladder is 2.65 s and a clip runs ten times longer. When it runs out with
     nothing new *and a previous line is still playing* (`playing.pid`, the same record the
     takeover reads), Stop keeps re-reading until that clip ends — bounded at 20 s — instead of
     giving up. The retry *condition* is unchanged, so a line that was already there is never
     delayed; only a line that would have been **dropped** is now merely late;
   - **dedup**: a read identical to the previous turn's *is* the stale previous turn, so it is
     dropped rather than spoken twice.

   Every one of those exits that abandons a line **logs why**. The paths that abandon nothing stay
   quiet on purpose: an eager firing with nothing new keeps its line for the next tool call, and a
   message with no marked line in it never had one to lose.
4. Playback **streams**. The marked text is split into sentence chunks (tiny sentences merged so a
   chunk is at least ~40 characters); the first chunk plays as soon as *it* is synthesized while the
   next one synthesizes. When the server's `/health` reports `"streaming": true`, the whole text
   goes to `POST /tts/stream` instead and the SSE chunks — each a complete standalone WAV — feed
   the same player queue as they arrive (the server does the sentence chunking); a stream that
   fails before its first chunk falls back once to the blob `/tts` path. A fresher line kills a
   still-playing older one — precisely, by the PIDs the speaking chain recorded.
5. Every failure path exits 0. A voice problem must never fail a turn.
6. **Eager mode** (opt-in, `speak.eager`) adds a second event to the same script: `hooks.json` also
   registers **PostToolUse**, and `speak.py` branches on `hook_event_name`. The PostToolUse path
   reads the marked lines of *all* assistant messages (not just the last) and does not retry the
   flush race — the next tool call is a free retry. What keeps the two paths from saying the same
   thing twice is the **spoken-ledger** (`~/.local/state/voice-loop/spoken.ledger`):
   `sha1(transcript_path + message index + line)` per spoken line, consulted and appended by both,
   plus one `seeded:<hash>` marker per transcript whose absence means "first run — write the history
   in it off silently". The message index is half of that identity because a transcript is
   append-only and repeats itself: without it, the second "Done." of a session would be mistaken for
   the first and never spoken.
   The ledger, the seeding and the lock are all **gated on `speak.eager`**. With it off there is one
   event path and therefore nothing to be idempotent against, so `Stop` runs the step-1..5 logic
   above unchanged — prev-utterance dedup, no shared state, no lock. Two paths are what create the
   problem, so the machinery arrives with the second path and not before.
7. **Concurrency in eager mode: one speaker, no queue.** The whole read-claim-speak sequence runs
   under an exclusive flock (`speaking.lock`) — claiming outside it would let two firings claim the
   same line — and the flock is taken **non-blocking**. A PostToolUse firing that loses simply
   exits: it claimed nothing, and the next tool call retries for free. That is deliberately not a
   queue. A waiting firing would be one blocked python process per tool call, and worse, it is
   invisible to everything that reads `playing.pid` — neither step 4's takeover nor `dictate.py`'s
   echo guard can stop a process that has not started speaking yet, so a queued firing wakes up and
   speaks into a microphone that is already recording. Only `Stop` waits, and only for `LOCK_GRACE`
   (a fraction of a second) before superseding the holder and trying once more; a `Stop` that still
   cannot get in leaves its lines unclaimed for the next turn's eager firing rather than claiming
   without the lock.

## The dictation path

1. A global hotkey (per desktop — `gsettings`, KDE shortcuts, sway/Hyprland config, `skhd`) runs
   `dictate-toggle.sh`. It is a **toggle**: first press records, second press stops.
   A **held** key is one toggle, not a stream of them: the OS turns it into an autorepeat, and a
   re-fire within `dictate.debounce_ms` of the previous **fire** is dropped before the direction is
   even chosen (otherwise every second repeat would stop a recording milliseconds old). The window
   is measured from the last fire, *admitted or dropped* — that is what makes it a debounce rather
   than a rate limiter: measured from the last admitted toggle, a hold would re-admit once per
   window for as long as it lasted. The stamp it compares against lives in `dictate-last-toggle`,
   is refreshed on every fire, and is read and written under a non-blocking `flock`, so two
   near-simultaneous fires cannot both read the old value. The default window (750 ms) is sized
   above the OS's key-repeat *delay*, not its interval: a shorter window would let the first repeat
   through. On macOS this is also
   why the wiring prefers a physical chord over the F-row: on a Touch Bar model `F9` needs `fn`
   held and may deliver no keycode at all.
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
| `local` | HTTP on `127.0.0.1`, or a direct command | [`server/`](../server/README.md) run on this machine; or `stt.command` / `tts.command` for engines that are not servers (`say`, whisper.cpp) |
| `lan` | HTTP to another host, or through an ssh tunnel | `ssh -N -L 8355:127.0.0.1:8355 user@host` keeps the endpoint `127.0.0.1` and the server unexposed |
| `cloud` | HTTPS to a provider | OpenAI-compatible or ElevenLabs; the key lives in a `key_file` or a named env var, never in the config |

The scripts speak two request shapes: the server's own (`POST /stt` multipart, `POST /tts` JSON) and
the provider's. Nothing else in the system changes when you switch backends.

## The server

`server/voice_server.py` (this plugin's own, one directory up) is a single FastAPI file: `POST /stt`
(faster-whisper), `POST /tts` (Silero
+ per-language accentuation by default, or XTTS-v2 voice cloning via `VOICE_LOOP_TTS_ENGINE=xtts`),
`POST /tts/stream` (the same synthesis as server-sent events, one WAV segment per sentence chunk),
`GET /health`. Models load lazily on first use, through seams that the unit tests replace with
fakes — which is why the whole file is testable without a model on disk.

Language is a **request field** (`?language=` / `{"language": ...}`), defaulting to the server's
`VOICE_LOOP_LANGUAGE`. Recognition is multilingual; synthesis is limited to the languages the
selected engine speaks, and an unsupported code returns `400` with the supported list rather than a
stack trace.

**Two fallbacks compose, and neither knows about the other.** The client falls back from
`/tts/stream` to the blob `/tts` when a stream dies before its first chunk (the speak path, step 4);
the server falls back from the primary engine to `VOICE_LOOP_TTS_FALLBACK_ENGINE` when synthesis
breaks ([Engine fallback](../server/README.md#engine-fallback)). Stacked, one piece of marked text
can therefore cost up to ~4 synthesis attempts before it is given up on: stream on the primary,
stream restarted on the fallback engine, then the blob request repeating the same pair. Each layer
is bounded on its own —
the server restarts a stream at most once and never after the first chunk, the client drops to the
blob path at most once — so the composition terminates instead of looping; what it costs on a
broken engine is latency, and the `tts_fallbacks` counter says how often it is being paid.

## Where state lives

| path | what |
|---|---|
| `~/.config/voice-loop/config.json` | the only configuration; written by `/voice-setup` |
| `~/.config/voice-loop/stress.json` | optional stress overrides for the synthesizer |
| `stt_hallucinations.txt` (next to `voice_server.py`) | known Whisper hallucinations `/stt` drops whole, or strips off the tail of real speech (user-extendable) |
| `~/.config/voice-loop/*.key` | optional cloud key files (mode 600) |
| `~/.local/state/voice-loop/` | logs, the last spoken line, the recorder PID, the last WAV |
| `~/.local/share/voice-loop/` | optional: the venv, models, voice previews |

Nothing is written into the repo, and nothing outside these paths is touched. `/voice-remove` walks
this same table in reverse — plus the model caches under `~/.cache/` that the server's own loaders
(torch.hub, HuggingFace, coqui) fill, which are shared with other tools and so are removed entry by
entry rather than wholesale.
