#!/usr/bin/env python3
"""voice-loop — the speaking hook: speak the assistant's marker-tagged lines.

This is the hook's whole logic; ``scripts/speak.sh`` is only a thin launcher (hooks.json keeps
invoking the .sh so the registration surface never changes). Stdlib only, Python 3.10+.

Two events reach it, and ``hook_event_name`` on stdin is what tells them apart: **Stop** (always
registered) at the end of a turn, and **PostToolUse** (opt-in, ``speak.eager``) after every tool
call — see "eager speaking" below.

Convention: only lines whose first non-space character is the marker (default 🔊) are voiced;
everything else stays text. The model decides what is worth hearing.

Reads ~/.config/voice-loop/config.json. Never fails a turn: every path exits 0.

Deliberate behaviours, found by live debugging — do not "simplify" them away:

* flush race — Stop can fire BEFORE the final assistant message is written to the transcript.
  The transcript is read IMMEDIATELY; a retry happens only on the two real race signatures — an
  EMPTY extract, or an extract IDENTICAL to the previously spoken line — with adaptive backoff
  (0.15 → 1.0 s), so an already-flushed transcript costs zero sleep.
* queued, not dropped — that backoff is 2.65 s all told, and one cloud clip runs ~10 s. A Stop that
  ran out of ladder while the PREVIOUS line was still playing used to give up, and the line was
  never spoken at all: the voice appeared to lag turns behind. So when the ladder runs out with
  nothing new, the Stop path keeps re-reading FOR AS LONG AS an older speaking chain is audibly
  alive (playing.pid, same identity check the takeover uses), bounded by PLAYBACK_POLLS. The retry
  CONDITION is unchanged — it waits only while there is nothing new — so a line that was already
  there is never delayed by a millisecond; only a line that would have been dropped is now late.
  Only Stop waits: an eager firing's next chance is one tool call away (see the lock note below).
* never a silent drop — a hook that stops without speaking says why in the log. This one cost a
  session of log archaeology: speak.log held NO entry at all for lines the user never heard.
  Every path that abandons a line now logs its reason. Three stay quiet on purpose, because a log
  nobody can read is as good as no log: an eager firing with nothing new (it claimed nothing, and
  its line waits for the next tool call), a message with no marked line in it (there was nothing to
  lose), and a hook the user has switched off (``speak.enabled``, once per turn, forever).
* dedup — a same-as-last read IS the stale previous turn, so it is dropped, not spoken twice.
* eager speaking (opt-in, ``speak.eager``) — Stop fires only at the END of a turn, so a 🔊 line
  printed early in a long tool-heavy turn is heard minutes late. With eager on, hooks.json also
  registers PostToolUse, and every tool call becomes a chance to narrate what has appeared so far.
  The two paths differ in scope and in retries, in nothing else: PostToolUse reads the marked lines
  of ALL assistant messages (by the time a tool returns, the line can be several messages old) and
  never retries the flush race — a half-written line is caught by the NEXT firing, which is free;
  Stop reads the last message and keeps the retry, because for it there is no next firing.
* spoken-ledger, AND IT EXISTS ONLY FOR EAGER — with two event paths racing to say the same line,
  "have I said this already?" can no longer be one last-spoken string.
  ``sha1(transcript_path + message_index + line)[:16]`` of every line either path speaks is appended
  to ``spoken.ledger`` (trimmed to the last LEDGER_LINES on entry), and both paths consult it before
  speaking, so a line is voiced exactly once whichever hook saw it first. The MESSAGE INDEX is part
  of that identity on purpose: a session says «Done.» many times, and the ninth one is a different
  line in a different message — not the first one echoing. A ledger keyed by text alone would mute
  it forever.
  The whole mechanism is gated on ``speak.eager``. With eager OFF only ONE event path speaks —
  Stop, for the turn's line AND for the contour page (#40), which is why the check is gated on the
  event too — so there is nothing to be idempotent WITH: the Stop hook runs EXACTLY its pre-0.3.2
  logic for the turn's line — dedup against the immediately previous utterance and nothing else,
  no ledger read, no ledger write, no seeding. A → B → A speaks three times, as it always did.
  (The contour check takes the speaking lock on BOTH paths regardless: its announced-ledger is a
  read-modify-write, and two Stop firings racing it would page twice.)
  With eager on, a line is claimed BEFORE playback: a line whose synthesis then fails is lost rather
  than repeated — for something that talks out loud, idempotence beats completeness.
* first-run seeding (eager only, with the ledger) — a transcript nobody has spoken for yet (no
  ``seeded:<sha1(path)[:16]>`` marker in the ledger) is HISTORY, not news: every marked line already
  in it is written to the ledger WITHOUT being spoken. Turning eager on mid-session otherwise
  recites the entire session back at you, which is exactly what the live deployment did. The Stop
  path seeds everything EXCEPT the last message's lines — those are the turn it was called to speak.
* one speaker at a time, and NOBODY WAITS — the whole read-claim-speak sequence runs under an
  exclusive flock on ``speaking.lock``, so the ledger cannot be read by one firing while another is
  half-way through claiming it, and two firings cannot talk over each other. The acquire is
  NON-BLOCKING: an eager firing that loses the race exits at once. It never claimed anything, so its
  line stays available to the next firing — and the next firing is free, one tool call away. Waiting
  would cost far more than it buys: one blocked python per tool call piles up through a heavy turn,
  and a waiter is invisible to everything that looks at ``playing.pid`` — the takeover cannot
  supersede it and dictate.py's echo guard cannot stop it, so it wakes up and speaks into an open
  microphone. Only Stop waits, and only briefly (LOCK_GRACE): it is the turn's last chance, so it
  lets the holder finish, then supersedes it (SIGTERM releases that chain's lock with it) and takes
  one more shot. A Stop that still cannot get in leaves its lines UNCLAIMED rather than claiming
  outside the lock — the next turn's eager firing, which reads every message, still says them.
* takeover — a fresher hook invocation supersedes a still-playing older one. Scoped precisely:
  the speaking chain records its PIDs (this process + the current player/command child) in a
  pidfile, and a new invocation SIGTERMs exactly those — nothing pattern-matched, nothing else —
  after confirming via /proc/<pid>/cmdline that each pid still IS the voice-loop chain (a
  pidfile outlives its process, and the kernel recycles PIDs).
* streaming — the marked text is split into sentence chunks (tiny sentences merged so a chunk is
  at least ~MIN_CHUNK_CHARS chars); chunk 1 starts playing as soon as IT is synthesized, and the
  next chunk synthesizes while the previous one plays. Perceived latency is one small synthesis,
  not the whole message.
* server-side streaming — when GET /health says ``"streaming": true`` (checked once per
  invocation), the LAN path POSTs the WHOLE text to /tts/stream and plays SSE chunks as they
  arrive: the server does the sentence chunking, each ``chunk`` event is one complete standalone
  WAV, and every decoded chunk enters the SAME player queue a locally-synthesized sentence chunk
  would. A stream that fails BEFORE its first chunk falls back to the blob /tts path once; after
  the first chunk we play what arrived and stop on the terminal ``error`` event (logged). The
  client-side sentence splitter stays for the blob path and older servers.
* heartbeat — EVERY invocation, speaking or not, rewrites the ``hook-last-fired`` stamp in the
  state dir (temp-then-replace, so a reader never sees it torn). It exists because the harness
  itself once stopped calling the Stop hook mid-session while the whole plugin chain stayed
  healthy: the stamp's AGE — surfaced as ``hook_last_fired_age_s`` on GET /health — is what tells
  "the harness is no longer calling us" apart from "there was nothing to say". The remedy for the
  first is a Claude Code session restart (hooks re-initialize); see docs/troubleshooting.md.
* keys — the cloud API key comes from ``key_file`` (wins) or the named env var, is used only as an
  in-process HTTP header, and NEVER appears in argv, in the config, or in the log.
* timing — every spoken run logs ``timings extract_ms=… first_audio_ms=… total_ms=…``, and ALL
  THREE are measured from ONE monotonic t0 taken at hook start, so the three numbers compare.
  **first_audio_ms is hook start -> the spawn of the FIRST player process**: the moment sound can
  begin, counted from the moment the hook began. It therefore includes everything you wait through
  before hearing anything — the transcript read (incl. flush-race retries), the /health probe,
  opening the stream, and the first chunk's synthesis. It was previously measured from inside the
  player loop, which started its clock AFTER the streaming source had already eagerly pulled (and
  waited out) the first chunk; that under-reported a real 2308 ms wait as 3 ms.
* the contour check (#40) — after the turn's own speech, the hook reads ``contour.json`` (written
  by ``scripts/contour_poll.py``, the voice contour's poller) and voices every active alert it has
  not voiced before: a service that demoted itself off the GPU and kept serving breaks nothing
  loudly, so it must be HEARD, not left on a dashboard nobody opens. Dedup is a small announced
  file of alert keys, pruned to the alerts still active — a condition that clears and comes back
  pages again, one that persists is said once. The check travels the SAME playback path a marked
  line takes (extracted as ``play_text``), takes the speaking lock, respects ``speak.enabled``,
  and is opted out with ``contour.alerts: false``. No status file at all — an install that never
  set the poller up — costs one tolerant read and silence. Two properties it does NOT share with
  the marked-line path, both learned the hard way:
  - a key is announced only after ``play_text`` reports the audio REACHED A PLAYER. The alert's
    delivery path is the very service most alerts are about, so claiming first made "the speech
    server is down" the one condition the hook could never report — voiced never, repeated never.
    A failed delivery re-arms and the next firing tries again.
  - a status file older than its own ``max_age`` is not read as a green contour: past that bound
    the only thing it proves is that nobody is polling, and THAT is what gets voiced. #40 opens on
    "no way to tell 'the contour is fine' from 'nobody looked'", and a frozen file is that again.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only; Linux and macOS are the supported platforms
    fcntl = None  # type: ignore[assignment]

# Retry backoff for the flush race: adaptive, front-loaded — most races resolve within the first
# fraction of a second, so we probe early instead of sleeping a flat 5 x 0.7 s tail.
BACKOFF = (0.15, 0.3, 0.5, 0.7, 1.0)

# What happens where that ladder runs out, on the Stop path only: while an OLDER speaking chain is
# still playing, a line with nothing new behind it is not lost, it is standing in a queue — so keep
# looking. The bound is a COUNT of polls rather than a wall clock, because that is the same bound a
# test can drive with a fake sleep; PLAYBACK_POLLS * PLAYBACK_POLL is the ceiling in seconds (20 s
# — two cloud clips' worth), and it exists so a wedged player can never hold a turn open.
PLAYBACK_POLL = 0.25
PLAYBACK_POLLS = 80

# The spoken-ledger is a rolling window, not a journal: it only has to answer "did we already say
# this line?" for the transcript currently in front of us. LEDGER_LINES is roughly a long session's
# worth of marked lines; the per-transcript seed markers are kept separately (and far longer) —
# dropping a live session's marker would re-seed it and swallow its next line.
LEDGER_LINES = 400
LEDGER_SEEDS = 40
SEED_PREFIX = "seeded:"

# The ONLY wait left in the lock path, and it belongs to Stop alone: having let the current speaker
# have its moment, Stop SIGTERMs it and needs a beat for that chain to die and drop the flock.
# Bounded and tiny — an eager firing passes no grace at all and loses instantly.
LOCK_GRACE = (0.05, 0.1, 0.2)

# Streaming chunks below this length gain nothing (player spawn overhead dominates), so tiny
# sentences are merged up to at least this many characters.
MIN_CHUNK_CHARS = 40

# The /health probe is a tiny GET against a server we are about to POST to anyway — it must never
# stall a turn longer than this, whatever speak.timeout says about synthesis itself.
HEALTH_TIMEOUT = 5.0

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
_LOG_PATH = os.path.join(_STATE_DIR, "speak.log")
_LAST_PATH = os.path.join(_STATE_DIR, "last-spoken")

# One line per line ever spoken (plus one seed marker per transcript) — the cross-event memory that
# makes Stop and PostToolUse idempotent with respect to each other.
_LEDGER_PATH = os.path.join(_STATE_DIR, "spoken.ledger")

# The inter-firing lock: held for the whole read-claim-speak sequence, so two firings can neither
# claim the same line nor talk over each other. Taken non-blocking — see acquire_lock.
_LOCK_PATH = os.path.join(_STATE_DIR, "speaking.lock")

# CROSS-SCRIPT CONTRACT (keep in sync with dictate.py): playing.pid holds the space-separated
# PIDs of the live speaking chain — this python process first, then the current player/command
# child. dictate.py's echo guard reads THIS file to stop in-flight playback before recording
# (its pkill fallback only fires when the file is absent), and both takeover paths verify each
# pid via /proc/<pid>/cmdline before signalling (PID-reuse guard).
_PID_PATH = os.path.join(_STATE_DIR, "playing.pid")

# The heartbeat: epoch seconds of the last hook INVOCATION, rewritten on every one — even a firing
# that speaks nothing proves the harness still calls the hook, which is the fact a silent session
# needs checked first. GET /health reports its age as hook_last_fired_age_s (cross-process
# contract with server/voice_server.py: one bare float, nothing else).
_STAMP_PATH = os.path.join(_STATE_DIR, "hook-last-fired")

# The contour check (#40): the poller's status file, and the announced-ledger of alert keys
# already voiced (pruned to the alerts still active, so a cleared-and-returned condition pages
# again). contour.json is written by scripts/contour_poll.py — this hook only ever READS it.
_CONTOUR_PATH = os.path.join(_STATE_DIR, "contour.json")
_CONTOUR_ANNOUNCED_PATH = os.path.join(_STATE_DIR, "contour-announced")

# How old a status file may be before it stops being evidence about the contour and becomes
# evidence that nobody is polling. The poller writes its own bound into the file (contour.max_age)
# and that wins; this is the fallback for a file written before the bound existed.
CONTOUR_MAX_AGE = 900

# The event this invocation was fired for, recorded by main() the moment stdin is read so the
# contour check can tell which path it is on. A module cell rather than a return value: main()'s
# return IS the process exit code, and hooks.json reads that.
_fired: dict = {"event": "Stop"}

# state the SIGTERM handler (takeover by a fresher invocation) must be able to reach:
# the current player child, the temp WAVs on disk, and the open SSE response (its socket
# must close mid-stream on takeover, not linger until the server finishes synthesizing)
_live: dict = {"proc": None, "files": set(), "stream": None}


def log(message: str) -> None:
    try:
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > 1_000_000:
            open(_LOG_PATH, "w").close()
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _default_wall_clock() -> float:
    """Epoch seconds. Wall time is an INPUT here, injected so a test needs no real clock."""
    return time.time()


def stamp_hook_fired(clock: Callable[[], float] = _default_wall_clock) -> None:
    """Record that the harness invoked the hook — the heartbeat whose age /health reports.

    Stamped on EVERY invocation, speaking or not: what it proves is that the harness still calls
    the hook, which is exactly the fact a silent session needs checked first. Temp-then-replace,
    so a /health read mid-write sees the old stamp or the new one, never a torn one. Never raises
    — the stamp is diagnostics, and a hook error must not surface into the session."""
    try:
        fd, tmp = tempfile.mkstemp(prefix="voice-loop-stamp-", dir=os.path.dirname(_STAMP_PATH))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{clock():.3f}\n")
        os.replace(tmp, _STAMP_PATH)
    except OSError:
        pass


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


def resolve_settings(config: dict, system: str) -> dict:
    """Every knob speak.sh honoured, same names, same defaults, same precedence."""
    speaker = str(cfg(config, "tts.speaker", ""))
    provider = str(cfg(config, "tts.cloud.provider", "openai"))
    default_model = "eleven_multilingual_v2" if provider == "elevenlabs" else "tts-1"
    voice_settings = cfg(config, "tts.cloud.voice_settings", None)
    return {
        "enabled": cfg(config, "speak.enabled", True) not in (False, "false"),
        # eager is opt-in and OFF by default: with it off the PostToolUse registration is a no-op
        # that costs one stdin read per tool call and never touches the transcript.
        "eager": cfg(config, "speak.eager", False) not in (False, "false"),
        "marker": str(cfg(config, "speak.marker", "🔊")),
        "player": str(cfg(config, "speak.player", "afplay" if system == "Darwin" else "aplay -q")),
        "max_chars": int(cfg(config, "speak.max_chars", 600)),
        "timeout": float(cfg(config, "speak.timeout", 60)),
        "backend": str(cfg(config, "tts.backend", "lan")),
        # left empty here: the per-backend default differs (see synthesize) — the LAN server and the
        # OpenAI-compatible path default to the local speech server, ElevenLabs to its own API host.
        "endpoint": str(cfg(config, "tts.endpoint", "")),
        "speaker": speaker,
        # top-level "language" is the one the user sets; ".tts.language" is the advanced escape for
        # people who dictate in one language and listen in another.
        "language": str(cfg(config, "tts.language", cfg(config, "language", "en"))),
        "command": str(cfg(config, "tts.command", "")),
        "provider": provider,
        "voice_id": str(cfg(config, "tts.cloud.voice_id", speaker)),
        "cloud_model": str(cfg(config, "tts.cloud.model", default_model)),
        "output_format": str(cfg(config, "tts.cloud.output_format", "mp3_44100_128")),
        "key_env": str(cfg(config, "tts.cloud.api_key_env", cfg(config, "tts.api_key_env", "VOICE_LOOP_TTS_API_KEY"))),
        "key_file": str(cfg(config, "tts.cloud.key_file", "")),
        # provider-specific synthesis knobs, passed through verbatim (ElevenLabs: stability,
        # similarity_boost, style, use_speaker_boost — see the anti-robovoice notes in voice-design)
        "voice_settings": voice_settings if isinstance(voice_settings, dict) else None,
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


def assistant_texts(lines) -> list[str]:
    """The text of every assistant message in the transcript, oldest first — messages carrying no
    text part at all (a pure tool call) are not messages we could ever speak, so they are skipped."""
    texts: list[str] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        parts = [c.get("text", "") for c in (msg.get("content") or []) if c.get("type") == "text"]
        if any(parts):
            texts.append("\n".join(parts))
    return texts


def marked_bodies(text: str, marker: str) -> list[str]:
    """One message's marker-tagged lines, marker and surrounding space stripped. A bare marker
    yields '' and is KEPT here: the caller needs to tell "marked nothing" from "marked nothing at
    all"."""
    return [ln.lstrip()[len(marker):].strip() for ln in text.splitlines() if ln.lstrip().startswith(marker)]


