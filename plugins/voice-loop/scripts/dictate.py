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
"""

from __future__ import annotations

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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

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

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
_LOG_PATH = os.path.join(_STATE_DIR, "dictate.log")
_PID_PATH = os.path.join(_STATE_DIR, "dictate.pid")
_WAV_PATH = os.path.join(_STATE_DIR, "dictate.wav")
_LAST_WAV_PATH = os.path.join(_STATE_DIR, "dictate-last.wav")
# When the previous toggle happened — the whole state the key-repeat guard keeps. Separate from the
# pidfile on purpose: the pidfile says whether a recorder is running, this says how long ago the
# hotkey was last acted on, and the guard has to answer before either branch is chosen.
_TOGGLE_PATH = os.path.join(_STATE_DIR, "dictate-last-toggle")
# Which window was focused when THIS recording started — the whole state the same-window guard
# keeps, written at start and consumed at stop. Absent (guard off at start, an unwritable state
# dir, a platform that cannot name its focus) reads as "unknown", which pastes.
_FOCUS_PATH = os.path.join(_STATE_DIR, "dictate-focus")

# CROSS-SCRIPT CONTRACT (keep in sync with speak.py): speak.py records its live speaking chain in
# playing.pid — space-separated PIDs, its python process first, then the current player/command
# child. The echo guard below reads THIS file to stop in-flight playback before recording, and
# verifies each pid via /proc/<pid>/cmdline before signalling (PID-reuse guard), exactly like
# speak.py's own take_over does.
_SPEAK_PID_PATH = os.path.join(_STATE_DIR, "playing.pid")

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


def resolve_settings(config: dict, system: str) -> dict:
    """Every knob dictate-toggle.sh honoured, same names, same defaults, same precedence."""
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
        # milliseconds in the config (what a human reasons about for a keypress), seconds inside
        "debounce_ms": resolve_debounce_ms(cfg(config, "dictate.debounce_ms", DEBOUNCE_SECONDS * 1000)),
        "clipboard": str(cfg(config, "dictate.clipboard", "auto")),
        "start_sound": str(cfg(config, "dictate.start_sound", "")),
        "stop_sound": str(cfg(config, "dictate.stop_sound", "")),
        "player": str(cfg(config, "speak.player", "afplay" if system == "Darwin" else "aplay -q")),
        "backend": str(cfg(config, "stt.backend", "lan")),
        "endpoint": str(cfg(config, "stt.endpoint", "http://127.0.0.1:8355")),
        # top-level "language" is the one the user sets; ".stt.language" is the advanced escape
        # for people who dictate in one language and listen in another.
        "language": str(cfg(config, "stt.language", cfg(config, "language", "en"))),
        "stt_model": str(cfg(config, "stt.model", "whisper-1")),
        "stt_command": str(cfg(config, "stt.command", "")),
        "key_env": str(cfg(config, "stt.cloud.api_key_env", cfg(config, "stt.api_key_env", "VOICE_LOOP_STT_API_KEY"))),
        "key_file": str(cfg(config, "stt.cloud.key_file", "")),
        "timeout": float(cfg(config, "stt.timeout", 60)),
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


def recorder_argv(recorder: str, system: str, wav: str) -> list[str]:
    """The exact device/format flags the shell used: 16 kHz, mono, S16 — [] for an unknown name."""
    if recorder == "pw-record":
        return ["pw-record", "--rate", str(RECORD_RATE), "--channels", "1", wav]
    if recorder == "arecord":
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(RECORD_RATE), "-c", "1", wav]
    if recorder == "sox":
        return ["rec", "-q", "-r", str(RECORD_RATE), "-c", "1", "-b", "16", wav]
    if recorder == "ffmpeg":
        source = ["-f", "avfoundation", "-i", ":default"] if system == "Darwin" else ["-f", "alsa", "-i", "default"]
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", *source, "-ar", str(RECORD_RATE), "-ac", "1", "-y", wav]
    return []


def clip_seconds(size_bytes: int) -> float:
    """A 16 kHz mono S16 WAV's audible length from its byte size (header excluded)."""
    return max(0, size_bytes - WAV_HEADER_BYTES) / BYTES_PER_SECOND


def resolve_clipboard(clipboard: str, system: str, have, wayland: bool) -> str:
    """auto -> pbcopy (macOS), wl-copy (a live Wayland session), xclip, wl-copy (installed but no
    $WAYLAND_DISPLAY — XWayland setups); an explicit name is taken as-is."""
    if clipboard != "auto":
        return clipboard
    if system == "Darwin":
        return "pbcopy"
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
    return []


def pick_paste_tool(system: str, have, ydotool_socket_ok: bool, display: str) -> str:
    """The platform table, in the shell's order: osascript / ydotool(+socket) / wtype / xdotool."""
    if system == "Darwin":
        return "osascript" if have("osascript") else ""
    if have("ydotool") and ydotool_socket_ok:
        return "ydotool"
    if have("wtype"):
        return "wtype"
    if display and have("xdotool"):
        return "xdotool"
    return ""


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


def multipart_form(fields: dict[str, str], file_field: str, filename: str, payload: bytes, boundary: str) -> bytes:
    """A multipart/form-data body the way curl -F built it: text fields, then one WAV part."""
    lines: list[str] = []
    for name, value in fields.items():
        lines += [f"--{boundary}", f'Content-Disposition: form-data; name="{name}"', "", value]
    lines += [
        f"--{boundary}",
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"',
        "Content-Type: audio/wav",
        "",
    ]
    head = "\r\n".join(lines).encode("utf-8") + b"\r\n"
    return head + payload + f"\r\n--{boundary}--\r\n".encode("ascii")


