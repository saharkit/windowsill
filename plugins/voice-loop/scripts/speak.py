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
* still being written — that ladder is a FIXED 2.65 s, and #106 is the turn it was too short for:
  a long turn (many tool calls, then a long final text) whose flush had not landed when the ladder
  ran out, with no older clip playing to extend the wait, was dropped in silence. So where the
  ladder ends with nothing new, the TRANSCRIPT'S OWN ACTIVITY extends it — while (size, mtime_ns)
  keeps ADVANCING somebody is still appending, and each advance buys one more poll, bounded by
  FLUSH_POLLS (12.5 s) so a wedged writer can never hold the turn open. Its entry condition is the
  ladder's NARROWED: eager-off only (an eager Stop has a successor one tool call away and holds
  speaking.lock while it waits, so waiting there buys nothing and mutes everything), which also
  subsumes the ledger's decided veto. The cost of an IDLE transcript is unchanged: a file that did
  not move while the ladder ran is answered on the first stat, with zero extra sleep, which is
  exactly today's behaviour for a turn that genuinely had nothing to say.
  THE WORST CASE IS THE COMPOSITION, and it is worth stating plainly because the three waits run in
  SEQUENCE from one main(): 2.65 s of ladder, then up to PLAYBACK_POLLS * PLAYBACK_POLL = 20 s of
  waiting out a wedged player, then up to FLUSH_POLLS * FLUSH_POLL = 12.5 s of a transcript that
  keeps growing — **35.15 s** in all, inside the timeout this plugin declares for its own hooks
  (see the FLUSH_POLL constants below, and hooks/hooks.json for the number). Reaching it needs BOTH
  pathologies at once (a player that never exits AND a file appended to throughout); either alone
  is 22.65 s or 15.15 s, and the overwhelming common case is still 0 s.
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
  Every path that abandons a line logs its reason, and since #106 so does every path that abandons
  NOTHING: a Stop firing ALWAYS writes a reason line — nothing marked, the ledger's veto, the dedup
  drop, the give-up after the ladder, speech switched off. Never zero, and not always exactly one:
  a firing that waited says what it waited for as well as what it decided (a wedged player and a
  growing transcript each leave their own line before the verdict). That is what
  makes conformance row 3.12 ("a turn with NO log line at all is a FAIL") literally checkable
  instead of nearly true; the forensics that reopened it were a manual replay of the silent turn
  exiting 0 with no log line at all, which left the log unable to tell "the hook gave up" from
  "the hook was never called". Two of the three earlier silences went with it — the ones with no
  successor to catch the line — and the volume argument goes with them: one line per turn in a log
  that rotates at 1 MB is the price of a "speech is switched off" diagnosis anybody can read, and
  that trade is worth making once per turn. It costs one thing worth naming: the ``speak.enabled``
  check now runs AFTER the stdin read (the event has to be known first), so a disabled install
  reads the hook payload it used to skip — one read of a string the harness already wrote.
  ONE deliberate silence is left, and it is the EAGER path's: a PostToolUse firing with nothing new
  (or nothing marked) has claimed nothing, its line waits one tool call, and it fires after EVERY
  tool call — a line per firing would drown the drops the log exists to make visible.
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
import io
import itertools
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

# The one module in scripts/ this file imports: the provider registry (see providers.py). It
# resolves because sys.path[0] is this file's directory when the hook calls it — the tests, which
# load this file by spec_from_file_location, put scripts/ on sys.path themselves.
#
# When the imports fail and this file is the process entry point (hooks.json calls python3
# directly), exit silently: the same contract the bash launcher provided — a half-copied scripts/
# directory is silence, not a traceback in the middle of a turn.
try:
    import providers
    import wsclient
except ImportError:
    if __name__ == "__main__":
        sys.exit(0)
    raise

try:
    import fcntl
except ImportError:    fcntl = None  # type: ignore[assignment]