def extract_from_lines(lines, marker: str, limit: int, *, all_messages: bool = False, accept=None) -> str | None:
    """Marker-tagged text joined into one utterance and clipped to ``limit``.

    Scope is the event's: the LAST assistant message for Stop, and with ``all_messages`` every
    assistant message (the PostToolUse path — the line worth hearing may be several messages back
    by the time a tool returns).

    ``accept`` is the ledger's veto: a predicate over ``(message_index, line)`` that returns False
    for a line already spoken. The index is passed because that pair — not the text alone — is what
    the ledger keys on: a repeated sentence in a later message is a new line. Lines it rejects are
    dropped, and a scope whose every marked line is rejected reads as None — "nothing NEW yet",
    which is the same answer a not-yet-flushed message gives, and the same answer the pre-ledger
    code gave when the extract equalled the last spoken line. Absent (the eager-off Stop path),
    every marked line is taken — the pre-ledger behaviour, unchanged.

    Returns None when NO assistant message has reached the transcript yet — the flush-race
    signature the caller retries on — and '' when the message IS present and parsed but yields no
    spoken text (no marker line, or a bare marker with nothing after it): a re-read cannot change a
    parsed message, so '' means exit at once, never backoff."""
    texts = assistant_texts(lines)
    if not texts:
        return None
    scope = list(enumerate(texts))
    marked = [(i, body) for i, text in (scope if all_messages else scope[-1:]) for body in marked_bodies(text, marker)]
    speakable = [(i, body) for i, body in marked if body]
    if marked and not speakable:
        log("marker with no text")  # a bare marker is a decided "nothing to say", not a race
    if not speakable:
        return ""
    fresh = [body for i, body in speakable if accept is None or accept(i, body)]
    if not fresh:
        return None  # every marked line is already in the ledger: nothing new to say YET
    return " ".join(fresh)[:limit]


