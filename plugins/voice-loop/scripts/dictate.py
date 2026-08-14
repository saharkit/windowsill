#!/usr/bin/env python3
"""voice-loop — push-to-talk dictation toggle: record -> STT -> clipboard/paste-into-prompt.

This is the toggle's whole logic; ``scripts/dictate-toggle.sh`` is only a thin launcher (hotkey
bindings keep invoking the .sh — gsettings/sway/skhd register that path — so the registration
surface never changes). Stdlib only, Python 3.10+.

Usage: dictate.py [send|paste]
  send  (default) paste the text AND press Enter — hands-free prompting
  paste           paste only, you press Enter yourself

Config: ~/.config/voice-loop/config.json (see README). Zero-root by default: the text is put on
the clipboard and you press your own paste key. Auto-paste is the opt-in tier.

Deliberate behaviours, found by live debugging — do not "simplify" them away:

* toggle — one hotkey, two meanings: a pidfile with a LIVE pid means "recording, stop it";
  anything else (absent, stale pid) means "start". A stale pidfile is removed, never obeyed.
* key-repeat guard — a toggle is a human TAP. A HELD hotkey is turned into an autorepeat stream by
  the OS (4+ fires per second), and every second fire stops a recording milliseconds old, so the
  log fills with "clip too short" and nothing is ever transcribed (windowsill#49). A re-fire within
  dictate.debounce_ms of the previous FIRE — admitted or dropped, the stamp moves either way — is
  dropped before either branch is chosen. Measuring from the last fire is what makes it a DEBOUNCE
  rather than a rate limiter: a key held for ten seconds is ONE toggle, not one every window.
* start is an ATOMIC claim — the pidfile is created with O_CREAT|O_EXCL before any recorder is
  spawned, so of two near-simultaneous hotkey invocations exactly one starts a recorder; the
  loser exits with a note. Stop removes the pidfile BEFORE signalling the recorder, so a racing
  start claims a fresh slot instead of adopting a dying recorder.
* echo guard — starting a recording first stops any in-flight speak playback, so the microphone
  never records our own speakers (windowsill#3). Primary path: read speak.py's playing.pid and
  SIGTERM exactly the recorded chain (identity-checked against /proc); the historical
  pkill -f "voice-loop-speak" pattern-kill is only the fallback when no pidfile exists.
  The belt-and-suspenders half runs AFTER transcription: a transcript that matches what the
  assistant just spoke (read from the state store) is dropped before it reaches the prompt
  (windowsill#101). AEC alone can degrade — this is load-bearing, not optional.
* echo cancel source — when ``dictate.source`` names a PipeWire source (the Echo-Cancel Source
  created by the AEC module), ``pw-record --target`` captures from it instead of the default mic.
  If the module is absent pw-record falls back to the default source — degrade, never block.
* recorder table — auto picks pw-record -> arecord -> ffmpeg on Linux, sox(rec) -> ffmpeg on
  macOS; every recorder is pinned to 16 kHz mono S16 (what STT wants, and what the clip-length
  guard's byte math assumes).
* stop is INT-then-TERM — SIGINT lets sox/ffmpeg finalize the WAV header; TERM is the fallback
  for pw-record/arecord. After the process exits, a 0.2 s settle: the recorder flushes the tail
  of the file AFTER it stops accepting samples — this pause is the difference between a complete
  phrase and a truncated one.
* min-clip guard — a clip shorter than MIN_CLIP_SECONDS (an accidental double-tap, a bounced
  hotkey) is dropped BEFORE the STT call: below that length there is no speech to find, and the
  server would only hallucinate on the silence (#19).
* clipboard tiers — pbcopy (macOS), wl-copy (Wayland, + --primary), xclip (X11, clipboard AND
  primary selections). The clipboard is the DEFAULT tier, not a failure mode.
* paste table — osascript (macOS; no Insert key on mac keyboards, so shift+insert maps to the
  native cmd+v; key code 36 is Return), ydotool (Wayland with a running ydotoold: NAMED combos
  only — older ydotool cannot type non-ASCII, which is exactly why we paste a clipboard instead
  of typing the text), wtype (wlroots/KDE: pure userland, no daemon, no root; GNOME/Mutter
  exposes no virtual-keyboard protocol, so wtype is not available there — the clipboard path
  covers it), xdotool (X11). Failing to paste falls back to "it is in your clipboard, press
  <paste_key>" — that is the default tier, not an error.
* paste-at-focus, and the guard for it — auto-paste presses the paste key into whatever is focused
  when the recording STOPS, which is the feature (dictate into any app, not just Claude Code) and
  the footgun in one: switch windows mid-sentence and the words meant for the agent land in a chat.
  dictate.paste_target: "any" (default, the power behaviour) or "same-window", which records the
  focused window at START and degrades to clipboard-only if focus moved by stop-time. The identity
  is what the platform can name — macOS: the frontmost application (so window-to-window inside ONE
  app is not a move); X11: xdotool's active window id; Wayland: nothing portable exists, so the
  guard degrades to "any" rather than pretending. Every unknowable half degrades the same way: a
  guard that cannot see focus must not be a dictation that never pastes.
* keys — the cloud API key comes from ``key_file`` (wins) or the named env var, is used only as
  an in-process HTTP header, and NEVER appears in argv, in the config, or in the log.
* streaming dictation (opt-in, ``stt.cloud.streaming``, windowsill#99) — the batch flow makes a long
  dictation pay twice: you speak for a minute, then wait at the end while the whole clip uploads and
  transcribes. Where the configured provider's registry entry carries a STREAMING variant and the
  config asks for it, the START toggle spawns one child — the stream worker — which opens the
  provider's websocket and forwards the recording's PCM to it WHILE the microphone is open,
  collecting the finals as they come back. The stop toggle then stops the recorder, asks the worker
  for its last word, and pastes what is already assembled instead of POSTing the clip.
  Four properties hold it together, and none of them is optional:
  - **the WAV is written exactly as before.** The worker TAILS the recording file; it does not
    stand between the recorder and the disk. Salvage discipline is unchanged — the clip lands in
    ``dictate-last.wav`` whatever the socket did — and the recorder table, the min-clip guard and
    the debounce all work on the same bytes they always did.
  - **degrade to batch at ANY point.** No key, a socket that will not open, an auth refusal, a
    server that hangs up mid-recording, a worker that does not finish inside its bound, a stream
    that produced nothing at all — every one of them logs its reason and falls back to the existing
    record→POST path. A recording is never lost to the new path.
  - **it does not touch the toggle's mutex (#50).** The worker is spawned only after the O_CREAT|
    O_EXCL pidfile claim is won and the recorder is live; it records its own pid in its own file;
    the stop path stops it, reads its result and CLEARS both files unconditionally, so no later
    recording can adopt an older worker's transcript. The stop path's wait for it is bounded
    (STREAM_FINISH_TIMEOUT), which is what keeps "re-toggle during the tail" from wedging.
  - **the worker outlives nothing.** It stops on the stop toggle's SIGTERM, and failing that on its
    own evidence: the recorder pid is gone and the file has stopped growing (STREAM_TAIL_SECONDS),
    or the session hit STREAM_MAX_SECONDS. A hotkey that is never pressed again cannot leave a
    metered socket open forever.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# The two modules in scripts/ this file imports: the provider registry (see providers.py) and the
# stdlib-only websocket client the streaming variant needs (see wsclient.py). The launcher checks
# for both beside dictate.py the same way it checks for dictate.py itself, and they resolve because
# sys.path[0] is this file's directory — the tests, which load this file by spec_from_file_location,
# put scripts/ on sys.path themselves.
import providers
import wsclient

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only; Linux and macOS are the supported platforms
    fcntl = None  # type: ignore[assignment]

# Every recorder in the table records 16 kHz mono S16 — 32000 bytes of PCM per second. The clip
# guard converts the WAV's size to seconds with this constant, so the two must move together.
RECORD_RATE = 16000
BYTES_PER_SECOND = RECORD_RATE * 2
WAV_HEADER_BYTES = 44
# Below this length there is no phrase, only a bounced hotkey; STT is skipped entirely.
MIN_CLIP_SECONDS = 0.3
# Key-repeat guard: how long after a FIRE another one is treated as autorepeat rather than as a
# second tap. The window is sized against the OS's repeat DELAY, not its repeat interval — the
# interval is the easy part (~4 Hz, and every one of those fires restarts the window anyway), while
# the delay is what the FIRST repeat waits out, and it is 375 ms (macOS default), 500 ms (GNOME) or
# 660 ms (X11 default). A window at or under that delay lets exactly one repeat through, which is
# one bogus stop→STT→paste per hold; 750 ms clears all three with margin. The upper bound is the
# deliberate quick tap, and it is far away: three quarters of a second of speech is not a phrase
# anyone meant to dictate, so nothing a human intends is lost here. A repeat delay configured longer
# than the window (macOS allows 2 s) admits one repeat again — raise debounce_ms past it.
# Tunable as dictate.debounce_ms; 0 turns the guard off.
DEBOUNCE_SECONDS = 0.75
# Wall clock for one focus query (dictate.paste_target: "same-window"). It runs on the recording's
# critical path twice, so it is short: an osascript that has not answered in two seconds is a
# wedged System Events, and the guard's answer to "I cannot see focus" is already "paste anyway".
FOCUS_PROBE_TIMEOUT = 2.0

# --- streaming dictation (windowsill#99) ---------------------------------------------------------
#
# Every number here is a BOUND on something that would otherwise be unbounded, and each one is sized
# against what it is protecting: the toggle's responsiveness, a metered socket, or the user's tail.

# The argv the worker child is spawned with — this script calling itself, so there is one file to
# copy and one launcher to register, exactly as before.
STREAM_WORKER_ARG = "stream-worker"
# 250 ms of 16 kHz mono PCM per frame. Small enough that the server is transcribing while the
# sentence is still being spoken, large enough that a minute of speech is 240 frames, not 6000.
STREAM_CHUNK_BYTES = BYTES_PER_SECOND // 4
# How long a poll waits when the recorder has produced nothing new — the loop's idle cost.
STREAM_IDLE_POLL = 0.05
# The socket must open fast or not at all: this whole feature exists to REMOVE a wait.
STREAM_CONNECT_TIMEOUT = 5.0
# A vendor closes an idle live socket after ~10 s of silence; a keepalive every few seconds costs
# one tiny text frame and keeps a thinking pause from ending the dictation.
STREAM_KEEPALIVE_SECONDS = 5.0
# After CloseStream the server still owes us the finals for the audio it has not answered yet.
# THIS is the end-of-speech latency the feature is measured on, so it is bounded tightly.
STREAM_DRAIN_SECONDS = 3.0
# What the STOP toggle will wait for the worker's own last word before giving up on it and using
# the recorded clip instead. Bounded because #50 is exactly the shape of an unbounded tail.
STREAM_FINISH_TIMEOUT = 5.0
# The worker's own evidence that the recording is over even though no stop toggle ever arrived:
# the recorder process is gone AND the file has not grown for this long.
STREAM_TAIL_SECONDS = 2.0
# The last resort against a metered socket held open by a hotkey nobody pressed again.
STREAM_MAX_SECONDS = 3600.0
# A WAV header is 44 bytes canonically, but ffmpeg writes extra chunks; this is how far into the
# file the `data` chunk is looked for before the header is called unreadable.
STREAM_HEADER_PROBE_BYTES = 4096
# How long the worker waits for the recorder to produce a readable header at all.
STREAM_HEADER_TIMEOUT = 3.0

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
_LOG_PATH = os.path.join(_STATE_DIR, "dictate.log")
_PID_PATH = os.path.join(_STATE_DIR, "dictate.pid")
_WAV_PATH = os.path.join(_STATE_DIR, "dictate.wav")
_LAST_WAV_PATH = os.path.join(_STATE_DIR, "dictate-last.wav")
# CROSS-SCRIPT CONTRACT (keep in sync with speak.py's _LAST_PATH): the last text the assistant spoke
# out loud — the belt-and-suspenders echo guard reads this after transcription and drops a transcript
# that matches it (AEC alone can degrade; the state-store comparison is load-bearing).
_LAST_SPOKEN_PATH = os.path.join(_STATE_DIR, "last-spoken")
# When the previous toggle happened — the whole state the key-repeat guard keeps. Separate from the
# pidfile on purpose: the pidfile says whether a recorder is running, this says how long ago the
# hotkey was last acted on, and the guard has to answer before either branch is chosen.
_TOGGLE_PATH = os.path.join(_STATE_DIR, "dictate-last-toggle")
# Which window was focused when THIS recording started — the whole state the same-window guard
# keeps, written at start and consumed at stop. Absent (guard off at start, an unwritable state
# dir, a platform that cannot name its focus) reads as "unknown", which pastes.
_FOCUS_PATH = os.path.join(_STATE_DIR, "dictate-focus")

# The streaming worker's two files (windowsill#99), both owned by one recording and cleared by the
# stop that ends it. The pidfile is how the stop toggle finds the child it must stop; the result
# file is the child's ONE write — a whole JSON document, written temp-then-replace, so the reader
# either sees a complete answer or no answer at all. Its `text` field is the dictated words
# themselves, which is why /report-bug lists it for its SIZE and never opens it.
_STREAM_PID_PATH = os.path.join(_STATE_DIR, "dictate-stream.pid")
# The result filename carries the worker PID. A predecessor that outlives its stop can therefore
# finish writing its own document without replacing the current worker's answer. The name-is-the-pid
# trade is that a document nobody holds a pid for any more cannot be found by name at all, so
# FORGETTING a stream means sweeping every one of these (see _stream_result_paths): each one holds
# the dictated words, and an unswept one is speech left on disk for good.
# This path itself is only the naming anchor — the directory and the stem both come from it — and is
# never the file written or read.
_STREAM_RESULT_PATH = os.path.join(_STATE_DIR, "dictate-stream.json")
# Anchored on the digits: only _write_stream_result produces this spelling, so a sweep over it can
# never reach another file in the state dir (dictate-stream.pid and dictate.log do not match).
_STREAM_RESULT_RE = re.compile(r"dictate-stream\.\d+\.json")

# The live preview surface (windowsill#115): the stream worker writes the current interim and the
# assembled finals here, atomically (temp-then-replace — see _write_preview), and a separate
# preview.py process reads and renders it. Absent file → nothing to show; a delete → clear.
_PREVIEW_PATH = os.path.join(_STATE_DIR, "dictate-preview.json")
# Beside dictate.py; the short name is how the launcher checks for it.
_PREVIEW_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview.py")


def _stream_result_path(pid: int | None = None) -> str:
    """The result document for one worker, fenced by its process identity."""
    pid = os.getpid() if pid is None else pid
    return os.path.join(os.path.dirname(_STREAM_RESULT_PATH), f"dictate-stream.{pid}.json")


def _stream_result_paths() -> list[str]:
    """Every per-worker result document the state dir currently holds, whoever wrote it.

    The sweep side of the per-pid naming. The current worker's document is found by its pid; a
    predecessor's — written LATE, after its own stop had already cleared up — is found by nothing,
    because the only pid anybody still holds is the new worker's. That file holds dictated speech,
    so it is found here by pattern instead of by name."""
    directory = os.path.dirname(_STREAM_RESULT_PATH)
    try:
        names = os.listdir(directory)
    except OSError:
        return []  # no state dir, nothing to sweep — the same non-event as an absent file
    return sorted(os.path.join(directory, name) for name in names if _STREAM_RESULT_RE.fullmatch(name))


# CROSS-SCRIPT CONTRACT (keep in sync with speak.py): speak.py records its live speaking chain in
# playing.pid — space-separated PIDs, its python process first, then the current player/command
# child. The echo guard below reads THIS file to stop in-flight playback before recording, and
# verifies each pid via /proc/<pid>/cmdline before signalling (PID-reuse guard), exactly like
# speak.py's own take_over does.
# The belt-and-suspenders half (windowsill#101) reads last-spoken — the text speak.py just voiced —
# and drops any transcript that matches it after transcription.
_SPEAK_PID_PATH = os.path.join(_STATE_DIR, "playing.pid")

# When the macOS Accessibility permission for osascript keystroke injection was declined, this
# marker stops the paste retry until a later permission probe confirms that Accessibility was
# restored — the next toggle then clears the marker and retries the keystroke path.
_PASTE_DENIED_PATH = os.path.join(_STATE_DIR, "dictate-paste-denied")

# Paste-lock guard (windowsill#50): from stop-toggle until paste completes, the process that is
# transcribing/pasting records its pid here.  A start-toggle during that window is ignored so the
# previous paste cannot land mid-recording — the "lags one recording behind" symptom.
_PASTE_LOCK_PATH = os.path.join(_STATE_DIR, "dictate-paste-lock")

# A freshly O_EXCL-claimed pidfile holds no pid for only milliseconds (until the recorder is
# spawned and its pid written). An EMPTY/unparseable pidfile older than this is dead garbage from
# a crashed invocation and is removed — otherwise the toggle would wedge until a manual rm.
_CLAIM_GRACE_SECONDS = 5.0


def log(message: str) -> None:
    try:
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > 1_000_000:
            open(_LOG_PATH, "w").close()
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _is_loopback_host(host: str) -> bool:
    """True only for addresses that point at the local machine — the one case where plaintext is safe."""
    return host == "localhost" or host == "::1" or host.startswith("127.")


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else {}
    except FileNotFoundError:
        return {}  # no config at all is the normal zero-setup case — not worth a log line
    except (OSError, ValueError) as err:
        # ValueError covers both corrupt JSON and UnicodeDecodeError (a non-UTF-8 file); one
        # informative line so a broken config is diagnosable instead of silently ignored
        log(f"config ignored ({path}): {type(err).__name__}: {err}")
        return {}


def cfg(config: dict, dotted: str, default):
    """Walk a dotted path; absent, null and empty-string values all fall back (bash-cfg parity)."""
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    if node is None or node == "":
        return default
    return node


def resolve_debounce_ms(value) -> float:
    """The key-repeat window as the config gave it, or the default when it is not a usable number.

    Every other dictate setting is a `str(...)` that cannot throw; this one is a float, and it is
    read before ANY branch of main runs. A bare `float("soon")` (or `float(None)`) would therefore
    kill the hotkey with a traceback nobody sees — the visible symptom being a key that does
    nothing and an EMPTY dictate.log, which troubleshooting.md teaches the user to read as "the
    binding, not the script". A misconfigured guard falls back instead, loudly, in that same log.
    Infinities are rejected with the rest: a window of `inf` is a hotkey that never fires again."""
    try:
        ms = float(value)
    except (TypeError, ValueError):
        ms = float("nan")
    if not math.isfinite(ms):
        log(f"dictate.debounce_ms is not a usable number ({value!r}) — using {DEBOUNCE_SECONDS * 1000:.0f} ms")
        return DEBOUNCE_SECONDS * 1000
    return max(0.0, ms)  # negative is "off", same as 0 — clamped so the log and the docs agree


def resolve_paste_target(value) -> str:
    """Either "any" (paste wherever focus is at stop-time) or "same-window" (paste only if focus did
    not move) — anything else is a typo, and a typo here resolves to "same-window".

    That is the one place in this file where an unusable value does NOT fall back to the default,
    and the reason is whose typo it is. The default is "any", so the key is absent from every
    config that wants the default: a person who wrote this key at all wrote it to ask for the
    guard, and "same_window" or "samewindow" is that person, not someone who meant "any". Falling
    back to the default here would silently hand them the behaviour they were switching off. The
    cost of guessing wrong is one clipboard tier — the documented default path, still fully
    functional — and the log line below says which value did it."""
    target = str(value)
    if target in ("any", "same-window"):
        return target
    log(f'dictate.paste_target is not a known value ({value!r}) — using "same-window" (the cautious one)')
    return "same-window"


def resolve_stt_provider(name: str):
    """The registry entry for a configured provider name — the default entry, loudly, for a name
    the registry has never heard of.

    A typo here used to land on the OpenAI arm of an if/else silently, and the symptom was a cloud
    call that 404s and degrades to whisper under a log line blaming "the cloud". The fallback is
    unchanged (that IS the historical behaviour); what is new is that it says so."""
    entry = providers.stt_provider(name)
    if entry is None:
        log(f'stt.cloud.provider is not a known provider — using "{providers.DEFAULT_STT}" instead of {name!r}')
        entry = providers.STT_PROVIDERS[providers.DEFAULT_STT]
    return entry


def resolve_stt_language(config: dict):
    """The STT language, where an explicitly-empty ``stt.language`` survives and only a genuinely
    absent key falls back to the top-level ``language``.

    ``cfg`` treats empty as absent on purpose — bash-cfg parity, and the right rule for every other
    setting (``test_empty_string_falls_back_to_default`` pins it for ``stt.endpoint``). This is the
    one setting where that rule inverts: the voice-setup skill writes ``stt.language: ""`` to ask the
    recogniser to auto-detect mixed speech, and a value that collapses to the top-level ``language``
    is exactly the pinned hint that drops the second tongue (windowsill#159). So here an empty STRING
    survives, a null still falls back the way it does everywhere else, and only a missing key
    inherits the top-level ``language`` — which is the escape hatch the skill documents actually
    reaching the providers (Scribe drops ``language_code`` on a falsy value; the local server
    auto-detects)."""
    stt = config.get("stt") if isinstance(config, dict) else None
    if isinstance(stt, dict) and "language" in stt and stt["language"] is not None:
        return stt["language"]
    return cfg(config, "language", "en")


def resolve_settings(config: dict, system: str) -> dict:
    """Every knob dictate-toggle.sh honoured, same names, same defaults, same precedence."""
    # the provider is an ENTRY, never a branch — every per-provider default below comes off it
    entry = resolve_stt_provider(str(cfg(config, "stt.cloud.provider", providers.DEFAULT_STT)))
    return {
        "mode": str(cfg(config, "dictate.mode", "send")),
        "paste_key": str(cfg(config, "dictate.paste_key", "cmd+v" if system == "Darwin" else "ctrl+shift+v")),
        # the shell compared against the literal string "true"; JSON true and "true" both count —
        # and NOTHING else (not 1: `1 in (True,)` is true in Python, so the check is explicit)
        "auto_paste": cfg(config, "dictate.auto_paste", False) is True
        or cfg(config, "dictate.auto_paste", False) == "true",
        # where auto-paste is allowed to land: "any" (the default power behaviour) or "same-window"
        "paste_target": resolve_paste_target(cfg(config, "dictate.paste_target", "any")),
        "recorder": str(cfg(config, "dictate.recorder", "auto")),
        # PipeWire source to record from for echo cancellation — when set, pw-record adds --target
        # to capture from this source (the Echo-Cancel Source) instead of the default mic.
        # If the module is absent the pw-record invocation degrades: --target on an unknown node
        # makes pw-record fall back to the default source, so dictation never blocks.
        "source": str(cfg(config, "dictate.source", "")),
        # milliseconds in the config (what a human reasons about for a keypress), seconds inside
        "debounce_ms": resolve_debounce_ms(cfg(config, "dictate.debounce_ms", DEBOUNCE_SECONDS * 1000)),
        "clipboard": str(cfg(config, "dictate.clipboard", "auto")),
        # On macOS the system sounds are a natural pacing cue (windowsill#50); on other platforms
        # there is no single default sound that works everywhere, so the default is silence.
        "start_sound": str(cfg(config, "dictate.start_sound",
                               "/System/Library/Sounds/Pop.aiff" if system == "Darwin" else "")),
        "stop_sound": str(cfg(config, "dictate.stop_sound",
                              "/System/Library/Sounds/Blow.aiff" if system == "Darwin" else "")),
        "player": str(cfg(config, "speak.player", "afplay" if system == "Darwin" else "aplay -q")),
        "backend": str(cfg(config, "stt.backend", "lan")),
        "endpoint": str(cfg(config, "stt.endpoint", "http://127.0.0.1:8355")),
        # top-level "language" is the one the user sets; ".stt.language" is the advanced escape —
        # and unlike every other setting an explicitly-empty ".stt.language" survives (it is how a
        # user asks the recogniser to auto-detect mixed speech), so it has its own resolver.
        "language": str(resolve_stt_language(config)),
        "stt_model": str(cfg(config, "stt.model", entry.default_model)),
        "stt_command": str(cfg(config, "stt.command", "")),
        # stt.prompt: free-text jargon that biases the recogniser toward the user's recurring
        # vocabulary. ONE key reaches two paths — the cloud OpenAI request (as the `prompt` form
        # field) and the local server (as faster-whisper's initial_prompt, via _transcribe_lan's
        # ?prompt= query) — so a local user sets it in config.json rather than hand-editing the
        # server's systemd unit (VOICE_LOOP_STT_HINT). Trimmed here; the cloud builder truncates it
        # to the API's token cap, the local path sends it whole.
        "stt_prompt": str(cfg(config, "stt.prompt", "")).strip(),
        "stt_provider": entry.name,
        "cloud_endpoint": str(cfg(config, "stt.cloud.endpoint", "")),
        "key_env": str(cfg(config, "stt.cloud.api_key_env", cfg(config, "stt.api_key_env", "VOICE_LOOP_STT_API_KEY"))),
        "key_file": str(cfg(config, "stt.cloud.key_file", "")),
        "timeout": float(cfg(config, "stt.timeout", 60)),
        # Streaming is OPT-IN and defaults OFF (windowsill#99): the batch path is the proven one,
        # and a live socket is a second failure surface a user must ask for. Same "true"/JSON-true
        # spelling as auto_paste, and nothing else counts.
        "streaming": cfg(config, "stt.cloud.streaming", False) is True
        or cfg(config, "stt.cloud.streaming", False) == "true",
        # The PCM shape the recorder table pins, handed to the streaming entry because a live URL
        # declares what the client is about to send and only the client knows that.
        "stream_rate": RECORD_RATE,
        # Live transcript preview while speaking (windowsill#115): opt-in, streaming-only, and
        # silently off when there is no display surface. Same "true"/JSON-true spelling as the rest.
        "preview": cfg(config, "dictate.preview", False) is True
        or cfg(config, "dictate.preview", False) == "true",
    }


def read_key(key_file: str, key_env: str, environ) -> str:
    """key_file wins over the env var; the key itself is NEVER stored in config.json."""
    if key_file:
        path = os.path.expanduser(key_file)
        try:
            with open(path, encoding="utf-8") as fh:
                return re.sub(r"[ \t\r\n]", "", fh.read())
        except (OSError, UnicodeDecodeError) as err:
            # the type name only — never the file's content (it may be a half-corrupt key)
            log(f"key file unreadable ({path}): {type(err).__name__} — falling back to ${key_env}")
    return environ.get(key_env, "")


# --- pure decision tables (unit-tested; no I/O) --------------------------------------------------


def resolve_recorder(recorder: str, system: str, have) -> str:
    """auto -> the platform's preference order; an explicit name is taken as-is."""
    if recorder != "auto":
        return recorder
    if system == "Darwin":
        if have("rec"):  # sox installs the `rec` front-end
            return "sox"
        if have("ffmpeg"):
            return "ffmpeg"
    else:
        if have("pw-record"):
            return "pw-record"
        if have("arecord"):
            return "arecord"
        if have("ffmpeg"):
            return "ffmpeg"
    return ""


def recorder_argv(recorder: str, system: str, wav: str, *, source: str = "") -> list[str]:
    """The exact device/format flags the shell used: 16 kHz, mono, S16 — [] for an unknown name.

    ``source`` is platform-specific:
    * Linux + pw-record — the PipeWire source name (e.g. "Echo-Cancel Source"); when set, --target
      routes capture to that source. An unknown source makes pw-record fall back to the default
      source — dictate never blocks on echo cancellation.
    * Windows + ffmpeg — the DirectShow device name (e.g. "Microphone (Realtek Audio)");
      enumerated at start time when omitted (see _discover_dshow_mic). DirectShow demands the
      explicit device, so an empty source on Windows produces [] — the dictation fails clean
      rather than capturing from a non-existent input.

    An empty source on Linux/macOS is the historic auto behaviour — a device ffmpeg picks itself.
    Windows has no such default, which is why discovery is part of start_recording, not here."""
    if recorder == "pw-record":
        argv = ["pw-record", "--rate", str(RECORD_RATE), "--channels", "1"]
        if source:
            argv.extend(["--target", source])
        argv.append(wav)
        return argv
    if recorder == "arecord":
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(RECORD_RATE), "-c", "1", wav]
    if recorder == "sox":
        return ["rec", "-q", "-r", str(RECORD_RATE), "-c", "1", "-b", "16", wav]
    if recorder == "ffmpeg":
        if system == "Darwin":
            src = ["-f", "avfoundation", "-i", ":default"]
        elif system == "Windows":
            # DirectShow requires `audio=<device>`; no device name is a hard failure path —
            # capture-from-nothing is the silent-mic bug class, so refuse the argv instead.
            if not source:
                return []
            src = ["-f", "dshow", "-i", f"audio={source}"]
        else:
            src = ["-f", "alsa", "-i", "default"]
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", *src, "-ar", str(RECORD_RATE), "-ac", "1", "-y", wav]
    return []