def _kill_process_group(pgid: int, sig: int) -> None:
    """Terminate a command's process group where the platform supports groups."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        killpg(pgid, sig)
    else:
        # Windows has no POSIX process groups; the direct child is the session's
        # process boundary and is terminated by the caller's Popen handle.
        proc = _live.get("proc")
        if proc is not None:
            proc.terminate()

# Retry backoff for the flush race: adaptive, front-loaded — most races resolve within the first
# fraction of a second, so we probe early instead of sleeping a flat 5 x 0.7 s tail.
BACKOFF = (0.15, 0.3, 0.5, 0.7, 1.0)

# What happens where that ladder runs out, on the Stop path only: while an OLDER speaking chain is
# still playing, a line with nothing new behind it is not lost, it is standing in a queue — so keep
# looking. The bound is a COUNT of polls rather than a wall-clock claim: it is the sleep budget a
# test can drive with a fake sleep. PLAYBACK_POLLS * PLAYBACK_POLL is 20 s (two cloud clips' worth),
# and it exists so a wedged player can never hold a turn open.
PLAYBACK_POLL = 0.25
PLAYBACK_POLLS = 80

# What happens where the ladder runs out and the TRANSCRIPT is still being appended to (#106):
# every poll that sees the file advance buys another one, so a long turn's flush is waited out
# rather than dropped. The bound is a COUNT of polls for the same reason PLAYBACK_POLLS is — that
# is the bound a test can drive with a fake sleep — and FLUSH_POLLS * FLUSH_POLL is 12.5 s.
#
# THE THREE SLEEP BUDGETS COMPOSE, in sequence, inside one main(): sum(BACKOFF) 2.65 s +
# PLAYBACK_POLLS * PLAYBACK_POLL 20 s + FLUSH_POLLS * FLUSH_POLL 12.5 s = 35.15 s. This is not a
# wall-clock ceiling: parsing the growing transcript and other work can add time. The structural
# deadline below is checked once per poll against the hook's own timeout budget.
HOOK_BUDGET_S = 90
FLUSH_POLL = 0.25
FLUSH_POLLS = 50

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

# Latin → Cyrillic character mapping for ru/uk Silero voices. An alert composed in English (the
# contour poller's messages are all English) has no Cyrillic characters, so a Russian or Ukrainian
# voice cannot synthesize it — the server returns 400 "no speakable characters". This table maps
# each Latin letter to a visually/auditorily similar Cyrillic letter, producing text the voice CAN
# pronounce. The mapping is character-level (not phonetic): the result reads with an accent, which
# is exactly what makes the alert audible rather than silent.
_LATIN_TO_CYRILLIC: dict[str, str] = {
    "A": "А", "a": "а",
    "B": "Б", "b": "б",
    "C": "К", "c": "к",
    "D": "Д", "d": "д",
    "E": "Е", "e": "е",
    "F": "Ф", "f": "ф",
    "G": "Г", "g": "г",
    "H": "Х", "h": "х",
    "I": "И", "i": "и",
    "J": "Й", "j": "й",
    "K": "К", "k": "к",
    "L": "Л", "l": "л",
    "M": "М", "m": "м",
    "N": "Н", "n": "н",
    "O": "О", "o": "о",
    "P": "П", "p": "п",
    "Q": "К", "q": "к",
    "R": "Р", "r": "р",
    "S": "С", "s": "с",
    "T": "Т", "t": "т",
    "U": "У", "u": "у",
    "V": "В", "v": "в",
    "W": "В", "w": "в",
    "X": "Кс", "x": "кс",
    "Y": "Ы", "y": "ы",
    "Z": "З", "z": "з",
}


def _transliterate_to_cyrillic(text: str) -> str:
    """Transliterate Latin letters to Cyrillic so ru/uk Silero voices can pronounce the text.

    Non-Latin characters (Cyrillic already present, digits, punctuation) pass through unchanged.
    The mapping is character-by-character: "Voice contour" becomes "Воисе контур", which reads
    with an accent but is fully synthesizable by a Russian or Ukrainian voice.
    """
    return "".join(_LATIN_TO_CYRILLIC.get(ch, ch) for ch in text)


_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
_LOG_PATH = os.path.join(_STATE_DIR, "speak.log")
_LAST_PATH = os.path.join(_STATE_DIR, "last-spoken")
# The opaque identity beside last-spoken keeps the dictation echo guard's text contract intact while
# letting eager-off dedup distinguish a repeated line in a new assistant message from a still-flushing
# read of the previous message.
_LAST_KEY_PATH = os.path.join(_STATE_DIR, "last-spoken-key")

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
# The belt-and-suspenders half (windowsill#101): last-spoken is written here and read by
# dictate.py after transcription — a transcript matching it is dropped.
_PID_PATH = os.path.join(_STATE_DIR, "playing.pid")

# The heartbeat: epoch seconds of the last hook INVOCATION, rewritten on every one — even a firing
# that speaks nothing proves the harness still calls the hook, which is the fact a silent session
# needs checked first. GET /health reports its age as hook_last_fired_age_s (cross-process
# contract with server/voice_server.py: one bare float, nothing else).
_STAMP_PATH = os.path.join(_STATE_DIR, "hook-last-fired")

# The contour check (#40): the poller's status file, and the announced-ledger of alert keys
# already voiced (pruned to the alerts still active, so a cleared-and-returned condition pages
# again). contour.json is written by scripts/contour_poll.py — this hook only ever READS it. The
# path is the DEFAULT: contour.status_path relocates it for both halves at once (contour_status_path).
_CONTOUR_PATH = os.path.join(_STATE_DIR, "contour.json")
_CONTOUR_ANNOUNCED_PATH = os.path.join(_STATE_DIR, "contour-announced")

# How old a status file may be before it stops being evidence about the contour and becomes
# evidence that nobody is polling. The poller writes its own bound into the file (contour.max_age)
# and that wins; this is the fallback for a file written before the bound existed.
CONTOUR_MAX_AGE = 900

# --- streaming TTS over a resident ElevenLabs websocket (windowsill#113) -------------------------
#
# The voice-back counterpart of #99's streaming dictation. A cloud TTS provider with a streaming
# variant (only ElevenLabs today — see providers.TtsStreaming) is reached over a websocket a
# RESIDENT holder keeps open ACROSS TURNS: the TLS+WS dial is ~300-420 ms (more than the synthesis
# itself, per the live probe), so paying it once per session roughly halves first-sound, and a held
# socket renders at ~170 ms warm with ZERO latency penalty. The holder is a lazily-spawned daemon
# tracked by pidfile (the speak-path mirror of #99's stream-worker), spoken to over a unix-domain
# socket that carries the SAME SSE chunk protocol as the server's /tts/stream — so play_text reuses
# its stream_source/_play_stream verbatim. Every number here bounds something otherwise unbounded.

# The argv the holder daemon is spawned with — this script calling itself, one file, one launcher.
STREAM_HOLDER_ARG = "stream-holder"
# A vendor closes an idle stream-input socket at ~20 s (WS 1008); a whitespace keepalive every
# few seconds holds it open between turns without producing audio (the frame carries no flush).
STREAM_KEEPALIVE_SECONDS = 15.0
# How long a poll waits for the next audio fragment when none has arrived — the loop's idle cost.
STREAM_IDLE_POLL = 0.05
# The socket must open fast or not at all — this feature exists to REMOVE a wait.
STREAM_CONNECT_TIMEOUT = 5.0
# A line's synthesis must finish or be abandoned (and degraded): a held socket that stalls cannot
# hold a turn open forever. Per the probe, a flushed fragment is +174-212 ms; this is the ceiling.
STREAM_LINE_TIMEOUT = 30.0
# The throwaway priming synthesis (sent once on connect so the session's first REAL line is warm)
# is bounded the same way a real line is. Best-effort: a prime that fails costs the first line its
# warmth, nothing more — the holder serves regardless.
STREAM_PRIME_TIMEOUT = 15.0
# A self-cleaning holder: with no request for this long it exits, so a session that ended leaves
# nothing running. The next streaming turn spawns a fresh one (and re-primes).
STREAM_HOLDER_IDLE_EXIT = 300.0
# How long a turn waits for a just-spawned holder to bind its socket before using the blob path
# for that one turn only — the holder that would not start must not stall a turn.
STREAM_HOLDER_READY_TIMEOUT = 6.0

# The resident holder's two files: its pidfile (how a new turn tells a live holder from a stale
# one) and its unix-domain socket (the per-turn request channel). The pidfile carries a DIGEST of
# the synthesis settings the holder was started with, so a settings edit (e.g. voice_settings.speed)
# is a RECONNECT TRIGGER: a mismatched digest respawns the holder with the new settings, because a
# voice_settings change on a held socket may require reopening the stream.
_STREAM_HOLDER_PID = os.path.join(_STATE_DIR, "speak-stream.pid")
_STREAM_HOLDER_SOCK = os.path.join(_STATE_DIR, "speak-stream.sock")

# The event this invocation was fired for, recorded by main() the moment stdin is read so the
# contour check can tell which path it is on. A module cell rather than a return value: main()'s
# return IS the process exit code, and hooks.json reads that.
_fired: dict = {"event": "Stop"}
_stop_reason_logged = False

# state the SIGTERM handler (takeover by a fresher invocation) must be able to reach:
# the current player child, the temp WAVs on disk, and the open SSE response (its socket
# must close mid-stream on takeover, not linger until the server finishes synthesizing)
_live: dict = {"proc": None, "files": set(), "stream": None}
# The exact record this process last installed. Comparing the full record prevents an older
# invocation from unlinking a newer invocation's pidfile between its read and unlink.
_pidfile_record: str | None = None


def log(message: str) -> None:
    global _stop_reason_logged
    if _fired["event"] == "Stop" and message.startswith("stop:"):
        _stop_reason_logged = True
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


def _write_last_key(key: str) -> None:
    """Persist the opaque eager-off identity atomically; a reader sees the old key or the new one."""
    try:
        fd, tmp = tempfile.mkstemp(prefix="voice-loop-last-key-", dir=os.path.dirname(_LAST_KEY_PATH))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _LAST_KEY_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, UnboundLocalError):
            pass


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


def resolve_tts_provider(name: str):
    """The registry entry for a configured provider name — the default entry, loudly, for a name
    the registry has never heard of. The counterpart of dictate.resolve_stt_provider."""
    entry = providers.tts_provider(name)
    if entry is None:
        log(f'tts.cloud.provider is not a known provider — using "{providers.DEFAULT_TTS}" instead of {name!r}')
        entry = providers.TTS_PROVIDERS[providers.DEFAULT_TTS]
    return entry


def _default_player(system: str) -> str:
    """The player a config that names none resolves to, per platform.

    macOS plays WAVs through ``afplay``; Linux through ``aplay -q``. Windows has neither — the
    POSIX default made every Windows hook silently play nothing, because the "never fail a turn"
    contract swallows the missing-command error. The in-box equivalent is PowerShell driving
    ``System.Media.SoundPlayer``, which plays a WAV synchronously with nothing extra installed.
    ``{file}`` is the placeholder ``_player_argv`` fills with the temp WAV path, because shlex
    (POSIX) must not re-split a Windows path inside the ``-Command`` script."""
    if system == "Windows":
        return "powershell.exe -NoProfile -Command \"(New-Object System.Media.SoundPlayer '{file}').PlaySync()\""
    return "afplay" if system == "Darwin" else "aplay -q"


def resolve_settings(config: dict, system: str) -> dict:
    """Every knob speak.sh honoured, same names, same defaults, same precedence."""
    speaker = str(cfg(config, "tts.speaker", ""))
    # the provider is an ENTRY, never a branch — every per-provider default below comes off it
    entry = resolve_tts_provider(str(cfg(config, "tts.cloud.provider", providers.DEFAULT_TTS)))
    voice_settings = cfg(config, "tts.cloud.voice_settings", None)
    return {
        "enabled": cfg(config, "speak.enabled", True) not in (False, "false"),
        # eager is opt-in and OFF by default: with it off the PostToolUse registration is a no-op
        # that costs one stdin read per tool call and never touches the transcript.
        "eager": cfg(config, "speak.eager", False) not in (False, "false"),
        "marker": str(cfg(config, "speak.marker", "🔊")),
        "player": str(cfg(config, "speak.player", _default_player(system))),
        # PipeWire sink to route TTS audio into for echo cancellation — when set, the player targets
        # this sink (via pw-play --target or aplay -D pipewire:<sink>) instead of the default device.
        "sink": str(cfg(config, "speak.sink", "")),
        "max_chars": int(cfg(config, "speak.max_chars", 600)),
        "timeout": float(cfg(config, "speak.timeout", 60)),
        "backend": str(cfg(config, "tts.backend", "lan")),
        # left empty here: the per-backend default differs (see synthesize) — the LAN server and a
        # provider with no remote default host both land on the local speech server, while a
        # provider that HAS one (ElevenLabs, Deepgram) lands on its own remote API host. That choice
        # is the entry's.
        "endpoint": str(cfg(config, "tts.endpoint", "")),
        "speaker": speaker,
        # top-level "language" is the one the user sets; ".tts.language" is the advanced escape for
        # people who dictate in one language and listen in another.
        "language": str(cfg(config, "tts.language", cfg(config, "language", "en"))),
        "command": str(cfg(config, "tts.command", "")),
        "provider": entry.name,
        "voice_id": str(cfg(config, "tts.cloud.voice_id", speaker)),
        "cloud_model": str(cfg(config, "tts.cloud.model", entry.default_model)),
        # the audio CONTAINER, and its spelling is provider-private: one opaque token for
        # ElevenLabs, a pair of query parameters for Deepgram — so the default rides the entry
        "output_format": str(cfg(config, "tts.cloud.output_format", entry.default_output_format)),
        "key_env": str(cfg(config, "tts.cloud.api_key_env", cfg(config, "tts.api_key_env", "VOICE_LOOP_TTS_API_KEY"))),
        "key_file": str(cfg(config, "tts.cloud.key_file", "")),
        # provider-specific synthesis knobs, passed through verbatim (ElevenLabs: stability,
        # similarity_boost, style, use_speaker_boost — see the anti-robovoice notes in voice-design)
        "voice_settings": voice_settings if isinstance(voice_settings, dict) else None,
        # streaming TTS over the resident websocket (windowsill#113): opt-in, OFF by default. The
        # batch blob path is the proven one; streaming is the latency win for a cloud voice that
        # carries a streaming variant (only ElevenLabs today).
        "streaming": cfg(config, "tts.cloud.streaming", False) is True
        or cfg(config, "tts.cloud.streaming", False) == "true",
        # voice_settings.speed — ElevenLabs accepts ~0.7-1.2; the operator's contour runs ~0.9.
        # Read off the voice_settings object the same place the other knobs live, default 1.0 (no
        # change). Folded into voice_settings at BOS time so it rides the held socket's first frame.
        "speed": float(cfg(config, "tts.cloud.voice_settings.speed", 1.0)),
        # the audio container the STREAMING path asks for — raw s16le (pcm_22050) by default, so the
        # holder wraps it in a WAV and the player queue needs no decoder. Distinct from the batch
        # output_format (mp3), because a streaming socket sends samples, not a compressed blob.
        "stream_output_format": str(
            cfg(
                config,
                "tts.cloud.stream_output_format",
                entry.streaming.default_output_format if entry.streaming else "",
            )
        ),
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
    speaking proceeds unserialized rather than going silent. Closing it is a no-op.

    ``reason`` says WHICH unavailable lock this was, so a caller that goes on to speak can log
    the degrade once — attached to a turn that actually said something, never to a firing that
    found nothing new (which fires on every tool call and owes the log silence)."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

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
        return _NoLock(reason=f"the lockfile {_LOCK_PATH} could not be opened")
    if fcntl is None:
        # Windows lacks flock; do not mute speech solely for that unsupported advisory
        # primitive. Speech proceeds without inter-process locking, and two speakers can now
        # overlap — a fact the turn that speaks owes the log, logged where it is spoken.
        fh.close()
        return _NoLock(reason="speaking lock is unsupported on this platform — proceeding unlocked; speech may overlap")
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


def _is_loopback_host(host: str) -> bool:
    """True only for addresses that point at the local machine — the one case where plaintext is
    safe (the bundled server on 127.0.0.1 is the default and must never warn)."""
    return host == "localhost" or host == "::1" or host.startswith("127.")


def synthesize(text: str, s: dict, key: str) -> bytes | None:
    """One chunk -> audio bytes, or None (with the reason logged). Mirrors speak.sh's checks:
    empty body and JSON-error-document responses are dropped, not played.

    There is NO per-provider branch here and there must never be one: the cloud request is built by
    the configured provider's registry entry (see providers.py), which owns the host, the path, the
    auth header, the payload shape and the audio container. The one branch left is the one that is
    NOT about providers — cloud versus this plugin's own server.
    """
    if s["backend"] == "cloud":
        entry = resolve_tts_provider(s["provider"])
        try:
            request = entry.request(s, key, text)
        except ValueError as err:
            # a builder that refuses a misconfiguration (an unset ElevenLabs voice) — say why and
            # stop, rather than turn it into a request that fails opaquely.
            log(f"cloud tts misconfigured: {err}")
            return None
        endpoint = entry.endpoint(s)  # for the error messages below, which name where it went
        parsed = urllib.parse.urlsplit(request.url)
        if parsed.scheme == "http" and parsed.hostname and not _is_loopback_host(parsed.hostname):
            log(
                f"cloud tts endpoint is http:// to {parsed.hostname} — "
                "the API key and the text travel in the clear"
            )
        body = _post(request.url, request.headers, request.payload, s["timeout"])
    else:
        endpoint = s["endpoint"] or providers.LOCAL_SPEECH_HOST
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


def _atomic_write_text(path: str, content: str, prefix: str) -> None:
    """Replace a shared state file without exposing a partial document to another process."""
    directory = os.path.dirname(path) or "."
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=prefix, dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _write_pidfile(*tokens) -> None:
    """Write the speaking-chain identity to ``playing.pid`` atomically. Tokens are space-joined;
    the first is always our own PID. An optional ``"pg"`` marker before a child PID means the child
    was spawned in its own process group (``tts.command``) — ``take_over`` never signals those
    directly; the chain's ``_on_sigterm`` handler uses ``killpg`` instead."""
    global _pidfile_record
    record = " ".join(str(t) for t in tokens)
    _atomic_write_text(_PID_PATH, record, "voice-loop-playing-")
    _pidfile_record = record