def extract(path: str, marker: str, limit: int, *, all_messages: bool = False, accept=None) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return extract_from_lines(fh, marker, limit, all_messages=all_messages, accept=accept)
    except OSError:
        return None  # an unreadable transcript is "nothing extracted yet", retried like a race


def marked_history(path: str, marker: str, *, include_last: bool) -> list[tuple[int, str]]:
    """Every marked line already in the transcript as ``(message_index, line)`` — what first-run
    seeding writes off as history, in the shape the ledger keys on.

    ``include_last`` is the event's scope again, inverted: the PostToolUse path has no claim on any
    of it (include_last=True — never recite a session back at its user), while the Stop path is
    entitled to the last message, which is the turn it was invoked to speak (include_last=False)."""
    try:
        with open(path, encoding="utf-8") as fh:
            texts = assistant_texts(fh)
    except OSError:
        return []
    scope = list(enumerate(texts))
    if not include_last:
        scope = scope[:-1]
    return [(i, body) for i, text in scope for body in marked_bodies(text, marker) if body]


# --- the spoken-ledger: one memory, both event paths -------------------------------------------


def ledger_key(transcript_path: str, index: int, line: str) -> str:
    """The identity of one spoken line: which session, which assistant message, which text.

    Both coordinates matter. The transcript path, because the same sentence in two different
    sessions is two different lines and each deserves to be heard. The message index, because a
    session says «Done.» over and over: keyed by text alone the second one would be mistaken for
    the first and go permanently silent, which is precisely what a rolling per-session ledger must
    not do. An index is safe as an identity because a transcript is append-only — message N stays
    message N for as long as the session lives."""
    return hashlib.sha1(f"{transcript_path}\n{index}\n{line}".encode()).hexdigest()[:16]


def seed_marker(transcript_path: str) -> str:
    """The per-transcript "I have seen this one before" record. Its absence — not an empty ledger,
    which any trim can produce — is what makes a transcript first-run."""
    return SEED_PREFIX + hashlib.sha1(transcript_path.encode()).hexdigest()[:16]