def transcript_from_response(raw: bytes | None) -> str:
    """The ``text`` field of a JSON response, stripped — '' for anything malformed (parity with
    the shell's ``python3 -c 'json.load(...).get("text","")'`` tail, which printed '' on error)."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    return str(data.get("text", "")).strip() if isinstance(data, dict) else ""


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
    """POST the form, return the body even on an HTTP error (the body is the diagnosis). None only
    when the server was unreachable. Proxies bypassed (parity with ``curl --noproxy '*'``)."""
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **headers},
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
    """The recorded WAV -> text, by whichever backend is configured."""
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
    boundary = uuid.uuid4().hex
    if s["backend"] == "cloud":
        key = read_key(s["key_file"], s["key_env"], os.environ)
        if not key:
            log(f"cloud stt: no key (key_file unset/unreadable and ${s['key_env']} empty)")
            return ""
        body = multipart_form(
            {"model": s["stt_model"], "language": s["language"]}, "file", "dictate.wav", wav_bytes, boundary
        )
        raw = _post_multipart(
            f"{s['endpoint']}/v1/audio/transcriptions", {"Authorization": f"Bearer {key}"}, body, boundary, s["timeout"]
        )
    else:
        body = multipart_form({}, "audio", "dictate.wav", wav_bytes, boundary)
        url = f"{s['endpoint']}/stt?language={urllib.parse.quote(s['language'])}"
        raw = _post_multipart(url, {}, body, boundary, s["timeout"])
    return transcript_from_response(raw)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
    argv = recorder_argv(recorder, system, _WAV_PATH)
    if not argv:
        note("no recorder found — install pw-record/arecord (Linux) or sox/ffmpeg (macOS)", system)
        log("no recorder available")
        _release_claim(pidfile_fd)
        return 1
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=errlog)
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
    log(f"recording via {recorder} pid={proc.pid}")
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
    # Consumed unconditionally, whatever this stop goes on to do (see take_remembered_focus): an
    # identity must never outlive its own recording. Only the same-window guard below reads it.
    started_focus = take_remembered_focus()
    # Remove the pidfile BEFORE signalling: from this point the recording slot is free, so a
    # racing start claims a FRESH pidfile instead of adopting (and re-stopping) a dying recorder.
    try:
        os.unlink(_PID_PATH)
    except OSError:
        pass
    # SIGINT lets sox/ffmpeg finalize the WAV header; TERM is the fallback for pw-record/arecord.
    try:
        os.kill(recorder_pid, signal.SIGINT)
    except OSError:
        pass
    if not _wait_gone(recorder_pid):
        try:
            os.kill(recorder_pid, signal.SIGTERM)
        except OSError:
            pass
        _wait_gone(recorder_pid)
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
        note("clip too short — ignored", system)
        log(f"clip too short ({size} bytes ≈ {clip_seconds(size):.2f}s) — stt skipped")
        try:
            os.replace(_WAV_PATH, _LAST_WAV_PATH)
        except OSError:
            pass
        return 0

    note("transcribing…", system)
    text = transcribe(s)
    log(f"transcript: {text[:120]}")
    try:
        os.replace(_WAV_PATH, _LAST_WAV_PATH)
    except OSError:
        pass

    if not text:
        note("nothing recognized", system)
        log("empty transcription")
        return 0

    tool = resolve_clipboard(
        s["clipboard"], system, shutil.which, bool(os.environ.get("WAYLAND_DISPLAY"))
    )
    commands = clipboard_commands(tool)
    if not commands:
        log("no clipboard tool (install wl-clipboard or xclip)")
        note("no clipboard tool available", system)
        return 1
    for argv in commands:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
                subprocess.run(argv, input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=errlog, check=False)
        except OSError as err:
            log(f"clipboard failed: {err}")
            note("no clipboard tool available", system)
            return 1

    if s["auto_paste"]:
        # The same-window guard (dictate.paste_target): auto-paste presses the paste key into
        # whatever is focused NOW, so a window switch during the sentence sends it somewhere it was
        # never meant to go. Probing again only when start left an identity to compare against —
        # an unknown at either end degrades to "any", which is the documented default.
        if same_window_guard_on(s) and started_focus and focus_changed(started_focus, current_focus(system)):
            note("focus moved — text is in the clipboard", system)
            log("focus moved since this recording started — paste suppressed (dictate.paste_target=same-window)")
            return 0
        sock = os.environ.get("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")
        try:
            sock_ok = stat.S_ISSOCK(os.stat(sock).st_mode)
        except OSError:
            sock_ok = False
        paste_tool = pick_paste_tool(system, shutil.which, sock_ok, os.environ.get("DISPLAY", ""))
        if _run_paste(paste_tool, s["paste_key"], mode == "send", sock):
            log(f"auto-pasted (mode={mode} key={s['paste_key']})")
            return 0
        log("auto-paste unavailable — clipboard fallback")
    note(f"copied — press {s['paste_key']} to paste", system)
    return 0


def _run_paste(tool: str, paste_key: str, enter: bool, ydotool_socket: str) -> bool:
    """Execute a paste plan. True only when the paste keystroke itself was sent."""
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
            with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
                result = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=errlog, env=env, check=False)
            failed = result.returncode != 0
        except OSError:
            failed = True
        if failed and required:
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
    mode = argv[1] if len(argv) > 1 and argv[1] else s["mode"]

    # Key-repeat guard, before the pidfile is even read (see debounce_toggle): a held hotkey
    # autorepeats, and the fire that follows would otherwise stop a recording milliseconds old.
    # The window restarts on every fire, so the whole hold is one toggle however long it lasts.
    # Log only — a notification per repeat would be the same spam in another window.
    age = debounce_toggle(s["debounce_ms"] / 1000.0)
    if age is not None:
        log(f"toggle ignored — key repeat ({age * 1000:.0f} ms after the previous one)")
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