def _owns_pidfile() -> bool:
    """True only if the file still contains the exact record this process installed."""
    if _pidfile_record is None:
        return False
    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            return fh.read() == _pidfile_record
    except OSError:
        return False


def _clear_pidfile() -> None:
    """Drop playing.pid only when its recorded owner is this process.

    The ownership check is deliberately before unlinking: a stale invocation must not clear a newer
    chain's state after PID reuse or after a takeover has installed a replacement record."""
    global _pidfile_record
    if not _owns_pidfile():
        return
    try:
        os.unlink(_PID_PATH)
    except OSError:
        pass
    finally:
        _pidfile_record = None


def _cmdline_of(pid: int) -> str | None:
    """Linux ``/proc/<pid>/cmdline`` with NULs as spaces — None when unreadable.
    Duplicated helper — keep in sync with dictate.py."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _ps_cmdline_of(pid: int) -> str | None:
    """Read a macOS command line without signalling a process or invoking a shell."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip() or None


def pid_looks_like_speak(pid: int, read_cmdline=_cmdline_of, platform_id: str = sys.platform) -> bool:
    """PID-reuse guard: only a process whose command line identifies this speaking chain is trusted.

    Linux uses ``/proc`` and macOS uses its bounded ``ps`` query. Other platforms have no verified
    identity path here, so they fail closed rather than turning a stale pidfile into a signal for an
    unrelated process."""
    if platform_id.startswith("linux"):
        cmdline = read_cmdline(pid)
    elif platform_id == "darwin":
        cmdline = _ps_cmdline_of(pid) if read_cmdline is _cmdline_of else read_cmdline(pid)
    else:
        return False
    if cmdline is None:
        return False
    return "voice-loop-speak" in cmdline or "speak.py" in cmdline