def read_ledger() -> list[str]:
    """The ledger's entries, oldest first. An absent or unreadable ledger is an empty one — the
    cost of being wrong here is a line spoken twice, never a turn that fails."""
    try:
        with open(_LEDGER_PATH, encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    except OSError:
        return []


def append_ledger(entries) -> None:
    """Claim entries. Append-only and one write, so a concurrent firing's claim cannot be lost to a
    read-modify-write of ours."""
    entries = [e for e in entries if e]
    if not entries:
        return
    try:
        with open(_LEDGER_PATH, "a", encoding="utf-8") as fh:
            fh.write("".join(f"{e}\n" for e in entries))
    except OSError as err:
        # a ledger we cannot write is a ledger that cannot dedup: say so once, keep speaking
        log(f"ledger unwritable: {type(err).__name__}: {err}")


def trim_ledger() -> None:
    """Keep the ledger a rolling window: the last LEDGER_LINES line entries, plus the last
    LEDGER_SEEDS seed markers. Seed markers survive separately because a session that outlives its
    own marker would be re-seeded — and re-seeding writes off the line it was about to speak."""
    entries = read_ledger()
    seeds = [e for e in entries if e.startswith(SEED_PREFIX)]
    spoken = [e for e in entries if not e.startswith(SEED_PREFIX)]
    if len(seeds) <= LEDGER_SEEDS and len(spoken) <= LEDGER_LINES:
        return
    kept = seeds[-LEDGER_SEEDS:] + spoken[-LEDGER_LINES:]
    try:
        fd, tmp = tempfile.mkstemp(prefix="voice-loop-ledger-", dir=os.path.dirname(_LEDGER_PATH))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(f"{e}\n" for e in kept))
        os.replace(tmp, _LEDGER_PATH)
    except OSError as err:
        log(f"ledger trim failed: {type(err).__name__}: {err}")


# --- the inter-firing lock: one speaker at a time, and the loser never waits ---------------------


class _NoLock:
    """What acquire_lock returns when the platform or the filesystem cannot give us a real lock:
    speaking proceeds unserialized rather than going silent. Closing it is a no-op."""

    def close(self) -> None:
        pass


def acquire_lock(grace=()):
    """Take the exclusive speaking lock WITHOUT blocking.

    Returns the open lockfile (the flock lives as long as it stays open — and dies with the process,
    which is what lets a taken-over chain release it by exiting), a _NoLock when locking is not
    available at all, or None when another firing holds it.

    ``grace`` is a short sequence of pauses to re-try over, and only the Stop path passes one. An
    eager firing passes nothing and loses instantly: a firing that waits is a firing that piles up
    (one blocked python per tool call), that no takeover can supersede and no echo guard can stop —
    it holds no entry in ``playing.pid`` while it waits. Losing costs it nothing, because it has
    claimed nothing and the next firing is one tool call away."""
    try:
        fh = open(_LOCK_PATH, "w", encoding="utf-8")
    except OSError:
        return _NoLock()
    if fcntl is None:  # pragma: no cover - POSIX-only; Linux and macOS both have fcntl
        return fh
    for pause in (None, *grace):
        if pause is not None:
            time.sleep(pause)
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            continue
    fh.close()
    return None


def release_lock(lock) -> None:
    """Closing the file releases the flock; a _NoLock closes to nothing."""
    if lock is None:
        return
    try:
        lock.close()
    except OSError:
        pass


def chunk_sentences(text: str, min_chars: int = MIN_CHUNK_CHARS) -> list[str]:
    """Sentence-boundary chunks for streaming; tiny sentences merge until >= min_chars.

    A short tail merges INTO the previous chunk (the plan is computed before playback starts, so
    growing the last chunk is free) — only a text shorter than min_chars yields one small chunk.
    """
    chunks: list[str] = []
    buf = ""
    for sentence in _SENTENCE_END.split(text):
        buf = f"{buf} {sentence}".strip()
        if len(buf) >= min_chars:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks:
            chunks[-1] = f"{chunks[-1]} {buf}"
        else:
            chunks.append(buf)
    return chunks


def _post(url: str, headers: dict, payload: dict, timeout: float) -> bytes | None:
    """POST JSON, return the response body (even on an HTTP error — the body is the diagnosis,
    exactly like ``curl -o`` wrote it). None only when the server was unreachable. Proxies are
    bypassed (parity with ``curl --noproxy '*'``)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
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
        # the reason is host/errno only — never a header, never the key
        log(f"synthesis unreachable: {getattr(err, 'reason', err)}")
        return None


def _get(url: str, timeout: float) -> bytes | None:
    """GET a URL, return the body — None on any failure. Proxies bypassed like _post."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def server_offers_streaming(health_body: bytes | None) -> bool:
    """True only when GET /health answered valid JSON with ``"streaming": true`` — an older server
    (no such key), garbage, or an unreachable server all mean the blob path."""
    if not health_body:
        return False
    try:
        health = json.loads(health_body)
    except ValueError:
        return False
    return isinstance(health, dict) and health.get("streaming") is True


def parse_sse(lines):
    """(event, data) pairs off a raw SSE line stream (bytes or str), per the server's strict
    framing: ``event: <name>`` then ``data: <one line of JSON>`` then a blank line. A data line
    with no preceding event, or undecodable JSON, is skipped — never fatal."""
    event = None
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: ") and event is not None:
            try:
                data = json.loads(line[len("data: "):])
            except ValueError:
                continue
            if isinstance(data, dict):
                yield event, data
            event = None


def iter_stream_audio(lines):
    """Decoded WAV bytes per ``chunk`` event, stopping at the terminal ``end``/``error`` event.

    Per the contract, chunks already received stay valid when an ``error`` arrives — the caller
    plays what it got; the error is logged here. A dropped connection mid-read is the same shape:
    log, stop, keep what arrived."""
    count = 0
    try:
        for event, data in parse_sse(lines):
            if event == "chunk":
                try:
                    audio = base64.b64decode(str(data.get("audio", "")))
                except (ValueError, TypeError):
                    log(f"stream chunk {data.get('index')} had undecodable base64 — stopping")
                    return
                if audio:
                    count += 1
                    yield audio
            elif event == "end":
                return
            elif event == "error":
                log(f"stream error after {data.get('chunks')} chunk(s): {str(data.get('error'))[:200]}")
                return
    except (OSError, ValueError) as err:
        log(f"stream read failed after {count} chunk(s): {getattr(err, 'reason', err)}")
        return


def stream_source(lines):
    """The fallback decision, made in one place: pull the FIRST chunk eagerly; a stream that dies
    before it (refused, error event first, connection dropped) returns None so the caller can fall
    back to the blob endpoint once. After the first chunk there is no fallback — the returned
    iterator replays it and continues live."""
    audio = iter_stream_audio(lines)
    first = next(audio, None)
    if first is None:
        return None
    return itertools.chain([first], audio)