# DirectShow enumeration (windowsill#177). Windows capture has no platform default for ffmpeg's
# `-f dshow -i` device name the way ALSA's `default` and avfoundation's `:default` are defaults;
# the device has to be named, and the right one varies with whatever hardware is plugged in. The
# enumeration runs `ffmpeg -list_devices true -f dshow -i dummy` (the `dummy` trick: ffmpeg refuses
# to open a real device in a list, but `dummy` is enough to make it print the catalogue to stderr)
# and picks the first audio capture device. Override the pick with dictate.source when the user's
# rig misnames it — the ticket calls that defect class an "implementer's guard", not a level-shift.
_DSHOW_DISCOVERY_TIMEOUT = 5.0
_DSHOW_DISCOVERY_ARGV = ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
_DSHOW_AUDIO_HEADER = "DirectShow audio devices"
_DSHOW_VIDEO_HEADER = "DirectShow video devices"
# ffmpeg writes each device line as `[dshow @ ...]  "name"` — the dshow tag prefix is part of
# every device row. Match the first quoted string on the line, ignoring the prefix and any
# trailing text.
_DSHOW_DEVICE_LINE = re.compile(r'"([^"]+)"')


def _parse_dshow_audio_devices(stderr: str) -> str:
    """The first audio capture device ffmpeg listed; '' when the catalog is empty.

    ffmpeg's enumeration alternates 'DirectShow audio devices' and 'DirectShow video devices'
    headers with `  "<device>"` rows under each. The first audio row is the most likely microphone;
    on a multi-input rig (a USB interface, a virtual cable, stereo mix) this is the one an operator
    overrides via dictate.source — picking the wrong one captures silence (#177)."""
    in_audio = False
    for line in stderr.splitlines():
        if _DSHOW_AUDIO_HEADER in line:
            in_audio = True
            continue
        if _DSHOW_VIDEO_HEADER in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = _DSHOW_DEVICE_LINE.search(line)
        if match:
            return match.group(1)
    return ""