def _recorded_pids() -> list[int]:
    """The speaking chain PIDs that THIS process may safely SIGTERM — the previous chain's python
    process and its player children.  ``"pg"``-marked children (``tts.command`` process groups) are
    EXCLUDED: they are terminated by the old chain's ``_on_sigterm`` handler via ``killpg``, never
    targeted individually.  One reader for both users of this file — ``take_over`` and
    ``playback_is_live`` — so neither can drift into seeing it differently."""
    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            tokens = fh.read().split()
    except OSError:
        return []
    pids: list[int] = []
    skip_next = False
    for tok in tokens:
        if tok == "pg":
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        try:
            pids.append(int(tok))
        except ValueError:
            continue
    return [pid for pid in pids if pid and pid != os.getpid()]


def _recorded_pgids() -> list[int]:
    """Process-group leader PIDs that the ``tts.command`` path recorded — process-group children
    whose liveness ``playback_is_live`` checks but which ``take_over`` never signals directly
    (the old chain's ``_on_sigterm`` handler uses ``killpg`` on its own children when the chain's
    python process receives SIGTERM)."""
    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            tokens = fh.read().split()
    except OSError:
        return []
    pgids: list[int] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "pg" and i + 1 < len(tokens):
            try:
                pgids.append(int(tokens[i + 1]))
            except ValueError:
                pass
            i += 2
        else:
            i += 1
    return [pgid for pgid in pgids if pgid and pgid != os.getpid()]


def take_over() -> None:
    """A fresher line supersedes a still-playing older one: SIGTERM exactly the PIDs the previous
    chain recorded (its python process + its current player child) — nothing else — and only
    after each pid passes the PID-reuse guard above.  Process-group children (``tts.command``,
    marked ``"pg"`` in the pidfile) are NOT signalled here: the chain's python parent receives
    SIGTERM and its ``_on_sigterm`` handler uses ``killpg`` on its own children, which reaches the
    player inside the shell regardless of ``exec`` or temp-file naming.  Same semantics as
    dictate.py's echo guard (cross-script contract)."""
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
    ours to signal, or that the kernel has since handed to somebody else is not one to wait for.

    Process-group children (``tts.command``) are checked by pg-leader existence alone — no identity
    guard is needed because the ``"pg"`` marker in the pidfile IS the identity record."""
    for pid in _recorded_pids():
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        if pid_looks_like_speak(pid):
            return True
    for pgid in _recorded_pgids():
        try:
            os.kill(pgid, 0)
        except OSError:
            continue
        return True
    return False


def wait_out_playback(read, settled, text=None, *, deadline=None):
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
        if deadline is not None and time.monotonic() >= deadline:
            log("stop: hook deadline reached while waiting for playback")
            return text
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


def transcript_activity(path: str, stat=os.stat):
    """``(size, mtime_ns)`` of the transcript, or None when it cannot be stat'ed.

    The cheapest possible answer to "is somebody still writing this file?" — no open, no read, no
    parse, one syscall. Both halves are needed: a rewrite that keeps the length identical still
    moves mtime, and a write inside one clock granularity still moves the size.

    None is not "idle" in itself — it is one more value that compares equal to the previous None,
    so a transcript that cannot be stat'ed at all stops the wait immediately, which is exactly the
    verdict the pre-#106 code reached without looking."""
    try:
        st = stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def wait_out_flush(path, read, settled, text, mark, *, stat=os.stat, deadline=None):
    """Keep re-reading the transcript for as long as the transcript is still GROWING.

    The second half of #106, and the sibling of wait_out_playback above: same entry point (the
    ladder ran out with nothing new), same one-way effect (it can only turn a dropped line into a
    late one), different evidence. wait_out_playback waits on somebody else's clip still being in
    the air; this waits on our own turn's message still being WRITTEN — the case that produced the
    live silence, where nothing was playing at all and the fixed 2.65 s ladder simply ended before
    a long turn's flush did.

    ``mark`` is the activity reading taken BEFORE the ladder ran, so growth during the ladder is
    already visible on the first stat and costs no sleep to observe. Each poll that sees the file
    advance buys one more poll; the first that does not ends the wait, so an idle transcript
    (the overwhelming common case: a turn with genuinely nothing marked in it) is answered by one
    stat and nothing else. FLUSH_POLLS caps the whole thing however busy the file stays.

    Returns the first settled read, or the freshest unsettled one — the caller's give-up to log."""
    polls = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            log("stop: hook deadline reached while waiting for transcript")
            return text
        current = transcript_activity(path, stat)
        if current == mark:
            if polls:
                log(f"stop: the transcript stopped growing after {polls * FLUSH_POLL:.2f}s — nothing new arrived")
            return text
        mark = current
        if polls >= FLUSH_POLLS:
            log(f"stop: the transcript is still growing after {polls * FLUSH_POLL:.1f}s — waiting no longer")
            return text
        time.sleep(FLUSH_POLL)
        polls += 1
        text = read()
        if settled(text):
            waited = polls * FLUSH_POLL
            # settled() is true for '' as well: a message that parsed and marked NOTHING is a final
            # answer too, and it arrives here exactly like a line does. Saying "the line landed"
            # over it would put a line in the log that was never in the transcript.
            landed = "the line" if text else "a message with nothing marked"
            log(f"stop: the transcript was still being written — {landed} landed {waited:.2f}s past the ladder")
            return text