def _open_stream(endpoint: str, payload: dict, timeout: float):
    """POST /tts/stream and return the live response (iterable line by line), or None on any
    failure before the response starts — HTTP errors are the pre-synthesis JSON refusals."""
    request = urllib.request.Request(
        f"{endpoint}/tts/stream",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as err:
        try:
            body = err.read()[:200].decode("utf-8", "replace")
        except OSError:
            body = ""
        log(f"stream refused ({err.code}): {body}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        log(f"stream unreachable: {getattr(err, 'reason', err)}")
        return None


def synthesize(text: str, s: dict, key: str) -> bytes | None:
    """One chunk -> audio bytes, or None (with the reason logged). Mirrors speak.sh's checks:
    empty body and JSON-error-document responses are dropped, not played."""
    if s["backend"] == "cloud":
        if s["provider"] == "elevenlabs":
            # the response container follows output_format (mp3 by default) — your speak.player
            # must be able to play it (macOS afplay does; on Linux use mpg123 or ffplay)
            endpoint = s["endpoint"] or "https://api.elevenlabs.io"
            payload: dict = {"text": text, "model_id": s["cloud_model"]}
            if s["voice_settings"] is not None:
                payload["voice_settings"] = s["voice_settings"]
            url = f"{endpoint}/v1/text-to-speech/{s['voice_id']}?output_format={s['output_format']}"
            body = _post(url, {"xi-api-key": key}, payload, s["timeout"])
        else:
            # OpenAI-compatible speech API
            endpoint = s["endpoint"] or "http://127.0.0.1:8355"
            payload = {
                "model": s["cloud_model"],
                "voice": s["voice_id"] or "alloy",
                "input": text,
                "response_format": "wav",
            }
            body = _post(f"{endpoint}/v1/audio/speech", {"Authorization": f"Bearer {key}"}, payload, s["timeout"])
    else:
        endpoint = s["endpoint"] or "http://127.0.0.1:8355"
        payload = {
            k: v for k, v in (("text", text), ("speaker", s["speaker"]), ("language", s["language"])) if v
        }
        body = _post(f"{endpoint}/tts", {}, payload, s["timeout"])

    if body is None:
        return None
    if not body:
        log(f"empty synthesis from {endpoint}")
        return None
    if body[:1] in (b"{", b"["):
        log(f"synthesis returned an error document: {body[:200].decode('utf-8', 'replace')}")
        return None
    return body


def _write_pidfile(*pids: int) -> None:
    try:
        with open(_PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(" ".join(str(p) for p in pids))
    except OSError:
        pass


def _clear_pidfile() -> None:
    """Drop playing.pid if it is still OURS — the counterpart of _write_pidfile, and every path
    that writes the file owes it one. A pidfile left behind holding an exited pid is not inert:
    dictate.py reads it as "a chain is playing", signals a pid that is gone, and skips the pkill
    fallback it keeps for exactly this case (a chain that died without cleanup)."""
    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            if fh.read().split()[:1] == [str(os.getpid())]:
                os.unlink(_PID_PATH)
    except (OSError, IndexError):
        pass


def _cmdline_of(pid: int) -> str | None:
    """/proc/<pid>/cmdline with NULs as spaces — None when unreadable (process already gone, or
    not ours to inspect). Linux-only by construction; callers gate on the platform.
    Duplicated helper — keep in sync with dictate.py."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def pid_looks_like_speak(pid: int, read_cmdline=_cmdline_of, platform_id: str = sys.platform) -> bool:
    """PID-reuse guard (duplicated helper — keep in sync with dictate.py): a pidfile outlives its
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


def _recorded_pids() -> list[int]:
    """The speaking chain playing.pid records, minus our own pid and minus an unreadable or
    unparseable file. One reader for both users of that file — the takeover that SIGTERMs the chain
    and the waiter that waits it out — so neither can drift into seeing it differently."""
    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            pids = [int(tok) for tok in fh.read().split()]
    except (OSError, ValueError):
        return []
    return [pid for pid in pids if pid and pid != os.getpid()]


def take_over() -> None:
    """A fresher line supersedes a still-playing older one: SIGTERM exactly the PIDs the previous
    chain recorded (its python process + its current player child) — nothing else — and only
    after each pid passes the PID-reuse guard above. A tts.command child records no marker in its
    own argv, but killing its identity-verified python parent stops it too (the SIGTERM handler
    terminates the child). Same semantics as dictate.py's echo guard (cross-script contract)."""
    for pid in _recorded_pids():
        if pid_looks_like_speak(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def playback_is_live() -> bool:
    """True while ANOTHER voice-loop speaking chain is still on the air.

    The pidfile alone does not answer this: a chain that was superseded leaves through _on_sigterm,
    which exits before the cleanup that would remove the file, so the record outlives the process.
    ``os.kill(pid, 0)`` is the existence probe — it delivers no signal — and pid_looks_like_speak is
    the same PID-reuse guard the takeover applies before signalling. A pid that is gone, that is not
    ours to signal, or that the kernel has since handed to somebody else is not one to wait for."""
    for pid in _recorded_pids():
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        if pid_looks_like_speak(pid):
            return True
    return False


def wait_out_playback(read, settled, text=None):
    """Keep re-reading the transcript for as long as an older chain is still audibly playing.

    Entered at exactly one place: where the Stop path had exhausted BACKOFF with nothing new and
    used to give up in silence. So it can only turn a DROPPED line into a late one — it never
    delays a line that had already arrived, because a settled read never reaches here.

    ``read`` is one transcript read, ``settled`` decides whether its result is final, and ``text``
    is the unsettled read we arrived with — handed straight back when nothing is playing at all, so
    the caller still knows WHICH give-up it is about to log (a stale repeat reads differently from
    a transcript that never flushed). Returns the first settled read, or, once the loop has run,
    the freshest unsettled one.

    Bounded twice: the loop ends the instant playback does, and it never takes more than
    PLAYBACK_POLLS polls however long a player wedges for."""
    polls = 0
    while playback_is_live():
        if polls >= PLAYBACK_POLLS:
            log(f"stop: still playing after {polls * PLAYBACK_POLL:.0f}s of waiting — waiting no longer")
            return text
        time.sleep(PLAYBACK_POLL)
        polls += 1
        text = read()
        if settled(text):
            log(f"stop: the line waited {polls * PLAYBACK_POLL:.2f}s behind the one in front — queued, not dropped")
            return text
    if polls:
        # Playback just ended. The transcript often lands in that same instant, so look once more
        # before handing the answer back — an unsettled one is the caller's give-up to log.
        text = read()
        log(f"stop: the line in front finished after {polls * PLAYBACK_POLL:.2f}s of waiting")
    return text


# --- the contour check (#40): the poller writes the file, this hook is the page --------------------


def _utcnow() -> datetime:
    """Wall-clock UTC, at one seam — the staleness bound is the only thing in this file that needs
    it, and a test freezes it here rather than sleeping."""
    return datetime.now(timezone.utc)


def read_contour_status(path: str) -> dict:
    """The poller's whole status object — {} on ANY read problem.

    A missing contour.json is the normal state of an install that never set the poller up, and a
    half-written one is a turn that must not break: both are "nothing to say" and both come back
    empty, which is the ONE case that stays silent unconditionally.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def _status_age(status: dict, now: datetime) -> float | None:
    """Seconds since the poller wrote this file, or None when it does not say (or says something
    that will not parse). None is not "fresh": a status file whose own timestamp cannot be read
    cannot vouch for anything, and the caller treats it exactly like an expired one."""
    at = status.get("at")
    if not isinstance(at, str):
        return None
    try:
        written = datetime.fromisoformat(at)
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return (now - written).total_seconds()


def contour_alerts(status: dict, now: datetime) -> list[dict]:
    """What this status file gives the hook to say: its own active alerts, or the staleness page.

    #40 opens on "no way to tell 'the contour is fine' from 'nobody looked'", and a status file
    alone cannot tell them apart: remove the cron entry and contour.json freezes at its last
    green poll, quiet forever. So the poller writes its own freshness bound (``max_age``) into
    the file and this is the consumption side of it — past that bound the file is not evidence
    about the contour any more, it is evidence that nobody is polling, and only THAT is voiced.
    The file's own alerts are dropped with it on purpose: a reading nobody refreshed says nothing
    about now, and the one true thing left is that nobody is looking.

    Entries without a string key and message are not alerts this hook can voice or dedup, so they
    are dropped rather than voiced oddly.
    """
    if not status:
        return []
    max_age = status.get("max_age")
    if not isinstance(max_age, (int, float)) or isinstance(max_age, bool) or max_age <= 0:
        max_age = CONTOUR_MAX_AGE  # a file from a poller that predates the bound still gets one
    age = _status_age(status, now)
    if age is None:
        return [_stale_alert("the contour poller's status file carries no readable timestamp")]
    if age > max_age:
        return [_stale_alert(f"nobody has polled the voice contour for {int(age // 60)} minutes")]
    alerts = status.get("alerts")
    if not isinstance(alerts, list):
        return []
    return [
        alert
        for alert in alerts
        if isinstance(alert, dict) and isinstance(alert.get("key"), str) and isinstance(alert.get("message"), str)
    ]


def _stale_alert(detail: str) -> dict:
    """One key, so the announced-ledger says it once and pruning re-arms it when polling returns."""
    return {
        "key": "poller-stale",
        "kind": "poller-stale",
        "service": "",
        "message": f"{detail} — the contour is not being watched",
    }


def _write_contour_announced(keys, path: str) -> None:
    """One key per LINE, replaced atomically. Both halves matter: a key is an alert key, which
    contains whatever the operator named their service, so whitespace inside one must survive the
    round trip — and a reader must never catch this file mid-truncation."""
    body = "".join(f"{key}\n" for key in sorted(keys))
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".contour-announced-")
    except OSError:
        return  # a state dir that will not write costs the dedup, never the turn
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_contour_announced(path: str) -> set[str]:
    """The announced keys. Split on LINES, never on whitespace: an alert key is "<kind>:<service>"
    and a service the operator called "tts worker" tokenised into two keys that match nothing, so
    the pruning below wiped the whole file and the alert was voiced again on every single firing."""
    try:
        with open(path, encoding="utf-8") as fh:
            return {line.strip() for line in fh.read().splitlines() if line.strip()}
    except OSError:
        return set()


def sync_contour_announced(active: list[str], path: str) -> set[str]:
    """The announced-ledger pruned to the alerts still active; returns the active keys already
    voiced. Pruning is what lets a condition that cleared and came back page AGAIN — a stale key
    left behind would mute its return forever."""
    announced = _read_contour_announced(path)
    kept = announced & set(active)
    if kept != announced:
        _write_contour_announced(kept, path)
    return kept


def mark_contour_announced(keys, path: str) -> None:
    """Record keys as voiced. Called only AFTER a delivery that happened (see contour_check) and
    only under the speaking lock, which is what makes this read-modify-write safe against a second
    firing doing the same thing in the same assistant block."""
    _write_contour_announced(_read_contour_announced(path) | set(keys), path)


def contour_check(config: dict, t0: float, event: str = "Stop") -> None:
    """Voice every active contour alert not yet announced — #40's page, delivered mid-hook.

    Called once per hook firing on the event path that owns it, after the turn's own speech.
    Silence is the overwhelming common case and is cheap: no poller status file (the normal state
    of an install that never set the poller up), no active alerts, or nothing new — all cost one
    tolerant read. Opted out with ``contour.alerts: false``; a user who switched speech off
    entirely (``speak.enabled``) is not paged either.
    """
    s = resolve_settings(config, platform.system())
    if not s["enabled"]:
        return
    if event == "PostToolUse" and not s["eager"]:
        # The opt-in no-op, WHOLE. hooks.json registers PostToolUse unconditionally, so with
        # speak.eager off this file is executed after every single tool call; running the check
        # from there took over in-flight playback mid-turn and made a default-off install pay for
        # a feature it never enabled. Eager off means ONE event path — Stop — the page included.
        return
    if cfg(config, "contour.alerts", True) in (False, "false"):
        return
    alerts = contour_alerts(read_contour_status(_CONTOUR_PATH), _utcnow())

    # ONE lock, both event paths. The announced-ledger is a read-modify-write, and the check is a
    # second event path into it: two tool calls in one assistant block gave two firings that both
    # read an empty ledger, both saw the alert as fresh, and both spoke. The acquire never blocks
    # — a firing that loses leaves the alert UNANNOUNCED, and the next firing is one tool call
    # away. It also covers the prune, so the ledger is never compared against a set another
    # firing is half-way through changing.
    lock = acquire_lock()
    if lock is None:
        log("contour: another firing is speaking — alert left unannounced for the next firing")
        return
    try:
        # The sync runs even with NO active alerts: pruning is what lets a cleared-and-returned
        # condition page again, and it can only happen where the active set is compared.
        announced = sync_contour_announced([alert["key"] for alert in alerts], _CONTOUR_ANNOUNCED_PATH)
        fresh = [alert for alert in alerts if alert["key"] not in announced]
        if not fresh:
            return
        text = f"Voice contour: {'; '.join(alert['message'] for alert in fresh)}"
        log(f"contour: voicing {len(fresh)} alert(s): {text[:80]}")
        signal.signal(signal.SIGTERM, _on_sigterm)
        if not s["eager"]:
            # With eager off this runs on Stop, after the turn's own line has finished: the alert
            # supersedes a chain still playing exactly like a fresher turn's line would.
            take_over()
        _write_pidfile(os.getpid())
        if play_text(text[: s["max_chars"]], s, t0, extract_ms=0):
            # Announced ONLY for a delivery that happened, unlike the marked-line ledger. An
            # alert's delivery path is the very service most alerts are about: with the shipped
            # default (tts.backend lan → 127.0.0.1:8355, and the same server as the only polled
            # service) a dead speech server means the page cannot be synthesized, and claiming it
            # first made "the thing that speaks is down" the one condition this hook could never
            # report. A failed delivery re-arms: the key stays unannounced, so the next firing
            # tries again for as long as the condition holds.
            mark_contour_announced([alert["key"] for alert in fresh], _CONTOUR_ANNOUNCED_PATH)
        else:
            log("contour: the page reached no player — left unannounced, to be retried")
    finally:
        # The counterpart every writer of playing.pid needs: without it one page left the file
        # behind holding a dead pid, and dictate.py's echo guard then took the "there are pids"
        # branch forever and skipped the pkill fallback it keeps for a chain that died uncleanly.
        _clear_pidfile()
        release_lock(lock)


def _on_sigterm(signum, frame):  # noqa: ARG001 — signal-handler signature
    """We were superseded (or the harness timed us out): stop the player, close the stream socket,
    drop temp files, exit 0."""
    proc = _live.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    stream = _live.get("stream")
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass
    for path in list(_live["files"]):
        try:
            os.unlink(path)
        except OSError:
            pass
    os._exit(0)


def _synthesized_audio(chunks: list[str], s: dict, key: str):
    """The blob path's audio source: one /tts (or cloud) call per sentence chunk. Stops at the
    first failed synthesis, exactly like the pre-stream loop did."""
    for part in chunks:
        audio = synthesize(part, s, key)
        if audio is None:
            return
        yield audio


def _play_stream(audio_iter, s: dict, t0: float) -> tuple[int, int, int, int | None]:
    """Produce chunk N+1 while chunk N plays. The source is ANY iterator of playable audio bytes —
    locally-synthesized sentence chunks and decoded SSE chunks enter the same queue. One player
    subprocess per chunk; the next Popen is issued the moment the previous .wait() returns, so the
    only gap is process spawn.

    ``t0`` is the hook's start instant (``time.monotonic()`` in main) — NOT a local clock started
    here. That is deliberate: by the time this function is entered, the caller may already have
    burned seconds the listener waited through (the /health probe, opening the stream, and the
    first chunk pulled eagerly off ``audio_iter`` by stream_source). Measuring from t0 is what
    makes the reported number the real time-to-first-sound instead of the player-loop's own
    near-zero offset.

    Returns (chunks_played, total_bytes, first_audio_ms, last_rc), where first_audio_ms is
    t0 -> the spawn of the first player process, and -1 when nothing ever played."""
    player_argv = shlex.split(s["player"])
    proc: subprocess.Popen | None = None
    proc_wav: str | None = None
    played = 0
    total_bytes = 0
    first_audio_at: float | None = None
    rc: int | None = None

    def reap() -> None:
        nonlocal proc, proc_wav, rc
        if proc is not None:
            rc = proc.wait()
            proc = None
        if proc_wav is not None:
            _live["files"].discard(proc_wav)
            try:
                os.unlink(proc_wav)
            except OSError:
                pass
            proc_wav = None

    try:
        for audio in audio_iter:  # the pull overlaps with the previous chunk's playback
            fd, wav = tempfile.mkstemp(prefix="voice-loop-speak-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(audio)
            _live["files"].add(wav)
            reap()  # let the previous chunk finish before starting this one
            if first_audio_at is None:
                first_audio_at = time.monotonic()  # sound starts at the spawn below
            try:
                proc = subprocess.Popen(
                    player_argv + [wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except OSError as err:
                log(f"player failed: {err}")
                _live["files"].discard(wav)
                try:
                    os.unlink(wav)
                except OSError:
                    pass
                break
            _live["proc"] = proc
            proc_wav = wav
            _write_pidfile(os.getpid(), proc.pid)
            played += 1
            total_bytes += len(audio)
        reap()
    finally:
        _live["proc"] = None
        for path in list(_live["files"]):
            try:
                os.unlink(path)
            except OSError:
                pass
            _live["files"].discard(path)

    first_ms = -1 if first_audio_at is None else int((first_audio_at - t0) * 1000)
    return played, total_bytes, first_ms, rc


def play_text(text: str, s: dict, t0: float, *, extract_ms: int) -> bool:
    """Everything from "who makes the sound" to the timings log — one text, spoken.

    This is the whole playback tail the marked-line flow grew: the local ``tts.command`` path, the
    cloud key, server-side streaming with the one-shot blob fallback, and the timings line. It is
    extracted, not duplicated, so the contour check's alert (#40) travels EXACTLY the path a marked
    line takes — same synthesis, same fallback, same player. The takeover/pidfile prelude stays
    with the callers: WHO supersedes whom is a property of the event path, not of the playback.

    Returns whether a player was actually handed audio. The marked-line path ignores it (a line is
    claimed before synthesis on purpose — idempotence beats completeness for something that talks
    out loud), but the contour check CANNOT: its delivery path is the very service most of its
    alerts are about, so "did this reach a player" is the difference between a page and permanent
    silence. Same fact the "nothing played" log line already stated, given to the caller.
    """
    if s["command"]:
        # tts.command: speak locally without any server (e.g. "say -v Milena" on macOS, or a
        # piper pipeline). The command receives the text on stdin and produces the sound itself
        # — synthesis and playback are one opaque step, so there is nothing to pipeline: the
        # whole text goes in one call. first_audio_ms keeps its definition here too: t0 -> the
        # spawn of the process that makes the sound (this command IS the player).
        first_ms = int((time.monotonic() - t0) * 1000)
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", s["command"]],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as err:
            log(f"local command failed: {err}")
            return False
        _live["proc"] = proc
        _write_pidfile(os.getpid(), proc.pid)
        proc.communicate(input=text.encode("utf-8"))
        _live["proc"] = None
        log(f"local command rc={proc.returncode}")
        total_ms = int((time.monotonic() - t0) * 1000)
        log(f"timings extract_ms={extract_ms} first_audio_ms={first_ms} total_ms={total_ms}")
        return proc.returncode == 0

    key = ""
    if s["backend"] == "cloud":
        key = read_key(s["key_file"], s["key_env"], os.environ)
        if not key:
            log(f"cloud tts: no key (key_file unset/unreadable and ${s['key_env']} empty)")
            return False

    result = None
    via = "tts"
    if s["backend"] != "cloud":
        # Server-side streaming: one cheap /health probe per invocation decides the path. Only
        # a stream that dies BEFORE its first chunk falls back to the blob endpoint (once);
        # after the first chunk we play what arrives and stop where the stream stops.
        endpoint = s["endpoint"] or "http://127.0.0.1:8355"
        if server_offers_streaming(_get(f"{endpoint}/health", min(s["timeout"], HEALTH_TIMEOUT))):
            payload = {
                k: v for k, v in (("text", text), ("speaker", s["speaker"]), ("language", s["language"])) if v
            }
            resp = _open_stream(endpoint, payload, s["timeout"])
            if resp is not None:
                _live["stream"] = resp
                try:
                    # stream_source pulls the first chunk eagerly: the whole first synthesis
                    # is waited out HERE, which is exactly why _play_stream measures from t0.
                    source = stream_source(resp)
                    if source is not None:
                        result = _play_stream(source, s, t0)
                        via = "stream"
                    else:
                        log("stream died before its first chunk — falling back to /tts")
                finally:
                    _live["stream"] = None
                    try:
                        resp.close()
                    except OSError:
                        pass
    if result is None:
        # blob path: the client does the sentence chunking (older server, cloud, or fallback)
        result = _play_stream(_synthesized_audio(chunk_sentences(text), s, key), s, t0)
    played, total_bytes, first_ms, rc = result
    total_ms = int((time.monotonic() - t0) * 1000)
    if played:
        log(f"played rc={rc} bytes={total_bytes} chunks={played} via={via}")
    else:
        # The line was claimed and then never made a sound — synthesis produced nothing, or the
        # player would not spawn. Both already log their own reason; this is the consequence,
        # said plainly, so the log never has to be read backwards to notice a lost line.
        log(f"nothing played via={via} — the line was claimed but no audio reached the player")
    log(f"timings extract_ms={extract_ms} first_audio_ms={first_ms} total_ms={total_ms}")
    return bool(played)


def main() -> int:
    # THE clock: every logged timing (extract_ms, first_audio_ms, total_ms) is an offset from this
    # one instant, so the three are directly comparable and none can hide a wait the other counted.
    t0 = time.monotonic()
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
    except OSError:
        pass
    # Before anything else — before the config, before the enabled check: the heartbeat. Even a
    # firing that will speak nothing (disabled, the eager no-op, nothing marked) is proof the
    # harness still calls the hook, and that proof is the whole point of the stamp.
    stamp_hook_fired()

    cfg_path = os.environ.get(
        "VOICE_LOOP_CONFIG",
        os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "voice-loop/config.json"),
    )
    s = resolve_settings(load_config(cfg_path), platform.system())
    if not s["enabled"]:
        return 0

    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        log("hook payload was not JSON — nothing to speak")
        return 0
    if not isinstance(payload, dict):
        payload = {}
    # Stop is the default for a payload that names no event: the hook has always been a Stop hook,
    # and an unknown event is treated as one rather than as the eager path it did not ask for.
    _fired["event"] = str(payload.get("hook_event_name") or "Stop")
    eager = _fired["event"] == "PostToolUse"
    if eager and not s["eager"]:
        return 0  # the opt-in no-op: one stdin read per tool call, and the transcript untouched
    transcript = payload.get("transcript_path")
    if not transcript or not os.path.isfile(transcript):
        # Both shapes — a payload naming no transcript at all, and one naming a file that is not
        # there — end the turn's speech before a single line has been read. Neither was visible.
        log(f"no transcript to read: transcript_path={transcript!r}")
        return 0

    # The ledger, the lock and the seeding are eager mode's machinery, and they exist ONLY when the
    # user has opted in. With eager off there is one event path, no race to be idempotent against,
    # and therefore nothing to gate on: everything below reduces to the pre-0.3.2 Stop hook.
    ledger_on = s["eager"]

    lock = None
    superseded = False  # a turn supersedes the chain before it exactly once, wherever it had to
    try:
        if ledger_on:
            # Everything from reading the ledger to finishing playback happens under this lock, so a
            # line cannot be claimed twice or spoken over. The acquire never blocks.
            if eager:
                lock = acquire_lock()
                if lock is None:
                    log("eager: another firing is speaking — line left unclaimed for the next firing")
                    return 0
            else:
                # Stop: give the holder its beat, then supersede it — the SIGTERM releases that
                # chain's flock with it — and take one more shot.
                lock = acquire_lock(LOCK_GRACE)
                if lock is None:
                    take_over()
                    superseded = True
                    lock = acquire_lock(LOCK_GRACE)
                if lock is None:
                    log("stop: the speaking lock is still held — lines left unclaimed")
                    return 0

            trim_ledger()
            # First run for this transcript: everything already in it is history. Write it off
            # without speaking it, so enabling eager mid-session cannot recite the session back.
            if seed_marker(transcript) not in read_ledger():
                history = marked_history(transcript, s["marker"], include_last=eager)
                append_ledger([ledger_key(transcript, i, line) for i, line in history] + [seed_marker(transcript)])
                log(f"seeded {len(history)} line(s) of history for this transcript — not spoken")

        try:
            with open(_LAST_PATH, encoding="utf-8") as fh:
                prev = fh.read()
        except OSError:
            prev = ""

        claimed: list[str] = []
        vetoed: list[str] = []

        def read_fresh() -> str | None:
            """One transcript read: the marked lines this event owns, minus everything the ledger
            already accounts for. The ledger is re-read every time — a firing that ran between our
            rounds may have claimed a line since. With eager off there is no ledger and no veto:
            the read is exactly the pre-0.3.2 one, the last message's marked lines, all of them."""
            if not ledger_on:
                return extract(transcript, s["marker"], s["max_chars"])
            claimed.clear()
            vetoed.clear()  # what the ledger turned away, so a quiet exit can say WHY it was quiet
            seen = set(read_ledger())

            def accept(index: int, line: str) -> bool:
                key = ledger_key(transcript, index, line)
                if key in seen:
                    vetoed.append(key)
                    return False
                seen.add(key)  # a line repeated within one read is still one line
                claimed.append(key)
                return True

            return extract(transcript, s["marker"], s["max_chars"], all_messages=eager, accept=accept)

        def settled(value: str | None) -> bool:
            """Is this read final? '' is: a parsed message with no marked text in it cannot change
            on a re-read. A non-empty extract is, unless the eager-off dedup recognises it as the
            previous utterance. None never is — that is the flush race's own signature."""
            return value == "" or bool(value and (ledger_on or value != prev))

        # Flush race: read immediately; retry ONLY on the race signatures — nothing new extracted
        # (None: no assistant message yet, or nothing but lines already spoken), or, with the ledger
        # off, an extract identical to the previously spoken line. A parsed message that yields ''
        # (no marker, or a marker with no text) is FINAL: exit at once, zero backoff. The eager path
        # never retries — a line half-written now is caught by the next firing, which costs nothing.
        text = read_fresh()
        if not eager:
            for pause in BACKOFF:
                if settled(text):
                    break
                time.sleep(pause)
                text = read_fresh()
            if not settled(text):
                # 2.65 s of ladder against a clip that runs ~10 s: this is where the line used to
                # be dropped, in silence. The retry condition has not changed — we are here only
                # because there is still nothing new — so waiting out the line in front costs a
                # ready line nothing and buys a late one its turn.
                text = wait_out_playback(read_fresh, settled, text)
        if not text:
            if text is None and not eager:
                # A Stop that leaves without speaking has abandoned the line for good: unlike an
                # eager firing, it has no successor one tool call behind it. The ledger's veto is
                # the one quiet case that ISN'T a loss — eager already said those lines out loud.
                if vetoed:
                    log(f"stop: nothing new — the ledger already accounts for {len(vetoed)} marked line(s)")
                else:
                    log("stop: gave up with nothing new in the transcript — a line written now is DROPPED")
            return 0
        # dedup: the stale previous turn, dropped, not spoken twice. This is the WHOLE of the
        # eager-off memory — deliberately shallow, so a line repeated later in a session is heard
        # again. With the ledger on, the ledger has already answered the question more precisely,
        # and re-asking it here would silence exactly the repeat the message index exists to keep.
        if not ledger_on and text == prev:
            log(f"stop: dropped a read identical to the last spoken line (dedup): {text[:80]}")
            return 0
        extract_ms = int((time.monotonic() - t0) * 1000)

        try:
            with open(_LAST_PATH, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        # Claimed BEFORE a single byte is synthesized: a line lost to a failing server is silence
        # once, where an unclaimed line is the same sentence said twice by the next firing.
        append_ledger(claimed)
        log(f"text: {text[:80]}")

        signal.signal(signal.SIGTERM, _on_sigterm)
        if not eager and not superseded:
            # TAKEOVER: a fresher turn supersedes a still-playing older one. Unless the lock ladder
            # above already had to do it, this is where it happens — and it still has work to do
            # when the lock came free at once, because a chain running without a lock at all
            # (_NoLock, an unwritable state dir) is invisible to the flock and audible to the user.
            take_over()
        _write_pidfile(os.getpid())

        play_text(text, s, t0, extract_ms=extract_ms)
        return 0
    finally:
        _clear_pidfile()
        # last, and always: the lock is free the moment this returns, so the next firing to try
        # gets straight in rather than losing a race it did not have to lose
        release_lock(lock)


def entry() -> int:
    """The process entry point: the turn's own speech first, then the contour check (#40).

    Two duties share one invocation because the hook registration surface never changes —
    hooks.json has always run exactly this file. The check re-reads the small config the turn
    used, and a failure in it is the outer guard's to log: a broken alert path must never fail a
    turn either. t0 for the check is taken AFTER the turn's speech, so the alert's own
    first_audio_ms measures the alert's own wait, not the line's.

    The EVENT main() was fired for goes with it: an unconditional check here is what made a
    default-off install run the whole page path on every tool call, and the no-op eager promises
    covers this file, not just main().
    """
    rc = main()
    cfg_path = os.environ.get(
        "VOICE_LOOP_CONFIG",
        os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "voice-loop/config.json"),
    )
    contour_check(load_config(cfg_path), time.monotonic(), _fired["event"])
    return rc


if __name__ == "__main__":
    try:
        sys.exit(entry())
    except Exception:  # never fail the turn — a hook error must not surface into the session
        try:
            log(f"unexpected error: {sys.exc_info()[1]!r:.200}")
        except Exception:
            pass
        sys.exit(0)