def _discover_dshow_mic() -> str:
    """The first DirectShow audio device on Windows; '' on any failure (non-Windows, no ffmpeg,
    timed-out probe, empty catalog). Best-effort: a '' answer keeps dictate running — the next
    step, recorder_argv's empty-source refusal, is what surfaces the missing device as a failed
    start rather than a silent clip.

    Tested via _parse_dshow_audio_devices and a fake subprocess.run — ffmpeg itself is a Windows
    binary that doesn't run on the lane this is developed on, and a 5 s subprocess on each start
    is the cost a hardware-locked enumeration pays anyway."""
    try:
        result = subprocess.run(
            _DSHOW_DISCOVERY_ARGV,
            capture_output=True,
            text=True,
            check=False,
            timeout=_DSHOW_DISCOVERY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0 and not result.stderr:
        return ""
    return _parse_dshow_audio_devices(result.stderr)


# --- belt-and-suspenders echo guard -------------------------------------------------------------
# AEC alone can degrade (misconfigured module, a PipeWire restart that drops the loaded module,
# an amp turned up loud enough to saturate the canceller). The state-store comparison is the
# second belt: a transcript that matches what the assistant just spoke out loud is dropped before
# it reaches the prompt — load-bearing, not optional (windowsill#101 comment 1).


def _normalize_for_echo_check(text: str) -> str:
    """Collapse whitespace, lowercase, strip punctuation — the axes along which the same phrase
    varies between the TTS speaker and the STT recognizer."""
    text = re.sub(r"[^\w\s]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _is_echo_of_last_spoken(transcript: str, last_spoken: str) -> bool:
    """True when the transcript looks like an echo of what the assistant just spoke.

    Two shapes, both caught in the live reproduction:
    - Exact match: the spoken line was captured verbatim (the staged experiment, confidence 0.98).
    - Contained match: the whole spoken line appears inside a longer transcript (the assistant's
      full sentence leaked into a real dictation — the production incident in the tracker).
    """
    t = _normalize_for_echo_check(transcript)
    s = _normalize_for_echo_check(last_spoken)
    if not t or not s:
        return False
    if t == s:
        return True
    # The assistant spoke a substantial line and the whole thing was captured inside a longer
    # transcript. The length floor keeps "the" from muting every dictation.
    if len(s) >= 30 and s in t:
        return True
    return False


def clip_seconds(size_bytes: int) -> float:
    """A 16 kHz mono S16 WAV's audible length from its byte size (header excluded)."""
    return max(0, size_bytes - WAV_HEADER_BYTES) / BYTES_PER_SECOND


def resolve_clipboard(clipboard: str, system: str, have, wayland: bool) -> str:
    """auto -> pbcopy (macOS), wl-copy (a live Wayland session), xclip, wl-copy (installed but no
    $WAYLAND_DISPLAY — XWayland setups), clip.exe (native Windows); an explicit name is taken as-is.

    The WSL trap is closed by ``system`` alone: ``platform.system()`` returns 'Linux' inside WSL,
    so the Windows branch only fires on NATIVE Windows — a binary search of PATH never enters
    it on a Linux/WSL guest even when clip.exe is reachable through interop. Under WSLg the
    session exports $WAYLAND_DISPLAY like any Wayland desktop, so the wl-copy branch already
    fires there (#179 defect 3).
    """
    if clipboard != "auto":
        return clipboard
    if system == "Darwin":
        return "pbcopy"
    if system == "Windows":
        return "clip" if have("clip.exe") else ""
    if wayland and have("wl-copy"):
        return "wl-copy"
    if have("xclip"):
        return "xclip"
    if have("wl-copy"):
        return "wl-copy"
    return ""


def clipboard_commands(tool: str) -> list[list[str]]:
    """The pipe targets for a clipboard tool — wl-copy and xclip fill BOTH selections so that
    middle-click paste works too. [] means no known tool."""
    if tool == "pbcopy":
        return [["pbcopy"]]
    if tool == "wl-copy":
        return [["wl-copy"], ["wl-copy", "--primary"]]
    if tool == "xclip":
        return [["xclip", "-selection", "clipboard"], ["xclip", "-selection", "primary"]]
    if tool == "clip":
        # Native Windows only — clip.exe is not a tier on Linux/WSL even when interop finds it,
        # because resolve_clipboard gates on platform.system() == 'Windows' (WSL reports 'Linux').
        return [["clip.exe"]]
    return []


def pick_paste_tool(system: str, have, ydotool_socket_ok: bool, display: str) -> str:
    """The platform table, in the shell's order: osascript / sendkeys / ydotool(+socket) / wtype / xdotool.

    Native Windows uses ``SendKeys`` via the WSH COM object (a default Windows component, no
    third-party install). The shim is a one-line PowerShell that creates a WScript.Shell and
    calls SendKeys on it — the same automation surface AutoHotKey sits on, just the stdlib
    shortcut. #46's paste-at-focus footgun carries over unchanged: the same-window guard is
    the only thing standing between "I dictated into this app" and "I dictated into whatever
    was focused when I let go of the key", and that ruling is platform-agnostic.
    """
    if system == "Darwin":
        return "osascript" if have("osascript") else ""
    if system == "Windows":
        return "sendkeys" if have("powershell.exe") else ""
    if have("ydotool") and ydotool_socket_ok:
        return "ydotool"
    if have("wtype"):
        return "wtype"
    if display and have("xdotool"):
        return "xdotool"
    return ""


# Map the documented paste_key names to SendKeys syntax. SendKeys '^' is Ctrl, '+' is Shift,
# '%' is Alt, and a key name in braces is the literal key — so `^+v` is Ctrl+Shift+V and
# `+{Insert}` is Shift+Insert. Anything outside this map falls back to plain Ctrl+V, which is
# what `rec` had no rule for and is the safe default for an unknown paste_key on Windows.
_WIN_SENDKEYS_FOR_PASTE = {
    "ctrl+shift+v": "^+v",
    "ctrl+v": "^v",
    "shift+insert": "+{Insert}",
    "shift+ins": "+{Insert}",
}


def _win_sendkeys_argv(keys: str) -> list[str]:
    """Build a PowerShell argv that calls WScript.Shell.SendKeys on the given key sequence.

    The script is a single statement, -Command-compatible, and the keystrokes go through the
    shared WSH COM object that every Windows automation tool (AutoHotKey, VBA, the GUI) sits
    on, so no extra install is needed. The -NoProfile flag avoids hangs on user-land profiles
    while a hotkey is being held."""
    return [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        f"(New-Object -ComObject WScript.Shell).SendKeys('{keys}')",
    ]


def paste_plan(tool: str, paste_key: str, enter: bool) -> list[tuple[float, list[str], bool]]:
    """(delay_before_s, argv, required) steps for one paste. The paste keystroke is required (a
    failure falls back to the clipboard tier); the Enter press is best-effort, like the shell's
    ``|| true``. [] means the tool is unknown."""
    steps: list[tuple[float, list[str], bool]] = []
    if tool == "osascript":
        using = {
            "ctrl+shift+v": "{control down, shift down}",
            "shift+insert": "{command down}",  # no Insert key on mac keyboards — the native paste
        }.get(paste_key, "{command down}")
        steps.append((0.0, ["osascript", "-e", f'tell application "System Events" to keystroke "v" using {using}'], True))
        if enter:
            steps.append((0.25, ["osascript", "-e", 'tell application "System Events" to key code 36'], False))
    elif tool == "sendkeys":
        # SendKeys has no async-after delay (a race-free sequence against an open clipboard), and
        # the same 250 ms settle as the other paste tools to give the focused app time to register
        # the paste before the Enter arrives.
        keys = _WIN_SENDKEYS_FOR_PASTE.get(paste_key, "^v")
        steps.append((0.0, _win_sendkeys_argv(keys), True))
        if enter:
            # `{Enter}` — single braces, exactly like `+{Insert}` two lines up. PowerShell
            # single-quoted strings are VERBATIM: there is no brace-doubling escape in them, so a
            # doubled `{{Enter}}` reaches SendKeys as the literal `{{Enter}}`, SendKeys parses the
            # token as `{Enter` and raises, and because this step is required=False the failure is
            # silent — the default `send` mode pastes the transcript and never submits it.
            steps.append((0.25, _win_sendkeys_argv("{Enter}"), False))
    elif tool == "ydotool":
        steps.append((0.15, ["ydotool", "key", paste_key], True))
        if enter:
            steps.append((0.25, ["ydotool", "key", "enter"], False))
    elif tool == "wtype":
        argv = {
            "ctrl+shift+v": ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"],
            "shift+insert": ["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"],
        }.get(paste_key, ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"])
        steps.append((0.0, argv, True))
        if enter:
            steps.append((0.25, ["wtype", "-k", "Return"], False))
    elif tool == "xdotool":
        key = "shift+Insert" if paste_key == "shift+insert" else paste_key
        steps.append((0.0, ["xdotool", "key", key], True))
        if enter:
            steps.append((0.25, ["xdotool", "key", "Return"], False))
    return steps


def focus_probe_argv(system: str, have, wayland: bool, display: str) -> list[str]:
    """The command that prints an identity for whatever is focused right now — [] where the
    platform cannot answer.

    macOS: the frontmost application PROCESS, which is what System Events can name without a
    per-window accessibility walk. It is coarser than a window — two Terminal windows are one
    identity — and that is stated in the docs rather than papered over.

    X11: xdotool's active window id, a genuine per-window identity.

    Wayland: nothing. There is no portable "what is focused" query — wlroots, KDE and Mutter each
    answer it differently or not at all — and xdotool under XWayland would answer for the X subset
    only, which is worse than not answering: it reports a *stale* id after a switch to a native
    Wayland window, and a wrong identity is a guard that fires at the wrong times. So $WAYLAND_DISPLAY
    means [] whatever else is installed, and the guard degrades to "any"."""
    if system == "Darwin":
        if not have("osascript"):
            return []
        return [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
        ]
    if wayland:
        return []
    if display and have("xdotool"):
        return ["xdotool", "getactivewindow"]
    return []


def focus_changed(started: str, current: str) -> bool:
    """True only when BOTH ends named a focus and the names differ.

    Either half unknown ('' — the guard was off at start, the state dir was unwritable, the probe
    is missing, the platform cannot answer, the query failed) means the question was never
    answered, and an unanswered question degrades to "any": paste. Suppressing on an unknown is the
    other failure, and it is the worse one — dictation that silently stops pasting on every Wayland
    desktop, with the user's only evidence a notification saying focus moved when it did not."""
    return bool(started) and bool(current) and started != current


def same_window_guard_on(s: dict) -> bool:
    """Whether this run needs the focus probe at all.

    Only the auto-paste tier can paste into the wrong window: on the default clipboard tier the
    human presses the paste key themselves, in whatever window they meant, so there is nothing to
    guard and no reason to spend a probe on every recording."""
    return bool(s["auto_paste"]) and s["paste_target"] == "same-window"


# --- paste denial: an Accessibility marker persists until a re-probe clears it --------------------


_ACCESSIBILITY_PROBE = [
    "osascript",
    "-e",
    'tell application "System Events" to get name of every window of first application process whose frontmost is true',
]


def _paste_denied() -> bool:
    """Has a previous invocation recorded a macOS Accessibility denial?

    The marker persists across invocations, so a user who declines the permission is not prompted
    again on every toggle.  ``_run_paste`` probes the permission before honouring the marker, which
    lets a later grant in System Settings clear the stale denial and retry auto-paste.
    """
    return os.path.exists(_PASTE_DENIED_PATH)


def _mark_paste_denied() -> None:
    """Record that paste was denied so the next toggle skips the keystroke path.

    Best-effort: if the state dir is unwritable the toggle still falls back to clipboard — it just
    retries the (failing) keystroke on the next invocation."""
    try:
        with open(_PASTE_DENIED_PATH, "w", encoding="utf-8") as fh:
            fh.write("denied")
    except OSError:
        pass


def _clear_paste_denied() -> None:
    """Forget a denial after an Accessibility probe confirms permission was restored."""
    try:
        os.unlink(_PASTE_DENIED_PATH)
    except OSError:
        pass


# --- paste-lock guard (windowsill#50): one paste at a time ----------------------------------------


def _paste_locked() -> bool:
    """Is a previous stop still transcribing/pasting?

    Checks that the lock file exists and its pid is still alive — the same staleness pattern the
    recorder pidfile uses.  A stale lock (dead pid) is removed so one crashed stop cannot wedge
    the toggle forever."""
    try:
        with open(_PASTE_LOCK_PATH, encoding="utf-8") as fh:
            lock_pid = int(fh.read().strip() or "-1")
    except (OSError, ValueError):
        return False
    if lock_pid <= 0 or lock_pid == os.getpid():
        return False
    if _pid_alive(lock_pid):
        return True
    # Stale: the process that held the lock is gone — clean up so the toggle is not wedged.
    try:
        os.unlink(_PASTE_LOCK_PATH)
    except OSError:
        pass
    return False


def _claim_paste_lock() -> None:
    """Record that THIS process owns the transcription-and-paste tail — best-effort.

    A lock that cannot be written fails open (like the debounce stamp): the guard degrades to
    the old behaviour rather than wedging the toggle."""
    try:
        with open(_PASTE_LOCK_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError as err:
        log(f"paste lock not written: {err}")


def _release_paste_lock() -> None:
    """The paste tail is done — free the lock for the next recording cycle."""
    try:
        os.unlink(_PASTE_LOCK_PATH)
    except OSError:
        pass


def _accessibility_permission_regranted() -> bool:
    """True when System Events now accepts an Accessibility query.

    The probe reads window data from the frontmost process, which crosses the Accessibility grant;
    merely reading the frontmost process itself would only exercise the Automation grant.  This is
    a harmless, bounded probe rather than another paste attempt: it lets a user who later grants
    Accessibility recover from the persistent denial marker without sending a keystroke into the
    focused application first.
    """
    try:
        result = subprocess.run(
            _ACCESSIBILITY_PROBE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=FOCUS_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _is_accessibility_denial(stderr: bytes) -> bool:
    """True when the osascript stderr reads like a macOS Accessibility permission denial.

    The prose varies with the macOS locale, but the numeric OSStatus codes are stable.  Match both
    the known English diagnostics and the locale-independent ``-1743`` / ``1002`` codes.
    """
    text = stderr.decode("utf-8", "replace").lower()
    return (
        "not allowed to send keystrokes" in text
        or "not authorized to send apple events to system events" in text
        or re.search(r"\((?:-1743|1002)\)\s*\Z", text) is not None
    )


def _log_stderr(stderr_bytes: bytes) -> None:
    """Write captured subprocess stderr to the dictate log, best-effort."""
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(stderr_bytes.decode("utf-8", "replace"))
    except OSError:
        pass


# The multipart builder moved into the registry with the request-build axis it belongs to; this
# alias keeps the name where dictate.py's own callers (and its tests) have always found it.
multipart_form = providers.multipart_form


def transcript_from_response(raw: bytes | None) -> str:
    """The ``text`` field of a JSON response, stripped — '' for anything malformed (parity with
    the shell's ``python3 -c 'json.load(...).get("text","")'`` tail, which printed '' on error).

    This is the LOCAL server's shape, which is also what the OpenAI and ElevenLabs entries parse
    with; a cloud response goes through its own entry's ``transcript`` instead — and it reads the
    reader's None ("no transcript field") as the degrade signal, where this path has nothing to
    degrade to and flattens it to ''."""
    return providers.text_field(providers.decode(raw)) or ""


def applescript_escape(text: str) -> str:
    """Escape a string for interpolation into an AppleScript double-quoted literal.

    Backslash FIRST, then the quote — the message may carry config-controlled text (paste_key),
    and an unescaped quote would otherwise let it break out of the literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# --- runtime glue --------------------------------------------------------------------------------


def note(message: str, system: str) -> None:
    """Desktop notification — best-effort, never fatal."""
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{applescript_escape(message)}" with title "voice-loop"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "-t", "1500", "voice-loop", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except OSError:
        pass


def current_focus(system: str, environ=None) -> str:
    """Ask the platform what is focused — '' for "cannot say", which is never an error here.

    Bounded like every other spawn in this file: an argv list (never a shell string), a mandatory
    timeout, check=False with the exit code read rather than raised. A probe that fails, times out
    or is not installed simply leaves the guard with an unknown, which pastes."""
    environ = os.environ if environ is None else environ
    argv = focus_probe_argv(system, shutil.which, bool(environ.get("WAYLAND_DISPLAY")), environ.get("DISPLAY", ""))
    if not argv:
        return ""
    try:
        result = subprocess.run(argv, capture_output=True, timeout=FOCUS_PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as err:
        log(f"focus probe failed ({argv[0]}): {type(err).__name__}")
        return ""
    if result.returncode != 0:
        log(f"focus probe returned {result.returncode} ({argv[0]}) — focus unknown")
        return ""
    return result.stdout.decode("utf-8", "replace").strip()


def remember_focus(system: str) -> None:
    """Record the focus this recording started in, for stop_and_transcribe to compare against."""
    identity = current_focus(system)
    try:
        with open(_FOCUS_PATH, "w", encoding="utf-8") as fh:
            fh.write(identity)
    except OSError as err:
        # fail open, like the debounce stamp: a guard that cannot keep state must not become a
        # dictation that never pastes — stop reads no identity and treats the target as "any"
        log(f"focus not recorded: {err} — the same-window guard degrades to paste-at-focus")
        return
    log(f"focus at start: {identity or 'unknown — the same-window guard degrades to paste-at-focus'}")


def take_remembered_focus() -> str:
    """Read and CONSUME the start-time focus — '' when there is none.

    Consumed rather than merely read so that a stop which never reaches the paste decision (a clip
    below the guard, an empty transcription) cannot leave an identity behind for a later recording
    to compare itself against."""
    try:
        with open(_FOCUS_PATH, encoding="utf-8") as fh:
            identity = fh.read().strip()
    except OSError:
        identity = ""
    try:
        os.unlink(_FOCUS_PATH)
    except OSError:
        pass
    return identity


def sound(path: str, player: str) -> None:
    """Fire-and-forget feedback beep through speak.player — absent file or player is silence."""
    if not path or not os.path.isfile(path):
        return
    try:
        subprocess.Popen(shlex.split(player) + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        pass


def _post_multipart(url: str, headers: dict, body: bytes, boundary: str, timeout: float) -> bytes | None:
    """POST a multipart form — the LAN path's one shape, and the shape most cloud entries build."""
    return _post_bytes(url, headers, body, f"multipart/form-data; boundary={boundary}", timeout)


def _post_bytes(url: str, headers: dict, body: bytes, content_type: str, timeout: float) -> bytes | None:
    """POST a body of any encoding, return it even on an HTTP error (the body is the diagnosis).
    None only when the server was unreachable. Proxies bypassed (parity with ``curl --noproxy '*'``).

    The content type is an argument rather than a constant because it is a per-provider axis:
    Deepgram takes the WAV as the whole request body (``audio/wav``), not as a form part."""
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type, **headers},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        try:
            return err.read()
        except OSError:
            return b""
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        log(f"stt unreachable: {getattr(err, 'reason', err)}")
        return None


def transcribe(s: dict) -> str:
    """The recorded WAV -> text, by whichever backend is configured.

    When the cloud backend fails (network, auth, quota, API error) the call degrades to
    the local whisper server — a logged fallback, never a silent dead mic. The degrade
    is one-shot: it does not retry the cloud, and it does not retry the fallback.
    """
    if s["stt_command"]:
        # local engine without a server: the command gets the wav path as its last argument
        # and prints the transcript (e.g. whisper.cpp: "whisper-cli -m model.bin -nt -f").
        # No timeout on purpose — stt.timeout bounds the HTTP backends (curl -m parity); a local
        # engine on a slow machine may legitimately take longer, and the shell never capped it.
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
                out = subprocess.run(
                    ["/bin/sh", "-c", f'{s["stt_command"]} "{_WAV_PATH}"'],
                    stdout=subprocess.PIPE,
                    stderr=errlog,
                    check=False,
                )
        except OSError as err:
            log(f"stt command failed: {err}")
            return ""
        return out.stdout.decode("utf-8", "replace").strip()

    try:
        with open(_WAV_PATH, "rb") as fh:
            wav_bytes = fh.read()
    except OSError:
        return ""
    if s["backend"] == "cloud":
        boundary = uuid.uuid4().hex
        text = _transcribe_cloud(s, wav_bytes, boundary)
        if text is not None:
            return text
        # Cloud failed — degrade to the local whisper server.  Logged so the operator
        # can see that a transcription happened at all and that it came from the fallback.
        log("cloud stt failed — falling back to local whisper")
        # The degrade always goes to the localhost server, not to the configured cloud
        # endpoint: a cloud user's ``stt.endpoint`` may point at a remote API.
        return _transcribe_lan("http://127.0.0.1:8355", s["language"], wav_bytes, s["timeout"], s["stt_prompt"])
    return _transcribe_lan(s["endpoint"], s["language"], wav_bytes, s["timeout"], s["stt_prompt"])


def _transcribe_lan(endpoint: str, language: str, wav_bytes: bytes, timeout: float, prompt: str = "") -> str:
    """POST the WAV to a voice-loop server's /stt endpoint — the LAN path and the
    cloud-degrade target both use this.

    ``prompt`` carries the configured jargon (``stt.prompt``) as a ``?prompt=`` query parameter so the
    local server feeds it to faster-whisper as ``initial_prompt`` — the same key that primes the cloud
    OpenAI request, reaching the local path without a systemd edit. Omitted when empty, so the server
    falls back to its own ``VOICE_LOOP_STT_HINT`` default (an operator-wide hint) and the request
    shape is unchanged for users who set nothing."""
    boundary = uuid.uuid4().hex
    body = multipart_form({}, "audio", "dictate.wav", wav_bytes, boundary)
    query = f"language={urllib.parse.quote(language)}"
    if prompt:
        query += f"&prompt={urllib.parse.quote(prompt)}"
    url = f"{endpoint}/stt?{query}"
    raw = _post_multipart(url, {}, body, boundary, timeout)
    return transcript_from_response(raw)


def _transcribe_cloud(s: dict, wav_bytes: bytes, boundary: str) -> str | None:
    """Try cloud transcription. Returns the transcript on success, or None when the
    caller should degrade to the local whisper path.

    The caller logs the degrade reason — this function only logs the specific failure
    that made the cloud path unusable (missing key, network error, API error document).

    There is NO per-provider branch here and there must never be one: the configured provider is
    an entry in the registry, and every axis it varies — host, path, body encoding, auth header,
    which env vars hold its key, where the transcript lives, how its error documents read — is
    read off that entry (see providers.py).
    """
    entry = resolve_stt_provider(s["stt_provider"])
    key_envs = entry.key_envs(s["key_env"])
    key = ""
    for env in key_envs:
        key = read_key(s["key_file"], env, os.environ)
        if key:
            break
    if not key:
        tried = ", ".join(f"${env}" for env in key_envs)
        log(f"cloud stt: no key for {entry.name} (key_file unset/unreadable, and {tried} empty)")
        return None

    request = entry.request(s, key, wav_bytes, boundary)
    parsed = urllib.parse.urlsplit(request.url)
    if parsed.scheme == "http" and parsed.hostname and not _is_loopback_host(parsed.hostname):
        log(
            f"cloud stt endpoint is http:// to {parsed.hostname} — "
            "the API key, audio and transcript travel in the clear"
        )
    raw = _post_bytes(request.url, request.headers, request.body, request.content_type, s["timeout"])
    if raw is None:
        return None  # network error — already logged by _post_bytes
    data = providers.decode(raw)
    text = entry.transcript(data)
    if text is None:
        # The body carries no transcript field at all — an API error document, an empty body, or
        # something this provider's parser does not recognise. Log its shape so the operator can
        # tell a quota error from a bad model name.
        #
        # An EMPTY transcript is deliberately NOT this case: a silent clip transcribes to "" and
        # that is a success. Treating it as a failure logged a cloud error that never happened and
        # posted the clip a second time, at localhost, on every silent toggle (windowsill#93).
        if data is None:
            log(f"cloud stt returned undecodable response: {raw[:200]!r}")
        else:
            log(f"cloud stt returned an error: {entry.error_summary(data)}")
        return None
    return text


def _pid_alive(pid: int) -> bool:
    """Is *pid* a live process? POSIX uses the signal-0 idiom; on Windows there IS no signal 0 —
    os.kill maps straight to TerminateProcess, so the same call would TERMINATE the very process
    it is probing. The Windows arm opens a kernel32 handle and polls it with a zero timeout — no
    subprocess, because both callers ask this per poll iteration and a spawned tasklist.exe per
    ask is a per-iteration process launch."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover — exercised on Windows; the lane runs Linux
        import ctypes

        try:
            # use_last_error (not a bare windll): ctypes itself makes Win32 calls between our
            # failing call and the error read, so kernel32.GetLastError() can report a stale
            # code and misread access-denied as no-such-process. ctypes.get_last_error() is
            # captured at the boundary and is the reliable read.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # stdlib; Windows arm

            # Without argtypes/restype ctypes truncates a 64-bit handle to a C int on 64-bit
            # Python — exactly the class of defect that only shows up on the platform no lane
            # runs. Declare every signature.
            kernel32.OpenProcess.argtypes = (
                ctypes.c_uint32,  # dwDesiredAccess
                ctypes.c_int,  # bInheritHandle
                ctypes.c_uint32,  # dwProcessId
            )
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = (
                ctypes.c_void_p,  # hHandle
                ctypes.c_uint32,  # dwMilliseconds
            )
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int

            # SYNCHRONIZE (0x00100000) lets us wait; PROCESS_QUERY_LIMITED_INFORMATION
            # (0x1000) is the least-privilege right that still permits the wait below.
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            ERROR_INVALID_PARAMETER = 87
            WAIT_OBJECT_0 = 0
            WAIT_TIMEOUT = 0x102

            handle = kernel32.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                error = ctypes.get_last_error()
                if error == ERROR_ACCESS_DENIED:
                    # Exists but is not ours to open — the Windows twin of the POSIX arm's
                    # PermissionError → True, kept answering the same way on both arms.
                    return True
                # ERROR_INVALID_PARAMETER (87) is "no such process"; any other failure we
                # cannot ask: report dead rather than kill what we cannot see.
                return False
            try:
                wait = kernel32.WaitForSingleObject(handle, 0)
                if wait == WAIT_TIMEOUT:
                    return True  # still running
                if wait == WAIT_OBJECT_0:
                    return False  # exited
                return False  # WAIT_FAILED or anything else: cannot ask, report dead
            finally:
                kernel32.CloseHandle(handle)  # every path that opened it closes it
        except (OSError, AttributeError):
            return False  # kernel32 is not what we expect: report dead, do not fall back to spawn
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win_console_ctrl_c(pid: int) -> bool:  # pragma: no cover — exercised on Windows; the lane runs Linux
    """Send CTRL_C_EVENT to the console *pid* lives in — True if the event went out.

    This is the Windows equivalent of SIGINT, and the only graceful stop that reaches a process
    we did not spawn: the start toggle that did spawn the recorder has exited by stop time, so
    nobody still holds the recorder's stdin to write `q` into. Attaching to the target's console
    and raising CTRL_C_EVENT takes the path ffmpeg's own console Ctrl+C handler runs — it
    finalizes the output container — which TerminateProcess never does. Our own process ignores
    the event for the duration (SetConsoleCtrlHandler NULL/True), because the event reaches every
    process attached to that console, this one included."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # stdlib; this function is Windows-only by caller
    was_attached = False
    target_attached = False
    handler_set = False
    try:
        # GetConsoleCP returns zero when this process has no console, unlike a window-handle
        # probe which does not describe pseudoconsole attachment reliably.
        was_attached = bool(kernel32.GetConsoleCP())
        if was_attached:
            kernel32.FreeConsole()
        if not kernel32.AttachConsole(pid):
            return False  # the process (or its console) is already gone
        target_attached = True
        kernel32.SetConsoleCtrlHandler(None, True)
        handler_set = True
        return bool(kernel32.GenerateConsoleCtrlEvent(0, 0))  # 0 = CTRL_C_EVENT, console-wide
    except Exception:
        return False
    finally:
        # Detach BEFORE restoring the handler, on EVERY exit path: SetConsoleCtrlHandler(None,
        # False) re-arms this process's default Ctrl+C handler, and while we are still attached to
        # the target console the CTRL_C_EVENT just raised can still reach us — killing the stop
        # toggle before it transcribes the speech it stopped for. FreeConsole first, so the
        # re-armed handler can never receive that event.
        if target_attached:
            try:
                kernel32.FreeConsole()
            except Exception:
                pass
        if handler_set:
            try:
                kernel32.SetConsoleCtrlHandler(None, False)
            except Exception:
                pass
        if was_attached:
            try:
                kernel32.AttachConsole(-1)  # ATTACH_PARENT_PROCESS
            except Exception:
                pass


def _win_pid_is_recorder(pid: int, recorder: str) -> bool:
    """PID-reuse guard for the Windows stop path: is *pid* still OUR recorder?

    The pidfile outlives its process and the kernel recycles PIDs, so the pid main() reads out of
    dictate.pid can belong to somebody else by stop time — and _win_console_ctrl_c signals the WHOLE
    console that pid is attached to, after which _stop_recorder TerminateProcess-es it. Before
    either, confirm the process image is the recorder this recording would have spawned. The image
    name is read through the handle _pid_alive already opens (PROCESS_QUERY_LIMITED_INFORMATION is
    enough for QueryFullProcessImageNameW) — no subprocess, because the stop path must stay cheap.
    """
    if pid <= 0 or not recorder:
        return False  # plain-Python guard, left under coverage: no Windows-only API before this
    import ctypes  # pragma: no cover — exercised on Windows; the lane runs Linux

    try:  # pragma: no cover — everything from here reaches kernel32
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # stdlib; Windows arm
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,  # dwDesiredAccess
            ctypes.c_int,  # bInheritHandle
            ctypes.c_uint32,  # dwProcessId
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = (
            ctypes.c_void_p,  # hProcess
            ctypes.c_uint32,  # dwFlags (0 = the Win32 path form)
            ctypes.c_wchar_p,  # lpExeName
            ctypes.POINTER(ctypes.c_uint32),  # lpdwSize (in TCHARs, in and out)
        )
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # gone, or not ours to inspect — either way, not something to signal
        try:
            size = ctypes.c_uint32(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return False
            image = os.path.basename(buffer.value).lower()
            if image.endswith(".exe"):
                image = image[: -len(".exe")]
            # sox's front-end binary is `rec` (recorder_argv returns ["rec", ...]); every other
            # recorder key IS the executable name.
            expected = ("rec" if recorder == "sox" else recorder).lower()
            return image == expected
        finally:
            kernel32.CloseHandle(handle)  # every path that opened it closes it
    except (OSError, AttributeError):  # pragma: no cover — only the excluded Windows arm can raise these
        return False  # kernel32 is not what we expect: cannot verify, do not signal


def _stop_recorder(pid: int, system: str, recorder: str = "") -> None:
    """Ask the recorder to finalize, then make it stop.

    POSIX: SIGINT lets sox/ffmpeg write the WAV header and flush the tail; TERM is the fallback
    for a recorder that ignores INT (pw-record/arecord). Windows: os.kill(pid, SIGINT) is an
    unconditional TerminateProcess, which kills ffmpeg mid-write and leaves an unreadable WAV
    with no finalized header — the stop toggle would then transcribe nothing, every time. The
    Windows arm raises CTRL_C_EVENT in the recorder's console instead (see _win_console_ctrl_c)
    so ffmpeg closes the container; TERM/TerminateProcess remains the fallback, exactly the
    same INT-then-TERM shape as POSIX.

    Windows first verifies the pid is still OUR recorder (see _win_pid_is_recorder): the pidfile
    outlives its process and the kernel recycles PIDs, so a stale pid must not get a Ctrl+C in an
    unrelated console group, nor the TerminateProcess that follows it."""
    if system == "Windows":
        if not _win_pid_is_recorder(pid, recorder):  # pragma: no cover — exercised on Windows
            return  # pragma: no cover — a recycled pid: signal nobody, terminate nobody
        _win_console_ctrl_c(pid)  # pragma: no cover — exercised on Windows; the lane runs Linux
    else:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
    if not _wait_gone(pid):
        try:
            os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess — the fallback of last resort
        except OSError:
            pass
        _wait_gone(pid)


def _cmdline_of(pid: int) -> str | None:
    """/proc/<pid>/cmdline with NULs as spaces — None when unreadable (process already gone, or
    not ours to inspect). Linux-only by construction; callers gate on the platform.
    Duplicated helper — keep in sync with speak.py."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def pid_looks_like_speak(pid: int, read_cmdline=_cmdline_of, platform_id: str = sys.platform) -> bool:
    """PID-reuse guard (duplicated helper — keep in sync with speak.py): a pidfile outlives its
    process and the kernel recycles PIDs, so before SIGTERMing a recorded pid, confirm it still
    looks like the voice-loop speaking chain: the player child's argv carries the
    "voice-loop-speak-" temp-WAV prefix, the python half of the chain carries "speak.py".

    Non-Linux has no /proc/<pid>/cmdline; rather than depend on parsing `ps` there, the check is
    skipped and the historical raw-signal behaviour kept (the pidfile is same-user state, so the
    blast radius of a stale pid is one same-user SIGTERM — unchanged from before). On Linux an
    unreadable cmdline means the process is already gone (nothing to stop) or is not ours to
    inspect (then not ours to signal either) — both mean: do not signal."""
    if not platform_id.startswith("linux"):
        return True
    cmdline = read_cmdline(pid)
    if cmdline is None:
        return False
    return "voice-loop-speak" in cmdline or "speak.py" in cmdline


def stop_speak_playback() -> None:
    """Echo guard: never record our own speakers — stop any in-flight speak playback first
    (windowsill#3).

    Primary path (the cross-script contract, see _SPEAK_PID_PATH): read speak.py's playing.pid
    and SIGTERM exactly the PIDs it records, each identity-checked — the same semantics as
    speak.py's own take_over. Killing the python half of the chain also stops a tts.command
    player: speak's SIGTERM handler terminates its child, whose own argv carries no marker.
    The historical pkill pattern-kill runs ONLY when no pidfile exists (a pre-pidfile speak, or a
    chain that died without cleanup): it misses tts.command players and can substring-match
    innocent processes, so it is the fallback, never the rule."""
    try:
        with open(_SPEAK_PID_PATH, encoding="utf-8") as fh:
            tokens = fh.read().split()
    except OSError:
        tokens = None
    if tokens is None:
        if os.name == "nt":  # pkill and os.getuid are POSIX-only; Windows has no pattern fallback
            return
        try:
            subprocess.run(
                ["pkill", "-u", str(os.getuid()), "-f", "voice-loop-speak"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
        return
    for token in tokens:
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0 and pid != os.getpid() and pid_looks_like_speak(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


# --- streaming dictation: the pure parts (windowsill#99) -----------------------------------------


def streaming_wanted(s: dict) -> bool:
    """Does THIS recording get a live socket?

    Three conditions, and all three are the user's or the registry's, never a provider name in an
    if: the cloud backend is the one that has a socket to open, ``stt.cloud.streaming`` is the
    opt-in, and the configured provider's entry actually carries a streaming variant. A config that
    asks for streaming from a provider that has none is answered by the batch path — with a line in
    the log, because a silently ignored setting is how a user concludes the feature is broken."""
    if not s["streaming"] or s["backend"] != "cloud" or s["stt_command"]:
        return False
    entry = resolve_stt_provider(s["stt_provider"])
    if entry.streaming is None:
        log(f"stt.cloud.streaming is on but {entry.name} has no streaming variant — using the batch path")
        return False
    return True


def wav_data_offset(head: bytes) -> int:
    """Where the PCM starts inside a WAV, or -1 when this is not a readable RIFF/WAVE header.

    A live socket is fed RAW samples, so the header must be skipped — and skipping a fixed 44 bytes
    is only right for the canonical layout. ffmpeg writes a LIST/INFO chunk before `data`, and 44
    bytes into one of those files is the middle of a metadata chunk: the first quarter-second of
    every dictation would be transcribed as noise. So the `data` chunk is FOUND, never assumed."""
    if not head.startswith(b"RIFF") or head[8:12] != b"WAVE":
        return -1
    index = head.find(b"data", 12)
    if index < 0 or len(head) < index + 8:
        return -1
    return index + 8  # the chunk id and its 4-byte size, then the samples


def assemble_stream_text(finals: list[str]) -> str:
    """The finals, in arrival order, as one dictation.

    Finals only (an interim is a guess the next message rewrites), single-spaced, empties dropped —
    a live socket emits empty finals for the pauses between phrases, and joining them naively is how
    a transcript arrives full of double spaces."""
    return " ".join(part.strip() for part in finals if part.strip())


# --- streaming dictation: the worker's own state -------------------------------------------------


def _write_stream_result(result: dict) -> None:
    """The worker's ONE write, temp-then-replace so the stop toggle cannot read a half-written
    answer and conclude the stream produced nothing. 0600: it holds the dictated words.

    The result path carries this worker's pid, so a late predecessor can never replace the current
    worker's answer. The pid is also retained in the document as a defensive fence for readers."""
    pid = os.getpid()
    result = {**result, "pid": pid}
    result_path = _stream_result_path(pid)
    try:
        fd, tmp = tempfile.mkstemp(prefix="voice-loop-stream-", dir=os.path.dirname(result_path))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        os.replace(tmp, result_path)
    except OSError as err:
        # The stop toggle will see no result and use the recorded clip — the degrade, reached by a
        # different road. Worth one line, because a state dir that will not write is a real fault.
        log(f"stream result not written: {err}")


def _read_stream_result(pid: int | None = None) -> dict | None:
    """The worker's answer, or None while it has not finished (or could not be read at all).

    ``pid`` is a FENCING check, and it closes a real window: a worker that outstayed its stop
    toggle (a short clip discards it without waiting, and a wedged socket can be SIGKILLed) writes
    its document LATE — possibly after the next recording has already started its own worker. A
    result is only this stop's answer if the worker that wrote it is the one whose pid we hold; an
    older worker's transcript pasted into a later recording is precisely the #50 class of bug."""
    try:
        with open(_stream_result_path(pid), encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    if pid is not None and loaded.get("pid") != pid:
        return None
    return loaded


def _read_stream_pid() -> int | None:
    try:
        with open(_STREAM_PID_PATH, encoding="utf-8") as fh:
            pid = int(fh.read().strip() or "-1")
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and pid != os.getpid() else None


def pid_looks_like_stream_worker(pid: int, read_cmdline=_cmdline_of, platform_id: str = sys.platform) -> bool:
    """PID-reuse guard for the worker, the same shape as the speak-chain one above: a pidfile
    outlives its process and the kernel recycles PIDs. The worker's argv carries this script's
    name and the ``stream-worker`` word, which nothing else on the machine does."""
    if not platform_id.startswith("linux"):
        return True
    cmdline = read_cmdline(pid)
    if cmdline is None:
        return False
    return STREAM_WORKER_ARG in cmdline and "dictate.py" in cmdline


def clear_stream_state(*, stop_survivor: bool = True) -> None:
    """Forget everything about a previous stream — and stop it if it somehow still runs.

    Called on BOTH ends of the cycle: before a new worker is spawned (so a crashed cycle's leftover
    transcript can never be pasted into a later recording) and after the stop toggle has taken the
    answer (so the next start begins from nothing). This is the property #50 is about, in the one
    place the streaming path could have broken it.

    ``stop_survivor`` is False for the one caller that has JUST killed the worker itself: signalling
    a pid twice is harmless but says something untrue about what this call is doing.

    The result files go by PATTERN, not by the pid in the pidfile, and that is the whole point: a
    worker that wrote its document late left it under a pid this clear no longer holds (the pidfile
    is already the next worker's, or gone). Removing only the pid we hold would leave that file —
    dictated speech — on disk for good, which is the leak windowsill#112's rename would otherwise
    have introduced. Every one of these documents belongs to a stream this call is forgetting."""
    pid = _read_stream_pid()
    if stop_survivor and pid is not None and pid_looks_like_stream_worker(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for path in (_STREAM_PID_PATH, *_stream_result_paths()):
        try:
            os.unlink(path)
        except OSError:
            pass


# --- live preview surface (windowsill#115) --------------------------------------------------------


def _write_preview(data: dict, path: str) -> None:
    """Write the current preview state atomically — temp-then-replace so the reader (preview.py)
    sees a complete JSON document or no document at all. Best-effort: an unwritable state dir
    means the preview surface is silently off, which is the documented degrade."""
    try:
        fd, tmp = tempfile.mkstemp(prefix="voice-loop-preview-", dir=os.path.dirname(path))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _clear_preview(path: str = _PREVIEW_PATH) -> None:
    """Remove the preview state file so the overlay knows to clear itself. Best-effort."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _start_preview() -> None:
    """Launch the preview overlay process. Best-effort: no display surface → silently off."""
    try:
        subprocess.Popen(
            [sys.executable, _PREVIEW_SCRIPT, _PREVIEW_PATH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def start_stream_worker(recorder_pid: int) -> None:
    """Spawn the child that holds the socket open. Never fatal: a worker that will not start is a
    recording that transcribes the old way, which is exactly the fallback."""
    clear_stream_state()
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
            proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), STREAM_WORKER_ARG, str(recorder_pid)],
                stdout=subprocess.DEVNULL,
                stderr=errlog,
                start_new_session=True,  # its own session: a hotkey launcher exiting must not end it
            )
    except OSError as err:
        log(f"stream worker did not start: {err} — this recording uses the batch path")
        return
    try:
        with open(_STREAM_PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
    except OSError as err:
        # We cannot find it again to stop it, so stop it now rather than leave a metered socket
        # open: without the pidfile the stop toggle has no handle on this child at all.
        log(f"stream worker pid not recorded: {err} — stopping it and using the batch path")
        try:
            proc.terminate()
        except OSError:
            pass
        return
    log(f"stream worker started pid={proc.pid}")


def finish_stream_worker() -> str | None:
    """Stop the worker and take its transcript. None means "use the recorded clip".

    Every None is logged with its reason, and every one of them is a degrade to the batch path
    rather than a lost recording: no worker at all, a worker that missed its bound, a failed
    session, or a session that connected and never heard one message. The one empty string this
    CAN return is a stream that heard the server fine and found no speech — a silent clip, which
    is a success (windowsill#93) and must not cost a second transcription of the same silence."""
    pid = _read_stream_pid()
    if pid is None:
        return None  # streaming was off for this recording: nothing to stop, nothing to say
    # The same PID-reuse guard the echo guard applies: a pidfile outlives its process and the
    # kernel recycles PIDs, so a stale number here would be a same-user SIGTERM to a stranger. A
    # pid that is not ours is simply not signalled — and the missing result then degrades below.
    if pid_looks_like_stream_worker(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # already gone: its result file (if any) is still the answer
    deadline = time.monotonic() + STREAM_FINISH_TIMEOUT
    result = _read_stream_result(pid)
    while result is None and time.monotonic() < deadline:
        time.sleep(STREAM_IDLE_POLL)
        result = _read_stream_result(pid)
    if result is None:
        log(f"stream worker did not finish within {STREAM_FINISH_TIMEOUT:.0f}s — using the recorded clip")
        if pid_looks_like_stream_worker(pid):
            try:
                os.kill(pid, signal.SIGKILL)  # not merely abandoned: it holds a metered socket
            except OSError:
                pass
        clear_stream_state(stop_survivor=False)
        return None
    clear_stream_state()
    if result.get("status") != "ok":
        log(f"streaming stt failed ({str(result.get('reason', ''))[:120]}) — using the recorded clip")
        return None
    text = str(result.get("text", ""))
    if not text and not result.get("messages"):
        # Connected, closed cleanly, and the server never said a word: that is not a silent clip,
        # it is a stream that carried nothing. The clip is still on disk — transcribe it.
        log("streaming stt heard nothing back from the server — using the recorded clip")
        return None
    log(f"streaming stt done: finals={result.get('finals', 0)} audio_bytes={result.get('audio_bytes', 0)}")
    return text


# --- streaming dictation: the session itself ------------------------------------------------------


def _open_pcm(wav_path: str, sleep, clock):
    """The recording, opened and positioned at its first PCM sample — None if it never became one.

    The recorder has been running for milliseconds by the time the worker gets here, so the header
    may not exist yet; that is a wait of milliseconds, bounded by STREAM_HEADER_TIMEOUT, not a
    failure. Once positioned, plain repeated ``read()`` calls return whatever the recorder has
    appended since — which is the whole trick that lets the file be tailed without standing between
    the recorder and the disk."""
    deadline = clock() + STREAM_HEADER_TIMEOUT
    while clock() < deadline:
        try:
            handle = open(wav_path, "rb")
        except OSError:
            sleep(STREAM_IDLE_POLL)
            continue
        offset = wav_data_offset(handle.read(STREAM_HEADER_PROBE_BYTES))
        if offset >= 0:
            handle.seek(offset)
            return handle
        handle.close()
        sleep(STREAM_IDLE_POLL)
    return None


def run_stream_session(
    s: dict,
    entry,
    key: str,
    *,
    stopping,
    recorder_alive,
    wav_path: str = "",
    connect=wsclient.connect,
    sleep=time.sleep,
    clock=time.monotonic,
    on_interim=None,
) -> dict:
    """Forward the growing recording to the provider's socket and collect what comes back.

    The whole streaming path in one function, and every seam a test needs is an argument: when to
    stop, whether the recorder still lives, where the audio is, how to open a socket, and the two
    clocks. Returns the document the worker writes — ``status`` ok/failed, the assembled ``text``,
    and the counts that let the stop toggle tell "a silent clip" from "a stream that carried
    nothing".

    ``on_interim(interim: str, assembled: str)`` is called after every transcript update when the
    caller wants a live signal — the live preview surface (windowsill#115). None means no one is
    listening.

    It never raises: every failure of the socket is caught here and becomes ``status: failed``,
    because the caller's only response to any of them is the same batch fallback.
    """
    streaming = entry.streaming
    finals: list[str] = []
    current_interim = ""
    messages = 0
    sent = 0

    def snapshot(status: str, reason: str = "") -> dict:
        """The answer, whenever it is reached — every exit below returns one of these, so a
        failure that happened halfway still reports the audio it sent and the finals it heard."""
        return {
            "status": status,
            "reason": reason,
            "text": assemble_stream_text(finals),
            "finals": len(finals),
            "messages": messages,
            "audio_bytes": sent,
        }

    url = streaming.url(streaming, entry, s)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "ws" and parsed.hostname and not _is_loopback_host(parsed.hostname):
        log(
            f"streaming stt endpoint is ws:// to {parsed.hostname} — "
            "the API key, audio and transcript travel in the clear"
        )
    try:
        socket_ = connect(url, streaming.headers(key), timeout=STREAM_CONNECT_TIMEOUT)
    except wsclient.WebSocketError as err:
        return snapshot("failed", str(err))

    def collect(frames) -> bool:
        """Fold every frame into the finals; True when the peer said goodbye."""
        nonlocal messages, current_interim
        closed = False
        for opcode, payload in frames:
            if opcode == wsclient.OP_CLOSE:
                closed = True
                continue
            if opcode != wsclient.OP_TEXT:
                continue
            update = streaming.result(providers.decode(payload))
            if update is None:
                continue
            messages += 1
            if update.is_final:
                if update.text:
                    finals.append(update.text)
                current_interim = ""  # a final settles the span — the interim guess is cleared
            else:
                current_interim = update.text
            if on_interim:
                on_interim(current_interim, assemble_stream_text(finals))
        return closed

    try:
        audio = _open_pcm(wav_path or _WAV_PATH, sleep, clock)
        if audio is None:
            socket_.close()
            return snapshot("failed", "the recorder produced no readable WAV header")
        started = clock()
        idle_since = None
        last_frame_at = started
        with audio:
            while True:
                chunk = audio.read(STREAM_CHUNK_BYTES)
                if chunk:
                    socket_.send_binary(chunk)
                    sent += len(chunk)
                    idle_since = None
                    last_frame_at = clock()
                else:
                    now = clock()
                    idle_since = now if idle_since is None else idle_since
                    if now - last_frame_at >= STREAM_KEEPALIVE_SECONDS:
                        socket_.send_text(streaming.keepalive_message)
                        last_frame_at = now
                if collect(socket_.poll(0.0 if chunk else STREAM_IDLE_POLL)):
                    socket_.close()
                    return snapshot("failed", "the server closed the stream while the microphone was open")
                if stopping():
                    break
                if idle_since is not None and not recorder_alive() and clock() - idle_since >= STREAM_TAIL_SECONDS:
                    break  # the recorder is gone and the file stopped growing: the clip is complete
                if clock() - started >= STREAM_MAX_SECONDS:
                    break
            # The stop toggle beat us to the recorder's last flush: send what is left before
            # asking for the finals, or the tail of the dictation is transcribed by nobody.
            while True:
                chunk = audio.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                socket_.send_binary(chunk)
                sent += len(chunk)
        socket_.send_text(streaming.close_message)
        # THE end-of-speech wait, and the only one this feature adds: the server owes finals for
        # the audio it has not answered yet. Bounded, and it ends the moment the server hangs up.
        deadline = clock() + STREAM_DRAIN_SECONDS
        while clock() < deadline and not socket_.closed:
            if collect(socket_.poll(STREAM_IDLE_POLL)):
                break
        socket_.close()
        result = snapshot("ok")
    except wsclient.WebSocketError as err:
        socket_.close()
        result = snapshot("failed", str(err))
    except OSError as err:
        socket_.close()
        result = snapshot("failed", f"the recording could not be read: {err}")
    return result


def stream_worker(s: dict, args: list[str]) -> int:
    """The child process: resolve the key, run the session, write the one answer, exit.

    Spawned by this same script's START toggle and by nothing else. It never touches the recorder,
    the pidfile mutex, the clipboard or the paste path — its whole output is one JSON document the
    stop toggle reads, and its whole input is a file somebody else is writing."""
    stopping = {"now": False}

    def _stop(signum, frame):  # noqa: ARG001 — signal-handler signature
        stopping["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        recorder_pid = int(args[0])
    except (IndexError, ValueError):
        recorder_pid = -1

    entry = resolve_stt_provider(s["stt_provider"])
    if entry.streaming is None:
        _write_stream_result({"status": "failed", "reason": f"{entry.name} has no streaming variant"})
        return 1
    key_envs = entry.key_envs(s["key_env"])
    key = ""
    for env in key_envs:
        key = read_key(s["key_file"], env, os.environ)
        if key:
            break
    if not key:
        tried = ", ".join(f"${env}" for env in key_envs)
        _write_stream_result({"status": "failed", "reason": f"no key (key_file unset/unreadable, and {tried} empty)"})
        return 1

    preview_path = _PREVIEW_PATH if s.get("preview") else ""

    def on_interim(interim: str, assembled: str) -> None:
        _write_preview({"interim": interim, "assembled": assembled}, preview_path)

    result = run_stream_session(
        s,
        entry,
        key,
        stopping=lambda: stopping["now"],
        recorder_alive=lambda: _pid_alive(recorder_pid),
        on_interim=on_interim if preview_path else None,
    )
    if preview_path:
        _clear_preview(preview_path)
    _write_stream_result(result)
    return 0 if result["status"] == "ok" else 1


def debounce_toggle(window: float, now: float | None = None) -> float | None:
    """Stamp this toggle, or refuse it as a key-repeat re-fire.

    Returns None when the toggle may proceed, or the age in seconds of the previous fire when this
    invocation lands inside ``window`` and must be dropped. ``window <= 0`` disables the guard.

    The stamp is refreshed on EVERY fire, admitted or dropped — that is the whole difference
    between a debounce and a rate limiter. Measured from the last ADMITTED toggle instead, a held
    key would re-admit every ``window`` for as long as it is held (a three-second hold becoming six
    record→STT→paste cycles at the default), which is windowsill#49 re-timed rather than fixed, and
    worse than the original: the clips are then window-sized, so they clear MIN_CLIP_SECONDS and
    reach the focused window as pasted garbage instead of dying in the min-clip guard. Refreshing
    makes the window a QUIET PERIOD: a hold is one toggle whatever its length, and the guard clears
    one window after the key is released. A key genuinely stuck down therefore holds dictation
    still, which is the strictly better failure — a stuck key that sprays transcripts is not.

    Why this is not the pidfile claim: the claim arbitrates two invocations that both want to
    START. Autorepeat is the opposite shape — fires that alternate start/stop/start/stop, each one
    legitimately winning its own claim, and together producing nothing but empty clips. The guard
    therefore runs BEFORE the direction is chosen and drops the fire whichever branch it would have
    taken.

    Read-compare-write happens under a non-blocking exclusive flock on the stamp file itself, so
    two invocations cannot both read the old timestamp and both proceed. Failing to TAKE the lock
    is itself the answer: another toggle is inside that critical section right now, which is a
    re-fire by definition — no human taps twice in the microseconds it is held. Where a lock is not
    available at all (no fcntl) the plain read-compare-write still catches autorepeat, which is
    sequential; and where the stamp cannot be written at all (an unwritable state dir) the guard
    fails OPEN — a debounce that cannot record time must not be a hotkey that never records audio.

    The window is measured on the ABSOLUTE age, so a stamp that reads slightly in the future still
    debounces: rounding when the stamp is written can put it a fraction of a millisecond ahead of
    the next reader's clock, and a plain ``age >= 0`` test would let exactly the autorepeat fire it
    exists to catch straight through. A stamp further ahead than the window is a clock stepped
    backwards (ntp, a resume, a hand-edited file) and is admitted rather than wedging the toggle
    until wall-clock catches up — and the refresh below then puts the stamp back on this clock."""
    if window <= 0:
        return None
    now = time.time() if now is None else now
    try:
        fd = os.open(_TOGGLE_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as err:
        log(f"debounce stamp unavailable ({_TOGGLE_PATH}): {err} — toggling anyway")
        return None
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # a named reason of its own: "0 ms" below is a verdict, not a measured age
                log("debounce stamp locked by another toggle — same-instant re-fire")
                return 0.0
        try:
            previous = float(os.read(fd, 64).decode("ascii", "replace").strip() or "0")
        except (OSError, ValueError):
            previous = 0.0  # empty or garbage stamp: no previous toggle we can trust
        age = now - previous
        try:
            # unconditional, and before the verdict is returned: the window restarts on a DROPPED
            # fire too, which is what keeps a held key at one toggle instead of one per window
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{now:.6f}".encode("ascii"))
        except OSError as err:
            # the stamp did not move, so the guard degrades to measuring from the last fire it
            # COULD record — noisier under a hold, never wedged
            log(f"debounce stamp not written: {err}")
        return max(0.0, age) if abs(age) < window else None
    finally:
        try:
            os.close(fd)  # releases the flock with the file description
        except OSError:
            pass


def claim_pidfile() -> int | None:
    """Atomically claim the single recording slot.

    O_CREAT|O_EXCL is the whole mutex: of two near-simultaneous hotkey invocations exactly one
    creates the pidfile (and goes on to start a recorder); the loser gets FileExistsError and
    exits with a note instead of spawning a second recorder onto the same WAV — which used to
    leak one recorder forever. Returns the open fd (the winner writes the recorder pid into it)
    or None when the slot is held. Any other OSError also yields None: a recorder whose pid we
    could not record could never be stopped, which is exactly the leak this claim prevents."""
    try:
        return os.open(_PID_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    except OSError as err:
        log(f"pidfile claim failed: {err}")
        return None


def _release_claim(pidfile_fd: int) -> None:
    """Undo a successful claim on a failed start: close the fd, remove the pidfile."""
    try:
        os.close(pidfile_fd)
    except OSError:
        pass
    try:
        os.unlink(_PID_PATH)
    except OSError:
        pass


def start_recording(s: dict, system: str, pidfile_fd: int) -> int:
    """Start the recorder into the already-claimed pidfile (see claim_pidfile); every failure
    path releases the claim so the next toggle starts clean."""
    stop_speak_playback()

    recorder = resolve_recorder(s["recorder"], system, shutil.which)
    source = s.get("source", "")
    # DirectShow on Windows demands an explicit device name; alsa / avfoundation default to one
    # themselves, so the discovery is a Windows-only pre-step. Discovery races with hardware
    # changes (a USB headset swapped mid-day) — the operator override is dictate.source.
    if system == "Windows" and recorder == "ffmpeg" and not source:
        source = _discover_dshow_mic()
    argv = recorder_argv(recorder, system, _WAV_PATH, source=source)
    if not argv:
        if system == "Windows" and recorder == "ffmpeg":
            note(
                "no dshow device found — run `ffmpeg -list_devices true -f dshow -i dummy` "
                "and set dictate.source to the device name",
                system,
            )
            log("dshow discovery returned no device — recorder not started")
        else:
            note("no recorder found — install pw-record/arecord (Linux) or sox/ffmpeg (macOS)", system)
            log("no recorder available")
        _release_claim(pidfile_fd)
        return 1
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
            popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": errlog}
            if system == "Windows":
                # Give CTRL_C_EVENT its own console: otherwise GenerateConsoleCtrlEvent(0, 0)
                # reaches the user's shell and every other process sharing this terminal.
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as err:
        note("recorder failed to start", system)
        log(f"recorder failed: {err}")
        _release_claim(pidfile_fd)
        return 1
    try:
        os.write(pidfile_fd, str(proc.pid).encode("ascii"))
    except OSError:
        pass
    finally:
        try:
            os.close(pidfile_fd)
        except OSError:
            pass
    if system == "Windows" and recorder == "ffmpeg":
        # The DirectShow device name is the operator's microphone label — often a person's name, a
        # phone model or a company name. It is logged in full only locally; the report-bug collector
        # cuts it at " device=" (see LOG_RULES). The pid sits BEFORE the device so that cut drops
        # the name and nothing else.
        log(f"ffmpeg -f dshow recording pid={proc.pid} device={source!r}")
    else:
        log(f"recording via {recorder} pid={proc.pid}")
    # The socket goes up FIRST of the two things that follow the recorder: opening it costs a
    # round trip, and every millisecond of that is speech the server never hears. The focus probe
    # below can spend up to FOCUS_PROBE_TIMEOUT and is deliberately behind it.
    if streaming_wanted(s):
        start_stream_worker(proc.pid)
        # The live preview surface (windowsill#115) only exists when there is a live socket to
        # show interims from; in batch mode there is nothing to preview.
        if s.get("preview"):
            _start_preview()
    # After the recorder is live, never before it: the probe may spend up to FOCUS_PROBE_TIMEOUT,
    # and none of that may be time the microphone is not yet capturing.
    if same_window_guard_on(s):
        remember_focus(system)
    sound(s["start_sound"], s["player"])
    note("recording…", system)
    return 0


def _wait_gone(pid: int) -> bool:
    for _ in range(30):
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def stop_and_transcribe(s: dict, system: str, mode: str, recorder_pid: int) -> int:
    # END OF SPEECH. Every latency number this file reports is an offset from this instant — the
    # toggle that stopped the recording — because that is the wait the user actually feels, and the
    # one windowsill#99 exists to remove. It covers both paths, so the two are comparable.
    #
    # The paste-lock guard (windowsill#50): from this instant until this process exits (normal or
    # otherwise), no new recording may start — the lock keeps the "lags one recording behind"
    # symptom from surfacing.  Released by an atexit handler; a crash leaves a stale pid that the
    # next toggle's staleness check clears.
    _claim_paste_lock()
    atexit.register(_release_paste_lock)
    stopped_at = time.monotonic()
    # Consumed unconditionally, whatever this stop goes on to do (see take_remembered_focus): an
    # identity must never outlive its own recording. Only the same-window guard below reads it.
    started_focus = take_remembered_focus()
    # Remove the pidfile BEFORE signalling: from this point the recording slot is free, so a
    # racing start claims a FRESH pidfile instead of adopting (and re-stopping) a dying recorder.
    try:
        os.unlink(_PID_PATH)
    except OSError:
        pass
    # INT-then-TERM on POSIX; CTRL_C_EVENT-then-TerminateProcess on Windows (see _stop_recorder —
    # the WAV header only gets finalized by a graceful stop, and os.kill on Windows is not one).
    # On Windows the recorder name is re-derived so _stop_recorder can identity-check the pid before
    # signalling (a recycled pid must not get a stranger's Ctrl+C — see _win_pid_is_recorder).
    recorder = resolve_recorder(s["recorder"], system, shutil.which) if system == "Windows" else ""
    _stop_recorder(recorder_pid, system, recorder)
    # the recorder flushes the tail of the file AFTER it stops accepting samples — this short
    # settle is the difference between a complete phrase and a truncated one
    time.sleep(0.2)
    sound(s["stop_sound"], s["player"])

    # min-clip guard: a bounced hotkey leaves a clip too short to hold speech — skip STT entirely
    # rather than hand the server pure silence to hallucinate on
    try:
        size = os.path.getsize(_WAV_PATH)
    except OSError:
        size = 0
    if clip_seconds(size) < MIN_CLIP_SECONDS:
        # The worker is stopped here too, but NOT waited for: a bounced hotkey must stay as cheap
        # as it has always been, and there is no transcript anybody wants. Its late document (if it
        # writes one at all) carries a pid this stop never held, so no later recording can adopt it.
        clear_stream_state()
        _clear_preview()
        note("clip too short — ignored", system)
        log(f"clip too short ({size} bytes ≈ {clip_seconds(size):.2f}s) — stt skipped")
        try:
            os.replace(_WAV_PATH, _LAST_WAV_PATH)
        except OSError:
            pass
        return 0

    # Past the guard, the worker's answer IS the transcript when there is one. A no-op when
    # streaming was never on for this recording; bounded, and a degrade on every failure.
    streamed = finish_stream_worker()
    if streamed is None:
        # The batch path, unchanged, and the destination of every streaming degrade.
        note("transcribing…", system)
        text = transcribe(s)
        via = "batch"
    else:
        # Already assembled while the user was still speaking — nothing to upload, nothing to wait
        # for beyond the finals the drain already collected.
        text = streamed
        via = "stream"

    # Belt-and-suspenders echo guard (windowsill#101): AEC alone can degrade — a misconfigured
    # module, a PipeWire restart that drops it, an amp saturated past the canceller's range.
    # The state-store comparison catches what the signal path misses: a transcript that matches
    # what the assistant just spoke is dropped before it reaches the prompt.
    #
    # Read AFTER transcription so the read cost is only paid for speech someone heard.
    if text:
        try:
            with open(_LAST_SPOKEN_PATH, encoding="utf-8") as fh:
                last_spoken = fh.read().strip()
        except OSError:
            last_spoken = ""
        if last_spoken and _is_echo_of_last_spoken(text, last_spoken):
            log(f"echo guard: transcript matches the last spoken line — dropped ({len(text)} chars)")
            try:
                os.replace(_WAV_PATH, _LAST_WAV_PATH)
            except OSError:
                pass
            return 0

    def latency(landed: str) -> None:
        """The number windowsill#99 is judged on: end of speech -> the text being where it goes."""
        elapsed_ms = int((time.monotonic() - stopped_at) * 1000)
        log(f"dictation latency stop_to_paste_ms={elapsed_ms} via={via} to={landed}")

    log(f"transcript: {text[:120]}")
    try:
        os.replace(_WAV_PATH, _LAST_WAV_PATH)
    except OSError:
        pass

    if not text:
        _clear_preview()
        note("nothing recognized", system)
        log("empty transcription")
        # A silent clip is a RESULT, and a streaming one is the fastest result this file produces —
        # leaving it out of the latency ledger is how a path gets a reputation it never earned.
        latency("nothing")
        return 0

    tool = resolve_clipboard(
        s["clipboard"], system, shutil.which, bool(os.environ.get("WAYLAND_DISPLAY"))
    )
    log(f"clipboard resolved: {tool or '<none>'}")
    commands = clipboard_commands(tool)
    if not commands:
        _clear_preview()
        log("no clipboard tool (install wl-clipboard or xclip)")
        note("no clipboard tool available", system)
        return 1
    for argv in commands:
        try:
            # clip.exe auto-detects Unicode only as UTF-16LE with a BOM; POSIX clipboard
            # tools continue to receive UTF-8, including when running under WSL.
            clipboard_input = text.encode("utf-16le")
            if system == "Windows" and tool == "clip":
                clipboard_input = b"\xff\xfe" + clipboard_input
            else:
                clipboard_input = text.encode("utf-8")
            with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
                subprocess.run(argv, input=clipboard_input, stdout=subprocess.DEVNULL, stderr=errlog, check=False)
        except OSError as err:
            _clear_preview()
            log(f"clipboard failed: {err}")
            note("no clipboard tool available", system)
            return 1

    if s["auto_paste"]:
        # The same-window guard (dictate.paste_target): auto-paste presses the paste key into
        # whatever is focused NOW, so a window switch during the sentence sends it somewhere it was
        # never meant to go. Probing again only when start left an identity to compare against —
        # an unknown at either end degrades to "any", which is the documented default.
        if same_window_guard_on(s) and started_focus and focus_changed(started_focus, current_focus(system)):
            _clear_preview()
            note("focus moved — text is in the clipboard", system)
            log("focus moved since this recording started — paste suppressed (dictate.paste_target=same-window)")
            latency("clipboard")
            return 0
        sock = os.environ.get("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")
        try:
            sock_ok = stat.S_ISSOCK(os.stat(sock).st_mode)
        except OSError:
            sock_ok = False
        paste_tool = pick_paste_tool(system, shutil.which, sock_ok, os.environ.get("DISPLAY", ""))
        paste_was_denied = paste_tool == "osascript" and _paste_denied()
        if _run_paste(paste_tool, s["paste_key"], mode == "send", sock):
            _clear_preview()
            log(f"auto-pasted (mode={mode} key={s['paste_key']})")
            latency("paste")
            return 0
        if paste_tool == "osascript" and _paste_denied() and not paste_was_denied:
            # The user just declined the macOS Accessibility permission — tell them once that
            # the text is on the clipboard and the keystroke path won't be retried.
            _clear_preview()
            note("accessibility permission denied — text is on the clipboard", system)
            log("auto-paste denied — Accessibility permission not granted")
            latency("clipboard")
            return 0
        log("auto-paste unavailable — clipboard fallback")
    _clear_preview()
    note(f"copied — press {s['paste_key']} to paste", system)
    latency("clipboard")
    return 0


def _run_paste(tool: str, paste_key: str, enter: bool, ydotool_socket: str) -> bool:
    """Execute a paste plan. True only when the paste keystroke itself was sent.

    On macOS, the first osascript keystroke triggers the Accessibility permission prompt; if the
    user declines, the denial is detected from stderr, recorded in the state dir, and the keystroke
    path is skipped on every subsequent toggle — the text stays on the clipboard without another
    prompt. Other paste failures (tool not installed, a transient error) fall back to clipboard
    without recording a denial, so the toggle retries on the next invocation."""
    # A denial marker persists across invocations, but it must not make recovery depend on
    # manually deleting a file: probe the permission and clear a stale marker after a later grant.
    if tool == "osascript" and _paste_denied():
        if _accessibility_permission_regranted():
            _clear_paste_denied()
        else:
            return False

    steps = paste_plan(tool, paste_key, enter)
    if not steps:
        return False
    env = dict(os.environ)
    if tool == "ydotool":
        env["YDOTOOL_SOCKET"] = ydotool_socket
    for delay, argv, required in steps:
        if delay:
            time.sleep(delay)
        try:
            result = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, check=False)
        except OSError as err:
            log(f"paste spawn failed ({argv[0]}): {err}")
            if required:
                return False
            continue
        if result.stderr:
            _log_stderr(result.stderr)
        if result.returncode != 0:
            # macOS Accessibility denial: the osascript keystroke fails because the user declined
            # the permission. Detect the error once, mark it, and stop retrying.
            if tool == "osascript" and required and _is_accessibility_denial(result.stderr):
                _mark_paste_denied()
                log("accessibility permission denied — falling back to clipboard")
            if required:
                return False
    return True


def main(argv: list[str]) -> int:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
    except OSError:
        pass
    system = platform.system()
    cfg_path = os.environ.get(
        "VOICE_LOOP_CONFIG",
        os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "voice-loop/config.json"),
    )
    s = resolve_settings(load_config(cfg_path), system)

    # The streaming worker (windowsill#99) is this same script, spawned by its own START toggle —
    # never a hotkey invocation. It is dispatched here, ABOVE the debounce and the pidfile mutex,
    # because it is neither a toggle nor a competitor for the recording slot: it only reads the
    # file the recorder is writing. Nothing else may reach this argv, which is why it is checked
    # before `mode` is derived from the same position.
    if len(argv) > 1 and argv[1] == STREAM_WORKER_ARG:
        return stream_worker(s, argv[2:])

    mode = argv[1] if len(argv) > 1 and argv[1] else s["mode"]

    # Key-repeat guard, before the pidfile is even read (see debounce_toggle): a held hotkey
    # autorepeats, and the fire that follows would otherwise stop a recording milliseconds old.
    # The window restarts on every fire, so the whole hold is one toggle however long it lasts.
    # Log only — a notification per repeat would be the same spam in another window.
    age = debounce_toggle(s["debounce_ms"] / 1000.0)
    if age is not None:
        log(f"toggle ignored — key repeat ({age * 1000:.0f} ms after the previous one)")
        return 0

    # Paste-lock guard (windowsill#50): if a previous stop is still transcribing or pasting,
    # ignore this toggle — starting a new recording now would interleave pastes, producing the
    # "lags one recording behind" symptom.  The stop_sound is the audible cue that the previous
    # cycle is not done yet (on macOS it defaults on — see resolve_settings).
    if _paste_locked():
        log("toggle ignored — previous dictation still finishing (transcription/paste in progress)")
        sound(s["stop_sound"], s["player"])
        return 0

    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            recorder_pid = int(fh.read().strip() or "-1")
    except (OSError, ValueError):
        recorder_pid = -1

    if _pid_alive(recorder_pid):
        return stop_and_transcribe(s, system, mode, recorder_pid)
    if recorder_pid > 0:
        # a stale pidfile (parsed pid, dead process) is removed, never obeyed — but only after
        # re-checking it still holds the SAME stale pid, so a racing invocation's fresh claim
        # (empty until its recorder spawns) is never swept away by this unlink
        try:
            with open(_PID_PATH, encoding="utf-8") as fh:
                still_stale = fh.read().strip() == str(recorder_pid)
            if still_stale:
                os.unlink(_PID_PATH)
        except OSError:
            pass
    else:
        # A pidfile that exists but holds no parseable pid is either a claim being written RIGHT
        # NOW by a racing invocation (leave it — the claim below loses politely) or dead garbage
        # from a crashed one (older than the grace window — remove it, or the toggle wedges).
        try:
            if time.time() - os.path.getmtime(_PID_PATH) > _CLAIM_GRACE_SECONDS:
                os.unlink(_PID_PATH)
        except OSError:
            pass
    pidfile_fd = claim_pidfile()
    if pidfile_fd is None:
        note("another dictation toggle is already starting", system)
        log("pidfile already claimed — a concurrent invocation won the race; exiting")
        return 0
    return start_recording(s, system, pidfile_fd)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # a hotkey script: surface via the log, never a traceback into nowhere
        try:
            log(f"unexpected error: {sys.exc_info()[1]!r:.200}")
        except Exception:
            pass
        sys.exit(1)