# --- the contour check (#40): the poller writes the file, this hook is the page --------------------


def _utcnow() -> datetime:
    """Wall-clock UTC, at one seam — the staleness bound is the only thing in this file that needs
    it, and a test freezes it here rather than sleeping."""
    return datetime.now(timezone.utc)


def contour_status_path(config: dict) -> str:
    """Where the poller was told to write, read the SAME way the poller resolves it.

    The seam this closes: contour_poll.py can be pointed anywhere with ``--status`` while this
    hook only ever read the default, so a cron line written with ``--status /var/tmp/contour.json``
    polled correctly, exited 1 correctly, and paged nobody. One config key answers for both halves
    (contour_poll.resolve_status_path is the same lookup), and the default is unchanged.
    """
    return str(cfg(config, "contour.status_path", _CONTOUR_PATH))


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
    alerts = contour_alerts(read_contour_status(contour_status_path(config)), _utcnow())

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
            if alerts:
                # The dedup's own positive mark. Without it the second firing of a persisting alert
                # left NO trace, so "the page did not repeat" was indistinguishable from "the hook
                # crashed before it ever looked" — and speak.sh swallows every exception and
                # exits 0, so crashing is exactly what a regression here looks like. Only logged
                # when there is something to be quiet ABOUT: the empty case is every quiet turn.
                log(f"contour: already announced — nothing to voice ({len(alerts)} alert(s) still active)")
            return
        text = f"Voice contour: {'; '.join(alert['message'] for alert in fresh)}"
        # A non-Latin voice cannot synthesize an all-Latin alert: the server returns 400
        # "no speakable characters". Transliterate to Cyrillic so the page is audible on
        # ru/uk deployments — a silent page is a monitor with no output path.
        if s["language"] in ("ru", "uk"):
            text = _transliterate_to_cyrillic(text)
        log(f"contour: voicing {len(fresh)} alert(s): {text[:80]}")
        signal.signal(signal.SIGTERM, _on_sigterm)
        if not s["eager"]:
            # With eager off this runs on Stop, after the turn's own line has finished: the alert
            # supersedes a chain still playing exactly like a fresher turn's line would.
            take_over()
        _write_pidfile(os.getpid())
        if isinstance(lock, _NoLock) and lock.reason:
            log(lock.reason)
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
    # Process-group children (tts.command): signal the entire group so the player
    # inside the shell receives the signal regardless of exec or wrapper depth.
    pgid = _live.get("pgid")
    if pgid is not None:
        try:
            _kill_process_group(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
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


def _player_argv(player: str, wav: str) -> list[str]:
    """The argv that plays ``wav``. A ``{file}`` placeholder in the player string is replaced with
    the WAV path BEFORE it is treated as one argv element — the Windows PowerShell player embeds the
    path inside a ``-Command`` script, and splitting a substituted Windows path (backslashes) would
    corrupt it. Without a placeholder the WAV is appended as the last argument, exactly as before."""
    if "{file}" in player:
        return [part.replace("{file}", wav) for part in shlex.split(player)]
    return shlex.split(player) + [wav]


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
    player_template = s["player"]
    # Acoustic echo cancellation: when speak.sink names a PipeWire sink, route audio into it so the
    # echo canceller can subtract it from the mic signal. pw-play --target does this natively; aplay
    # reaches it via the pipewire ALSA plugin with -D pipewire:<node>. A sink is a PipeWire knob, so
    # it never combines with a {file} player template (the Windows player).
    _player_env: dict | None = None
    sink_prefix: list[str] | None = None
    if s.get("sink"):
        if shutil.which("pw-play"):
            sink_prefix = ["pw-play", "--target=" + s["sink"]]
        else:
            sink_prefix = shlex.split(player_template) + ["-D", "pipewire:" + s["sink"]]
    proc: subprocess.Popen | None = None
    proc_wav: str | None = None
    proc_audio_len = 0
    proc_started_at: float | None = None
    played = 0
    total_bytes = 0
    first_audio_at: float | None = None
    rc: int | None = None

    def reap() -> None:
        nonlocal proc, proc_wav, proc_audio_len, proc_started_at, rc, played, total_bytes, first_audio_at
        if proc is not None:
            rc = proc.wait()
            if rc == 0:
                played += 1
                total_bytes += proc_audio_len
                if first_audio_at is None:
                    first_audio_at = proc_started_at
            proc = None
            proc_audio_len = 0
            proc_started_at = None
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
            proc_started_at = time.monotonic()  # delivery is confirmed only after a zero exit
            try:
                proc = subprocess.Popen(
                    sink_prefix + [wav] if sink_prefix is not None else _player_argv(player_template, wav),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
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
            proc_audio_len = len(audio)
            _write_pidfile(os.getpid(), proc.pid)
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
        #
        # Spawned in its own session (start_new_session=True) so the shell + its player children
        # form a process group.  The pidfile records a "pg" marker before the child PID:
        # take_over never signals it directly (no cmdline guessing needed — the pg marker IS the
        # identity), and _on_sigterm uses killpg on the group so the player inside the shell
        # receives the signal regardless of whether the shim exec's or names temp files
        # "voice-loop-speak*".  windowsill#152.
        first_ms = int((time.monotonic() - t0) * 1000)
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", s["command"]],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as err:
            log(f"local command failed: {err}")
            return False
        _live["proc"] = proc
        _live["pgid"] = proc.pid  # session leader -> process group leader
        _write_pidfile(os.getpid(), "pg", proc.pid)
        proc.communicate(input=text.encode("utf-8"))
        _live["proc"] = None
        _live.pop("pgid", None)
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
    cloud_stream_degraded = False
    if s["backend"] == "cloud" and cloud_streaming_wanted(s):
        # Cloud streaming over the resident ElevenLabs websocket (windowsill#113): the holder emits
        # the SAME SSE chunk protocol as the server's /tts/stream, so the chunk reader is reused
        # verbatim. A holder that could not be readied falls through to the blob path below; one
        # that DEGRADED the line (quota/auth, a mid-line hang-up) is flagged so the blob fallback
        # speaks the LOCAL voice rather than re-hit the cloud — never silence.
        conn = _connect_stream_holder(text, s)
        if conn is not None:
            _live["stream"] = conn
            try:
                source = stream_source(conn.makefile("rb"))
                if source is not None:
                    result = _play_stream(source, s, t0)
                    via = "stream-cloud"
                else:
                    cloud_stream_degraded = True
                    log("cloud stream degraded before the first chunk — falling back to the local voice")
            finally:
                _live["stream"] = None
                try:
                    conn.close()
                except OSError:
                    pass
    if result is None and s["backend"] != "cloud":
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
        if cloud_stream_degraded:
            # The held socket degraded this line — often quota/auth, which the cloud BATCH path
            # would hit too. Speak the line on the LOCAL server instead: the cloud's loss is the
            # bundled Silero voice's turn, never silence (windowsill#113). tts.command, when set,
            # took the turn above and never reached here.
            local = dict(s)
            local["backend"] = "lan"
            result = _play_stream(_synthesized_audio(chunk_sentences(text), local, ""), s, t0)
            via = "stream-cloud-degraded"
        else:
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


def stream_settings_digest(s: dict) -> str:
    """A hash of the settings the holder bakes into its held socket — the ones a change to MUST
    respawn it (provider, voice, model, output format, voice settings incl. speed, endpoint). The
    KEY is deliberately not in it: the holder reads the key itself, and a rotated key is no reason
    to drop a warm socket."""
    parts = {
        "provider": s["provider"],
        "voice_id": s["voice_id"],
        "cloud_model": s["cloud_model"],
        "stream_output_format": s["stream_output_format"],
        "voice_settings": s["voice_settings"],
        "speed": s["speed"],
        "endpoint": s["endpoint"],
    }
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def stream_pcm_rate(output_format: str) -> int:
    """The sample rate hidden in an ElevenLabs ``pcm_<rate>`` token (pcm_22050 -> 22050), defaulting
    to 22050 for anything that does not name one. The holder wraps the raw samples in a WAV at THIS
    rate, so a silent upstream format change cannot pitch every utterance."""
    match = re.match(r"pcm_(\d+)", output_format)
    return int(match.group(1)) if match else 22050


def pcm_to_wav(pcm: bytes, sample_rate: int = 22050, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw s16le PCM in a minimal WAV container — the player queue plays WAVs, and a 44-byte
    header is all that separates raw pcm_22050 from something afplay and aplay both accept. No
    re-encoding: the samples are copied verbatim (stdlib ``wave``), which is what keeps the
    streaming path decoder-free."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def sse_event(name: str, data: dict) -> bytes:
    """One SSE frame in the strict shape parse_sse reads: ``event: <name>`` then ``data: <one JSON
    line>`` then a blank line. The holder speaks the SAME protocol as the server's /tts/stream so
    play_text's chunk reader is reused verbatim on either source."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


class TtsStreamClosed(Exception):
    """A held websocket could not finish a line — the vendor hung up (its ~20 s idle close, a quota
    close), the line outran its deadline, or the read failed. The caller degrades that line to the
    local path; the holder drops the socket and reconnects on the next line."""


class TtsStreamHolder:
    """One resident stream-input websocket held open across lines: prime on connect, synthesize a
    line on demand, keep an idle socket alive past the vendor's idle close, reconnect after the
    vendor hangs up. Every socket and time seam is injected so the whole holder is exercisable
    against a fake provider on a real loopback socket with no network — the voice-back mirror of
    dictate.run_stream_session's testability."""

    def __init__(
        self, entry, s, key, *, connect=wsclient.connect, clock=time.monotonic,
        sleep=time.sleep, connect_timeout=STREAM_CONNECT_TIMEOUT,
    ) -> None:
        self.streaming = entry.streaming
        self.entry = entry
        self.s = s
        self.key = key
        self._connect = connect
        self._clock = clock
        self._sleep = sleep
        self._connect_timeout = connect_timeout
        self._ws = None
        self._last_send: float | None = None  # clock of the last frame SENT — the keepalive-due clock

    def ensure_open(self) -> None:
        """Connect and send the begin-of-stream frame if the socket is not live; a no-op when it is.
        The BOS IS the priming frame: it goes out the moment the socket opens, so the session's
        first real line is not the one paying connect+init. Reconnects after a vendor close."""
        if self._ws is not None and not self._ws.closed:
            return
        url = self.streaming.url(self.streaming, self.entry, self.s)
        self._ws = self._connect(url, self.streaming.headers(self.key), timeout=self._connect_timeout)
        self._ws.send_text(self.streaming.bos(self.s))
        self._last_send = self._clock()

    def prime(self, text: str = " ") -> None:
        """Send a throwaway text+flush and drain its audio, so the session's first REAL line is warm
        (the probe measured a 2247 ms cold first call). Best-effort: a prime that fails only costs
        the first line its warmth — the holder serves regardless, and the next line reconnects."""
        self.ensure_open()
        self._ws.send_text(self.streaming.text_message(text))
        self._ws.send_text(self.streaming.flush_message)
        self._last_send = self._clock()
        try:
            for _ in self._drain_until_final(self._clock() + STREAM_PRIME_TIMEOUT):
                pass
        except (TtsStreamClosed, wsclient.WebSocketError, OSError) as err:
            log(f"stream holder priming failed (non-fatal): {err}")
            self._ws = None

    def synthesize_line(self, text: str, *, deadline: float) -> Iterator[bytes]:
        """Send text+flush on the held socket and yield raw PCM fragments until the final marker.
        Raises TtsStreamClosed when the vendor hung up or the line outran its deadline — the caller
        degrades the line, and the next line reconnects (ensure_open sees the dropped socket)."""
        self.ensure_open()
        self._ws.send_text(self.streaming.text_message(text))
        self._ws.send_text(self.streaming.flush_message)
        self._last_send = self._clock()
        yield from self._drain_until_final(deadline)

    def _drain_until_final(self, deadline: float) -> Iterator[bytes]:
        ws = self._ws
        while self._clock() < deadline:
            closed = False
            for opcode, payload in ws.poll(STREAM_IDLE_POLL):
                if opcode == wsclient.OP_CLOSE:
                    closed = True
                    continue
                if opcode != wsclient.OP_TEXT:
                    continue
                fragment = self.streaming.result(providers.decode(payload))
                if fragment is None:
                    continue
                if fragment.audio_b64:
                    try:
                        yield base64.b64decode(fragment.audio_b64)
                    except (ValueError, TypeError):
                        continue  # a bad fragment is skipped, not a failed line
                if fragment.is_final:
                    return
            if closed:
                self._ws = None
                raise TtsStreamClosed("the server closed the stream before the final fragment")
        self._ws = None
        raise TtsStreamClosed("a line's synthesis outran its deadline")

    def keepalive_if_due(self) -> None:
        """Send a whitespace keepalive when the socket has been idle past the interval — the frame
        carries no flush, so it produces no audio, and it resets the vendor's ~20 s idle close."""
        if self._ws is None or self._ws.closed or self._last_send is None:
            return
        if self._clock() - self._last_send < STREAM_KEEPALIVE_SECONDS:
            return
        try:
            self._ws.send_text(self.streaming.keepalive_message)
            self._last_send = self._clock()
        except wsclient.WebSocketError:
            self._ws = None  # a dead socket; reconnect on the next ensure_open

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except OSError:
                pass
            self._ws = None


# --- the holder daemon: pidfile lifecycle + the accept loop -------------------------------------


def _write_stream_holder_pid(pid: int, digest: str) -> None:
    try:
        with open(_STREAM_HOLDER_PID, "w", encoding="utf-8") as fh:
            fh.write(f"{pid} {digest}")
    except OSError:
        pass


def _read_stream_holder_pid() -> tuple[int | None, str]:
    """(pid, digest) the holder recorded, or (None, '') when there is no readable pidfile. The
    digest fences staleness: a holder for DIFFERENT settings is stale by construction."""
    try:
        with open(_STREAM_HOLDER_PID, encoding="utf-8") as fh:
            parts = fh.read().split()
    except OSError:
        return None, ""
    if len(parts) >= 2 and parts[0].isdigit():
        return int(parts[0]), parts[1]
    return None, ""


def _clear_stream_holder_pid() -> None:
    try:
        os.unlink(_STREAM_HOLDER_PID)
    except OSError:
        pass


def _remove_socket_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def pid_looks_like_stream_holder(pid: int, read_cmdline=_cmdline_of, platform_id: str = sys.platform) -> bool:
    """PID-reuse guard for the holder — the same shape as the speak-chain and stream-worker ones: a
    pidfile outlives its process and the kernel recycles PIDs, so before trusting a recorded pid,
    confirm it still looks like the holder (its argv carries this script's name and the stream-holder
    word, which nothing else on the machine does)."""
    if not platform_id.startswith("linux"):
        return True
    cmdline = read_cmdline(pid)
    if cmdline is None:
        return False
    return "speak.py" in cmdline and STREAM_HOLDER_ARG in cmdline


def cloud_streaming_wanted(s: dict) -> bool:
    """True when the cloud path should try the resident websocket: streaming opted in AND the
    configured provider's entry actually carries a streaming variant. A config that asks streaming
    of a provider that has none is answered by the blob path — never an error."""
    if not s["streaming"]:
        return False
    return resolve_tts_provider(s["provider"]).streaming is not None


def ensure_stream_holder(
    s: dict, *, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic,
) -> bool:
    """Make sure a resident holder matching THESE settings is running and bound. True when one is (or
    was just made) live; False when one could not be readied in time (the caller uses the blob path
    for this one turn).

    The digest is the reconnect trigger: a holder started with different settings (a speed edit, a
    new voice) is stale by construction, stopped, and replaced. The speaking lock serializes turns,
    so two spawns never race for one socket."""
    digest = stream_settings_digest(s)
    pid, pid_digest = _read_stream_holder_pid()
    if pid is not None and pid_looks_like_stream_holder(pid) and pid_digest == digest:
        return True  # a warm socket for exactly these settings
    if pid is not None and pid_looks_like_stream_holder(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    _clear_stream_holder_pid()
    _remove_socket_file(_STREAM_HOLDER_SOCK)
    try:
        popen(
            [sys.executable, os.path.abspath(__file__), STREAM_HOLDER_ARG, digest],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as err:
        log(f"stream holder did not start: {err} — this turn uses the blob path")
        return False
    # the holder writes its own pidfile on startup; wait for it (matching digest) AND the socket.
    deadline = clock() + STREAM_HOLDER_READY_TIMEOUT
    while clock() < deadline:
        got_pid, got_digest = _read_stream_holder_pid()
        if got_digest == digest and got_pid is not None and os.path.exists(_STREAM_HOLDER_SOCK):
            return True
        sleep(0.05)
    log("stream holder did not bind its socket in time — this turn uses the blob path")
    return False


def _connect_stream_holder(text: str, s: dict):
    """Connect to the resident holder, send the request, half-close the write side, and return the
    open connection (whose read side plays back as SSE). None when the holder could not be reached
    so play_text uses the blob path for this one turn."""
    if not hasattr(socket, "AF_UNIX") or not ensure_stream_holder(s):
        # The platform guard is FIRST: it is a constant, and putting it left of the
        # `or` keeps Windows out of ensure_stream_holder entirely — that path ends in
        # _bind_unix_listener, which has no implementation there. The resident holder
        # is an optional latency path; the blob endpoint remains the contract.
        return None
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(s["timeout"])
        conn.connect(_STREAM_HOLDER_SOCK)
        conn.sendall((json.dumps({"text": text}) + "\n").encode("utf-8"))
        conn.shutdown(socket.SHUT_WR)
    except OSError as err:
        log(f"cloud stream: could not reach the holder: {err} — using the blob path")
        try:
            conn.close()
        except OSError:
            pass
        return None
    return conn


def _send_all(conn, data: bytes) -> None:
    try:
        conn.sendall(data)
    except OSError:
        pass  # the client gone (a takeover, a closed turn) — nothing more to send


def _serve_stream_connection(conn, holder, s, *, clock=time.monotonic) -> None:
    """One per-turn connection: read the request, synthesize it chunk by chunk, emit the SAME SSE
    stream the server's /tts/stream does. Degrade is an ``error`` event (never silence): the client
    falls back to the blob path, and the holder stays up for the next turn."""
    reader = conn.makefile("rb")
    try:
        line = reader.readline()
    finally:
        reader.close()
    try:
        request = json.loads(line.decode("utf-8")) if line else {}
    except ValueError:
        request = {}
    text = str(request.get("text", "")).strip() if isinstance(request, dict) else ""
    if not text:
        _send_all(conn, sse_event("end", {"chunks": 0, "engine": s["provider"]}))
        return
    rate = stream_pcm_rate(s.get("stream_output_format", ""))
    sent = 0
    try:
        for chunk_text in chunk_sentences(text) or [text]:
            pcm = bytearray()
            for fragment in holder.synthesize_line(chunk_text, deadline=clock() + STREAM_LINE_TIMEOUT):
                pcm.extend(fragment)
            _send_all(conn, sse_event("chunk", {"audio": base64.b64encode(pcm_to_wav(bytes(pcm), rate)).decode("ascii")}))
            sent += 1
    except (TtsStreamClosed, wsclient.WebSocketError, OSError) as err:
        log(f"stream holder: a line failed after {sent} chunk(s): {err} — signalling degrade")
        _send_all(conn, sse_event("error", {"error": f"cloud stream failed ({type(err).__name__})", "chunks": sent}))
        return
    _send_all(conn, sse_event("end", {"chunks": sent, "engine": s["provider"]}))


def _bind_unix_listener(path: str):
    """A fresh AF_UNIX listener at `path` (any stale socket file removed first). NOT yet accepting."""
    try:
        os.unlink(path)
    except OSError:
        pass
    if not hasattr(socket, "AF_UNIX"):
        raise OSError("Unix-domain sockets are unavailable on this platform")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    return listener


def run_holder(
    s, entry, key, digest, *, connect=wsclient.connect, clock=time.monotonic,
    sleep=time.sleep, bind_socket=_bind_unix_listener,
) -> int:
    """The resident holder loop: bind the unix socket, hold the ElevenLabs websocket across turns,
    keepalive it when idle, self-exit after STREAM_HOLDER_IDLE_EXIT of silence. Returns the exit
    code (0 self-exit / 1 could not bind). Every socket seam is injected for the loopback tests."""
    stopping = {"now": False}

    def _stop(signum, frame):  # noqa: ARG001 — signal-handler signature
        stopping["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # announce before anything that can block: ensure_stream_holder waits on pidfile + socket
    _write_stream_holder_pid(os.getpid(), digest)
    try:
        listener = bind_socket(_STREAM_HOLDER_SOCK)
    except OSError as err:
        log(f"stream holder could not bind its socket: {err}")
        _clear_stream_holder_pid()
        return 1
    holder = TtsStreamHolder(entry, s, key, connect=connect, clock=clock, sleep=sleep)
    holder.prime()  # best-effort warmup; failure is logged inside prime() and non-fatal
    last_activity = clock()
    try:
        listener.settimeout(STREAM_KEEPALIVE_SECONDS)  # accept() times out -> keepalive + idle-exit
        while not stopping["now"]:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                if clock() - last_activity >= STREAM_HOLDER_IDLE_EXIT:
                    log("stream holder idle — exiting (the next streaming turn respawns)")
                    break
                holder.keepalive_if_due()
                continue
            except OSError:
                break  # the listener died (shutdown) — stop
            last_activity = clock()
            try:
                _serve_stream_connection(conn, holder, s, clock=clock)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                last_activity = clock()
    finally:
        holder.close()
        try:
            listener.close()
        except OSError:
            pass
        _remove_socket_file(_STREAM_HOLDER_SOCK)
        _clear_stream_holder_pid()
    return 0


def run_holder_main(digest: str) -> int:
    """The holder process entry: load config, refuse fast when streaming is off / the provider has
    no variant / there is no key, fold speed into voice_settings, and run the loop."""
    cfg_path = os.environ.get(
        "VOICE_LOOP_CONFIG",
        os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "voice-loop/config.json"),
    )
    s = resolve_settings(load_config(cfg_path), platform.system())
    if not s["streaming"]:
        return 1
    entry = resolve_tts_provider(s["provider"])
    if entry.streaming is None:
        return 1
    key = read_key(s["key_file"], s["key_env"], os.environ)
    if not key:
        log("stream holder: no key — exiting")
        return 1
    # fold speed into voice_settings so the BOS carries it on the held socket's first frame
    s["voice_settings"] = {**(s["voice_settings"] or {}), "speed": s["speed"]}
    return run_holder(s, entry, key, digest)


def main() -> int:
    # THE clock: every logged timing (extract_ms, first_audio_ms, total_ms) is an offset from this
    # one instant, so the three are directly comparable and none can hide a wait the other counted.
    global _stop_reason_logged
    _stop_reason_logged = False
    t0 = time.monotonic()
    deadline = t0 + HOOK_BUDGET_S
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
    if not s["enabled"]:
        # Read AFTER the event is known, which is the only reason the stdin read moved above it:
        # "speech is off" is a diagnosis a Stop turn owes its log (#106, conformance 3.12), and an
        # eager firing owes nothing — it would write that same line after every single tool call.
        if not eager:
            log("stop: speech is switched off (speak.enabled) — nothing was spoken this turn")
        return 0
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
        try:
            with open(_LAST_KEY_PATH, encoding="utf-8") as fh:
                prev_key = fh.read().strip()
        except OSError:
            prev_key = ""

        claimed: list[str] = []
        vetoed: list[str] = []

        current_key = ""
        current_text: str | None = None
        fresh_reads = 0

        def read_fresh() -> str | None:
            """One transcript read: the marked lines this event owns, minus everything the ledger
            already accounts for. The ledger is re-read every time — a firing that ran between our
            rounds may have claimed a line since. Eager-off also carries the last message identity so
            a repeated line in a new message is decided dedup rather than a flush race."""
            nonlocal current_key, current_text, fresh_reads
            if not ledger_on:
                fresh_reads += 1
                message_index: int | None = None

                def remember_message(index: int, line: str) -> bool:
                    nonlocal message_index
                    message_index = index
                    return True

                text = extract(transcript, s["marker"], s["max_chars"], accept=remember_message)
                current_text = text
                current_key = ledger_key(transcript, message_index, text) if text and message_index is not None else ""
                # A matching key is the same message still being observed: return None so the only
                # retry gate below sees the canonical race signature. Keep the text separately for
                # the final dedup verdict once the bounded wait has finished.
                if current_key and current_key == prev_key and fresh_reads > 1:
                    return None
                return text
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
            """Is this read final? A parsed empty message is final; None is the flush race. An
            eager-off read of the previous message's same line is also a race, identified by its
            transcript/message/line key rather than text alone."""
            return value == "" or bool(
                value
                and (
                    ledger_on
                    or value != prev
                    or (prev_key and current_key != prev_key)
                )
            )

        # Flush race: read immediately; retry ONLY on the race signatures — nothing new extracted
        # (None: no assistant message yet, or nothing but lines already spoken), or, with the ledger
        # off, an extract identical to the previously spoken line. A parsed message that yields ''
        # (no marker, or a marker with no text) is FINAL: exit at once, zero backoff. The eager path
        # never retries — a line half-written now is caught by the next firing, which costs nothing.
        #
        # The activity reading is taken BEFORE the first read, so anything appended while the
        # ladder runs is already visible to wait_out_flush's first stat — and an idle file is
        # answered there without a sleep.
        activity = transcript_activity(transcript)
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
                log(f"stop: waiting because extract was {'IDENTICAL' if text is not None or (current_key == prev_key and current_text == prev) else 'EMPTY'}")
                text = wait_out_playback(read_fresh, settled, text, deadline=deadline)
            if not ledger_on and not settled(text):
                # …and the other reason a line can be missing: nobody is playing, the ladder simply
                # ended before this turn's own message finished being written (#106). Waited out on
                # the file's own evidence, and free when there is none.
                #
                # EAGER-OFF ONLY, and that is the whole gate. With eager ON this Stop has a
                # successor — the next PostToolUse firing reads EVERY message and will say the late
                # line for free — so waiting here buys nothing and costs everything: the whole
                # read-claim-speak sequence runs under speaking.lock, and a Stop holding it for up
                # to 35 s is 35 s in which no eager firing can speak at all. (It also subsumes the
                # ledger's veto, which only exists with the ledger on: "eager already said it" is a
                # decided answer, never a race.) #106 was reported on the default, eager-off
                # install, which is exactly the configuration that has no successor to fall back on.
                log(f"stop: waiting because extract was {'IDENTICAL' if text is not None or (current_key == prev_key and current_text == prev) else 'EMPTY'}")
                text = wait_out_flush(transcript, read_fresh, settled, text, activity, deadline=deadline)
        if text is None and current_key and current_key == prev_key:
            # The race wait uses None as its public signature, but once its bound expires the
            # candidate is still the previous utterance and must reach the ordinary dedup verdict.
            text = current_text
        if not text:
            if not eager:
                # A Stop that leaves without speaking has abandoned the turn for good: unlike an
                # eager firing, it has no successor one tool call behind it. So every one of its
                # exits says which one it was — including the two that lost nothing (the ledger's
                # veto, because eager already said those lines out loud, and a message with no
                # marked line in it), because "no log line at all" is the one answer that leaves a
                # silent turn undiagnosable (conformance 3.12).
                if text is None and vetoed:
                    log(f"stop: nothing new — the ledger already accounts for {len(vetoed)} marked line(s)")
                elif text is None:
                    log("stop: gave up with nothing new in the transcript — a line written now is DROPPED")
                else:
                    log("stop: nothing marked in the last assistant message — nothing to speak this turn")
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
            _write_last_key(current_key)
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

        # A lock that degraded to _NoLock is a fact about THIS spoken turn, not about the
        # firing: an eager firing that found nothing new also degrades and owes the log
        # nothing, so the reason rides the lock object to here and is logged exactly once.
        if isinstance(lock, _NoLock) and lock.reason:
            log(lock.reason)
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
    # The holder daemon is this same script under a different argv (the speak-path mirror of #99's
    # stream-worker): when invoked as `speak.py stream-holder <digest>` it runs the resident socket
    # loop instead of the hook, and the contour check below does not apply to it.
    if len(sys.argv) > 1 and sys.argv[1] == STREAM_HOLDER_ARG:
        return run_holder_main(sys.argv[2] if len(sys.argv) > 2 else "")
    rc = main()
    if _fired["event"] == "Stop" and not _stop_reason_logged:
        log("stop: exited with no reason recorded")
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
