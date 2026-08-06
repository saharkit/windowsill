#!/usr/bin/env python3
"""voice-loop — the bug-report collector: diagnostics in, a redacted bundle out.

This is `/report-bug`'s whole mechanism; ``scripts/report-bug.sh`` is only a thin launcher, and the
skill (``skills/report-bug/SKILL.md``) is the consent flow around it. Stdlib only, Python 3.10+.

The contract, and every line below serves it:

* **nothing leaves the machine unseen.** ``collect`` writes ONE bundle file and prints the exact
  same bytes to stdout. What the skill shows the user, what lands in the file, and what a transport
  sends are the same string — there is no second, richer copy anywhere.
* **metadata only.** Timestamps, byte counts, chunk counts, rc codes, durations, versions. The words
  someone spoke or heard NEVER leave: every log line is classified by ``LOG_RULES`` and the ones
  whose payload is user speech keep their event and lose their text. A line the table does not know
  is redacted at its first ``": "`` rather than trusted — the safe direction for a table that can
  fall behind the scripts it describes (``tests/test_report_bug.py`` fails the moment it does).
* **no keys, no user, no host.** ``redact`` runs over every string that reaches the bundle — the
  config, the environment, the logs, the health payload, the paths in all of them.
* **the transport never fires by itself.** Each of the three is its own subcommand, invoked only
  after the user has said yes to a named destination.

Why a table of log prefixes rather than a regex for "looks like speech": speech looks like prose,
and so do half the diagnostics. The two scripts that write these logs are in this same directory, so
what each line carries is *known* — an enumerated vocabulary with a drift test beats a heuristic
that has to be right about language it has never seen.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Sequence

# --- where things are --------------------------------------------------------------------------

# Triage lives where the fixes live. Both other transports are ramps into this one.
REPO = "saharkit/windowsill"

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
_CONFIG_PATH = os.environ.get(
    "VOICE_LOOP_CONFIG",
    os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "voice-loop/config.json"),
)
_PLUGIN_MANIFEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude-plugin", "plugin.json")

# The state dir, file by file, with what each one is allowed to contribute. `last-spoken` holds the
# text of the previous utterance verbatim, which is exactly what must not travel — it appears here
# for its SIZE and its mtime and nothing else, and that is enforced by there being no code path that
# opens it.
_STATE_FILES = (
    ("dictate.log", "the dictation toggle's log"),
    ("speak.log", "the speaking hook's log"),
    ("spoken.ledger", "the spoken-ledger (opaque line hashes)"),
    ("last-spoken", "the previous utterance — size only, never its text"),
    ("speaking.lock", "the inter-firing speaking lock"),
    ("playing.pid", "the playback pidfile"),
    ("hook-last-fired", "the hook heartbeat stamp"),
    ("dictate.pid", "the recorder pidfile"),
    ("dictate.wav", "the clip being recorded"),
    ("dictate-last.wav", "the last recorded clip"),
    ("dictate-last-toggle", "the key-repeat debounce stamp"),
)

# How much of each log travels. Sixty lines is two or three full turns of speak.log — enough to see
# a failure and what led to it, small enough that a human reads the whole bundle before consenting.
LOG_TAIL_LINES = 60
JOB_LINES = 20

HEALTH_TIMEOUT = 3.0
GH_TIMEOUT = 30.0

# A pre-filled issue URL has to survive a browser address bar and GitHub's own request line; 8000
# is the usual practical ceiling (8 KiB) with headroom. A mailto: goes through a desktop handler,
# where 2 KiB is the documented Windows limit and the smallest of the three.
ISSUE_URL_LIMIT = 8000
MAILTO_LIMIT = 1800

# The service mailbox for the no-GitHub tier. It is EMPTY on purpose: `saharkit.com` publishes no MX
# record today, so a hardcoded address here would send a user's report into a black hole that looks
# like success. The transport is built and tested; it reports itself unavailable until an operator
# points this at a mailbox that exists (via the env var, or by this default gaining a value once the
# conformance ticket for the mailbox closes).
_MAILBOX_ENV = "VOICE_LOOP_REPORT_MAILBOX"
_DEFAULT_MAILBOX = ""


def _default_clock() -> datetime:
    """UTC now. Injected everywhere below so a bundle's stamps are reproducible in a test."""
    return datetime.now(timezone.utc)


Clock = Callable[[], datetime]
Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    """Every subprocess here: an argv LIST (never a shell string), a mandatory wall-clock timeout,
    and check=False so the caller turns an exit code into a result instead of an exception."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)


# --- redaction ----------------------------------------------------------------------------------

_HOME = os.path.expanduser("~")
_OTHER_HOME_RE = re.compile(r"(/(?:home|Users)/)[^/\s:\"']+")
_URL_RE = re.compile(r"\b(https?://)([^\s/\"'<>)\]]+)")
# An address also reaches the logs bare, with no scheme in front of it: "Connection refused to
# 192.168.7.31" is a urllib reason string, and it names the user's network just as precisely as a
# URL does. A dotted quad is unambiguous enough to rewrite on sight (a version number has three
# components, not four); a bare *hostname* in prose is not, and is left to the URL rule.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})

# Ordered: the shaped credentials first (so a token is named as one), the catch-all long run last.
# The catch-all is deliberately blunt — anything 32 characters of key-alphabet long is either a
# secret or a hash, and neither is worth a diagnosis.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "<redacted-key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "<redacted-key>"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "<redacted-key>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "<redacted-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), "<redacted-key>"),
    (re.compile(r"(?i)\b(bearer|authorization|api[_\-]?key|token|secret|password)\b([\"'\s:=]+)[^\s\"']+"), r"\1\2<redacted-key>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<redacted-key>"),
)

_SECRET_KEY_RE = re.compile(r"(?i)(key|token|secret|password|auth|credential)")


def usernames() -> tuple[str, ...]:
    """Every name this machine calls its user, longest first so the longest match wins.

    Short names are skipped: replacing a two-letter login everywhere would mangle the diagnostics it
    is meant to protect, and a two-letter login is not what identifies anybody.
    """
    names = {os.path.basename(_HOME)}
    for var in ("USER", "LOGNAME", "USERNAME"):
        names.add(os.environ.get(var, ""))
    try:
        names.add(getpass.getuser())
    except Exception:  # no passwd entry and no env var: nothing to redact, not a failure
        pass
    return tuple(sorted((name for name in names if len(name) >= 3), key=len, reverse=True))


def _redact_host(match: "re.Match[str]") -> str:
    netloc = match.group(2)
    host, sep, port = netloc.rpartition(":")
    if not sep or not port.isdigit():  # no port: the whole netloc is the host
        host, sep, port = netloc, "", ""
    if host.lower() in _LOOPBACK_HOSTS:
        return match.group(0)  # loopback is the fact worth keeping, and it names nobody
    return f"{match.group(1)}<host>{sep}{port}"


def redact(text: str) -> str:
    """Every string that reaches the bundle goes through here.

    Order matters: the home directory collapses to ``~`` before usernames are hunted (so the common
    case is one clean substitution), other users' homes next, then bare logins as whole words, then
    hosts, then credentials — the credential sweep runs last so it also catches whatever the earlier
    rewrites left behind.
    """
    if not text:
        return text
    if _HOME and _HOME != "/":
        text = text.replace(_HOME, "~")
    text = _OTHER_HOME_RE.sub(r"\1<user>", text)
    for name in usernames():
        text = re.sub(rf"\b{re.escape(name)}\b", "<user>", text)
    text = _URL_RE.sub(_redact_host, text)
    text = _IPV4_RE.sub(lambda m: m.group(0) if m.group(0) in _LOOPBACK_HOSTS else "<host>", text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(node: object, key: str = "") -> object:
    """A config/health tree with its secrets gone and its strings redacted.

    A key whose NAME says credential loses its value outright — the useful fact is that it is set,
    not what it is. ``*_env`` is the exception the config schema depends on: it names an environment
    variable rather than holding a secret, and a report that hides which variable was consulted
    hides the actual bug ("the key file is unset and $VOICE_LOOP_TTS_API_KEY is empty").
    """
    if isinstance(node, dict):
        return {str(k): redact_value(v, str(k)) for k, v in node.items()}
    if isinstance(node, list):
        return [redact_value(v, key) for v in node]
    if isinstance(node, str):
        if key and not key.endswith("_env") and not key.endswith("_file") and _SECRET_KEY_RE.search(key):
            return "<redacted-key>" if node else ""
        return redact(node)
    return node


# --- the log vocabulary ---------------------------------------------------------------------------

# Every line speak.py and dictate.py write, and what may travel from it.
#
#   (prefix, cut_at)
#     prefix  — the literal head of the log call's format string, up to its first interpolation.
#               This is the row's id: the drift test in tests/test_report_bug.py reads both scripts
#               and fails if any log call's head is not one of these.
#     cut_at  — None when the whole message is metadata. Otherwise the delimiter at which the
#               message stops being metadata: everything from the FIRST occurrence of it onward is
#               replaced by a character count. Those are the eight lines that carry user speech, a
#               server's echo of it, or a config value repr — the payloads a bug report must not have.
LOG_RULES: tuple[tuple[str, str | None], ...] = (
    # speak.py
    ("config ignored (", None),
    ("key file unreadable (", None),
    ("marker with no text", None),
    ("ledger unwritable: ", None),
    ("ledger trim failed: ", None),
    ("synthesis unreachable: ", None),
    ("stream chunk ", None),
    ("stream error after ", "chunk(s): "),
    ("stream read failed after ", None),
    ("stream refused (", "): "),
    ("stream unreachable: ", None),
    ("empty synthesis from ", None),
    ("synthesis returned an error document: ", "document: "),
    ("stop: still playing after ", None),
    ("stop: the line waited ", None),
    ("stop: the line in front finished after ", None),
    ("player failed: ", None),
    ("hook payload was not JSON", None),
    ("no transcript to read: transcript_path=", None),
    ("eager: another firing is speaking", None),
    ("stop: the speaking lock is still held", None),
    ("seeded ", None),
    ("stop: nothing new", None),
    ("stop: gave up with nothing new", None),
    ("stop: dropped a read identical to the last spoken line (dedup): ", "(dedup): "),
    ("text: ", "text: "),
    ("local command failed: ", None),
    ("local command rc=", None),
    ("timings extract_ms=", None),
    ("cloud tts: no key", None),
    ("stream died before its first chunk", None),
    ("played rc=", None),
    ("nothing played via=", None),
    # dictate.py
    ("dictate.debounce_ms is not a usable number (", "number ("),
    ("stt unreachable: ", None),
    ("stt command failed: ", None),
    ("cloud stt: no key", None),
    ("cloud stt failed — falling back to local whisper", None),
    ("elevenlabs stt: no key (key_file unset/unreadable, $", None),
    ("cloud stt returned an error: ", "error: "),
    ("cloud stt returned undecodable response: ", "response: "),
    ("debounce stamp unavailable (", None),
    ("debounce stamp locked by another toggle", None),
    ("debounce stamp not written: ", None),
    ("pidfile claim failed: ", None),
    ("no recorder available", None),
    ("recorder failed: ", None),
    ("recording via ", None),
    ("clip too short (", None),
    ("transcript: ", "transcript: "),
    ("empty transcription", None),
    ("no clipboard tool (", None),
    ("clipboard failed: ", None),
    ("auto-pasted (", None),
    ("auto-paste unavailable", None),
    ("toggle ignored — key repeat (", None),
    ("pidfile already claimed", None),
    ("dictate.paste_target is not a known value (", ") — using"),
    ("focus probe failed (", None),
    ("focus probe returned ", " — focus"),
    ("focus not recorded: ", None),
    ("focus at start: ", " — the same-window"),
    ("focus moved since this recording started", None),
    ("accessibility permission denied", None),
    ("auto-paste denied", None),
    ("paste spawn failed (", None),
    # both
    ("unexpected error: ", "unexpected error: "),
)


def _validate_log_rules() -> None:
    """Run at import: an ambiguous table would silently classify a line under the wrong row."""
    seen: set[str] = set()
    for prefix, cut_at in LOG_RULES:
        if not prefix:
            raise ValueError("LOG_RULES: an empty prefix matches every line")
        if prefix in seen:
            raise ValueError(f"LOG_RULES: duplicate prefix {prefix!r}")
        if cut_at is not None and not cut_at:
            raise ValueError(f"LOG_RULES: {prefix!r} has an empty cut marker")
        seen.add(prefix)
    for prefix in seen:
        for other in seen:
            if prefix != other and other.startswith(prefix):
                raise ValueError(f"LOG_RULES: {prefix!r} also matches {other!r} — one line, two rows")


_validate_log_rules()

# The scrubbed lines that read as "a piece of work finished, and how it went". The bundle lists the
# last JOB_LINES of them separately, because a maintainer's first question is always "did the last
# few actually complete, and how long did they take".
_JOB_PREFIXES = (
    "played rc=",
    "nothing played via=",
    "timings extract_ms=",
    "transcript: ",
    "clip too short (",
    "empty transcription",
    "recording via ",
    "local command rc=",
)

_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) (.*)$")

_MATCH_ORDER = tuple(sorted(LOG_RULES, key=lambda row: len(row[0]), reverse=True))


def scrub_message(message: str) -> tuple[str, bool]:
    """One log message, reduced to what may travel. Returns ``(scrubbed, classified)``.

    A message the table does not recognise is NOT trusted: it is cut at its first ``": "`` the same
    way a known content-bearing line would be — and if it has no such delimiter, it travels as its
    size alone. Either way it is reported as unclassified, so the bundle says out loud that this
    machine's scripts have moved on from this table.
    """
    for prefix, cut_at in _MATCH_ORDER:
        if message.startswith(prefix):
            if cut_at is None:
                return redact(message), True
            return _cut(message, prefix, cut_at), True
    return _cut(message, "", ": "), False


def _cut(message: str, prefix: str, cut_at: str) -> str:
    """Keep the message up to and including ``cut_at``; replace the rest with its size.

    The count is the point: "the transcript was 41 characters" is a real diagnostic (it separates
    "STT returned nothing" from "STT returned something the paste dropped") and it is not the words.
    """
    index = message.find(cut_at)
    if index < 0:
        head, tail = prefix, message[len(prefix):]
    else:
        head, tail = message[: index + len(cut_at)], message[index + len(cut_at):]
    if not tail:
        return redact(head)
    return f"{redact(head)}<redacted {len(tail)} chars>"


def scrub_log_line(line: str) -> tuple[str, bool]:
    """One raw log line -> what may travel, and whether the table recognised it.

    A line without the scripts' timestamp is not one of their log calls at all: it is a subprocess's
    stderr, appended straight to the same file (the recorder, the player, the clipboard tool, a
    whisper.cpp ``stt.command`` — which prints its transcript there). Third-party output is
    unclassifiable by construction, so it travels as its size alone.
    """
    if not line.strip():
        return "", True  # a blank line separates a tool's output; it carries nothing to withhold
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return f"<tool output, {len(line)} chars, withheld>", True
    stamp, message = match.group(1), match.group(2)
    scrubbed, classified = scrub_message(message)
    return f"{stamp} {scrubbed}", classified


def read_log_tail(path: str, lines: int = LOG_TAIL_LINES) -> dict[str, object]:
    """The last ``lines`` of a log, scrubbed. A missing log is a fact, not an error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read().splitlines()
    except FileNotFoundError:
        return {"present": False, "lines": [], "unclassified": 0}
    except OSError as err:
        return {"present": False, "error": f"{type(err).__name__}", "lines": [], "unclassified": 0}
    tail = raw[-lines:] if lines > 0 else []
    scrubbed, unclassified = [], 0
    for line in tail:
        text, classified = scrub_log_line(line)
        scrubbed.append(text)
        unclassified += 0 if classified else 1
    return {"present": True, "total_lines": len(raw), "lines": scrubbed, "unclassified": unclassified}


def recent_jobs(*tails: dict[str, object], limit: int = JOB_LINES) -> list[str]:
    """The last few outcome lines across the logs, newest last — already scrubbed by the tails."""
    picked: list[str] = []
    for tail in tails:
        for line in tail.get("lines", []):  # type: ignore[union-attr]
            message = line.split(" ", 1)[-1]
            if message.startswith(_JOB_PREFIXES):
                picked.append(line)
    return picked[-limit:]


# --- collection -----------------------------------------------------------------------------------


def read_json(path: str) -> tuple[dict, str]:
    """A JSON file as a dict, plus a one-word note about why it is empty when it is."""
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        return {}, "absent"
    except (OSError, ValueError) as err:
        return {}, f"unreadable ({type(err).__name__})"
    return (loaded, "ok") if isinstance(loaded, dict) else ({}, "not an object")


def collect_versions() -> dict[str, object]:
    manifest, note = read_json(_PLUGIN_MANIFEST)
    return {
        "plugin": manifest.get("version", "unknown"),
        "plugin_manifest": note,
        "python": platform.python_version(),
    }


def collect_machine() -> dict[str, object]:
    """OS and hardware CLASS — what a maintainer needs to reproduce, nothing that names a machine.

    Deliberately absent: the hostname, the kernel build string and the release, each of which is
    either identifying or a version of "which laptop is this".
    """
    return {
        "system": platform.system(),
        "arch": platform.machine(),
        "cpus": os.cpu_count(),
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "locale": os.environ.get("LANG", ""),
    }


def collect_environment() -> dict[str, str]:
    """The plugin's own environment variables. A credential-named one is reported as SET, never read."""
    found = {}
    for name in sorted(os.environ):
        if not name.startswith("VOICE_LOOP_"):
            continue
        value = os.environ[name]
        found[name] = "<set>" if _SECRET_KEY_RE.search(name) else redact(value)
    return found


def collect_config(path: str = _CONFIG_PATH) -> tuple[dict[str, object], dict]:
    """The config section of the bundle, plus the RAW config the caller still needs (the endpoints
    to probe live in it). Read once: a report should not race a user editing their own config."""
    config, note = read_json(path)
    return {"path": redact(path), "status": note, "config": redact_value(config)}, config


def collect_tools() -> dict[str, bool]:
    """Which pieces of the contour are installed at all — the answer to half the reports."""
    return {name: shutil.which(name) is not None for name in (
        "python3", "gh", "jq", "aplay", "paplay", "afplay", "ffmpeg", "pw-record", "arecord",
        "sox", "wl-copy", "xclip", "pbcopy", "wtype", "xdotool", "ydotool", "say",
    )}


def fetch_health(endpoint: str, timeout: float = HEALTH_TIMEOUT) -> dict[str, object]:
    """GET ``<endpoint>/health``. Unreachable is the most common bug report there is, so the failure
    is a reported value rather than an exception. Proxies bypassed, parity with the other scripts."""
    if not endpoint:
        return {"endpoint": "", "reachable": False, "error": "no endpoint configured"}
    url = f"{endpoint.rstrip('/')}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        return {"endpoint": redact(url), "reachable": True, "error": f"HTTP {err.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        return {"endpoint": redact(url), "reachable": False, "error": f"{type(err).__name__}"}
    return {
        "endpoint": redact(url),
        "reachable": True,
        "health": redact_value(payload) if isinstance(payload, dict) else {},
    }


def endpoints(config: dict) -> list[str]:
    """The distinct endpoints the config points at, in config order."""
    found: list[str] = []
    for section in ("stt", "tts"):
        node = config.get(section)
        endpoint = node.get("endpoint", "") if isinstance(node, dict) else ""
        if isinstance(endpoint, str) and endpoint and endpoint not in found:
            found.append(endpoint)
    return found


def collect_state(state_dir: str = _STATE_DIR, clock: Clock = _default_clock) -> list[dict[str, object]]:
    """Size and age of each state file. Never a byte of any of their contents."""
    now = clock()
    rows = []
    for name, what in _STATE_FILES:
        path = os.path.join(state_dir, name)
        try:
            stat = os.stat(path)
        except OSError:
            rows.append({"name": name, "what": what, "present": False})
            continue
        age = now - datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        rows.append({
            "name": name,
            "what": what,
            "present": True,
            "bytes": stat.st_size,
            "age_seconds": round(age.total_seconds(), 1),
        })
    return rows


def build_bundle(
    *,
    summary: str = "",
    state_dir: str = _STATE_DIR,
    config_path: str = _CONFIG_PATH,
    lines: int = LOG_TAIL_LINES,
    clock: Clock = _default_clock,
    health_timeout: float = HEALTH_TIMEOUT,
) -> dict[str, object]:
    """Everything the report is allowed to know, already redacted. Pure data — rendering is next."""
    config_section, raw_config = collect_config(config_path)
    dictate_tail = read_log_tail(os.path.join(state_dir, "dictate.log"), lines)
    speak_tail = read_log_tail(os.path.join(state_dir, "speak.log"), lines)
    return {
        "generated_at": clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": redact(summary),
        "versions": collect_versions(),
        "machine": collect_machine(),
        "config": config_section,
        "environment": collect_environment(),
        "tools": collect_tools(),
        "servers": [fetch_health(endpoint, health_timeout) for endpoint in endpoints(raw_config)],
        "state": collect_state(state_dir, clock),
        "jobs": recent_jobs(dictate_tail, speak_tail),
        "logs": {"dictate.log": dictate_tail, "speak.log": speak_tail},
    }


# --- rendering ------------------------------------------------------------------------------------

_FENCE = "```"

# The bundle carries text nobody in this repo wrote — a config a user typed, a server's error, a
# tool's stderr. Spliced into a fenced block, a literal fence inside it would end the block early and
# let the rest render as markdown. Defusing is a plain deterministic replace, applied before every
# splice: no clock, no randomness, the same input renders the same bytes every time.
def _defuse(text: str) -> str:
    return text.replace(_FENCE, "'''")


def _fenced(body: str, language: str = "") -> str:
    return f"{_FENCE}{language}\n{_defuse(body)}\n{_FENCE}"


def _kv_block(mapping: dict[str, object]) -> str:
    if not mapping:
        return "_(none)_"
    return _fenced("\n".join(f"{key}: {value}" for key, value in mapping.items()))


def render_digest(bundle: dict) -> str:
    """The bundle as the one artifact that travels: shown in chat, written to disk, sent as the body.

    Byte-exact by construction — every transport is handed THIS string, and the consent step shows
    THIS string. There is no fuller variant kept back for the maintainer.
    """
    logs = bundle.get("logs", {})
    unclassified = sum(int(tail.get("unclassified", 0)) for tail in logs.values())
    parts = [
        "## voice-loop bug report",
        "",
        f"Collected {bundle.get('generated_at', '')} by `scripts/report_bug.py`. "
        "Redacted: no keys, no usernames or home paths, no hostnames, and no transcript or spoken "
        "text — log lines that carry speech are reduced to a character count.",
        "",
    ]
    if bundle.get("summary"):
        parts += ["### What happened", "", str(bundle["summary"]), ""]
    parts += [
        "### Versions",
        "",
        _kv_block(dict(bundle.get("versions", {}))),
        "",
        "### Machine",
        "",
        _kv_block(dict(bundle.get("machine", {}))),
        "",
        "### Tools present",
        "",
        _fenced(", ".join(sorted(name for name, present in bundle.get("tools", {}).items() if present)) or "(none)"),
        "",
        "### Config",
        "",
        f"`{bundle.get('config', {}).get('path', '')}` — {bundle.get('config', {}).get('status', '')}",
        "",
        _fenced(json.dumps(bundle.get("config", {}).get("config", {}), indent=2, ensure_ascii=False, sort_keys=True), "json"),
        "",
        "### Environment (VOICE_LOOP_*)",
        "",
        _kv_block(dict(bundle.get("environment", {}))),
        "",
        "### Server health",
        "",
    ]
    servers = bundle.get("servers", [])
    if not servers:
        parts += ["_(no HTTP endpoint configured — command backends only)_", ""]
    for server in servers:
        parts += [_fenced(json.dumps(server, indent=2, ensure_ascii=False, sort_keys=True), "json"), ""]
    parts += [
        "### State",
        "",
        _fenced("\n".join(_state_row(row) for row in bundle.get("state", []))),
        "",
        f"### Last {JOB_LINES} job states",
        "",
        _fenced("\n".join(bundle.get("jobs", [])) or "(no completed jobs in the log tail)"),
        "",
    ]
    for name, tail in logs.items():
        parts += [
            f"### {name} (last {len(tail.get('lines', []))} of {tail.get('total_lines', 0)} lines)"
            if tail.get("present")
            else f"### {name} (absent)",
            "",
            _fenced("\n".join(tail.get("lines", [])) or "(empty)"),
            "",
        ]
    if unclassified:
        parts += [
            f"> {unclassified} log line(s) were not recognised by this collector's table and were "
            "cut at their first `: ` rather than trusted — the scripts on this machine are newer or "
            "older than `scripts/report_bug.py`.",
            "",
        ]
    return "\n".join(parts).rstrip() + "\n"


def _state_row(row: dict) -> str:
    if not row.get("present"):
        return f"{row.get('name', ''):<22} {'absent':>11}  {'':>14}  {row.get('what', '')}"
    return (
        f"{row.get('name', ''):<22} {row.get('bytes', 0):>9} B  "
        f"{row.get('age_seconds', 0):>10.1f}s ago  {row.get('what', '')}"
    )


def write_bundle(path: str, text: str) -> str:
    """Write the digest so a reader never sees a half-written one, and nobody else sees it at all.

    Temp file in the same directory (so ``os.replace`` is an atomic rename on one filesystem),
    fsynced, then moved into place. ``mkstemp`` creates it 0600, which is the mode this file wants:
    it is diagnostics about one user's machine, sitting in a shared state tree.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".voice-loop-report-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def default_bundle_path(state_dir: str = _STATE_DIR, clock: Clock = _default_clock) -> str:
    return os.path.join(state_dir, f"bug-report-{clock().strftime('%Y%m%dT%H%M%SZ')}.md")


# --- transports -------------------------------------------------------------------------------------


def service_mailbox() -> str:
    return os.environ.get(_MAILBOX_ENV, _DEFAULT_MAILBOX).strip()


def gh_ready(runner: Runner = _default_runner) -> tuple[bool, str]:
    """Is there an authenticated ``gh`` on this machine? Never prompts, never opens a browser."""
    if shutil.which("gh") is None:
        return False, "the gh CLI is not installed"
    try:
        proc = runner(["gh", "auth", "status"], 10.0)
    except (OSError, subprocess.SubprocessError) as err:
        return False, f"gh could not be run ({type(err).__name__})"
    if proc.returncode != 0:
        return False, "gh is installed but not authenticated (`gh auth login`)"
    return True, "gh is installed and authenticated"


def create_issue(title: str, body_path: str, repo: str = REPO, runner: Runner = _default_runner) -> tuple[bool, str]:
    """``gh issue create`` with the digest as the body. The file is passed by PATH, never by argv:
    a bundle is thousands of characters and an argv is not the place for it."""
    argv = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", body_path]
    try:
        proc = runner(argv, GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"gh issue create timed out after {GH_TIMEOUT:.0f}s"
    except (OSError, subprocess.SubprocessError) as err:
        return False, f"gh could not be run ({type(err).__name__})"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "gh issue create failed").strip()
    return True, (proc.stdout or "").strip()


def _fit_url(build: Callable[[str], str], body: str, limit: int, note: str) -> str:
    """The longest prefix of ``body`` whose URL fits, with ``note`` appended when anything was cut.

    Binary search rather than a shrink loop: percent-encoding expands by different amounts for
    different characters, so the only honest test is building the URL and measuring it — and this
    way that happens a bounded number of times, with the same answer every run.
    """
    whole = build(body)
    if len(whole) <= limit:
        return whole
    low, high = 0, len(body)
    best = build("" + note)
    while low <= high:
        mid = (low + high) // 2
        candidate = build(body[:mid].rstrip() + note)
        if len(candidate) <= limit:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    return best


def issue_url(title: str, body: str, repo: str = REPO, limit: int = ISSUE_URL_LIMIT, note: str = "") -> str:
    """A pre-filled new-issue form. Nothing is sent until the user presses Submit — that is the
    point of this tier: the consent is the Submit button, in GitHub's own UI."""
    base = f"https://github.com/{repo}/issues/new?"

    def build(text: str) -> str:
        return base + urllib.parse.urlencode({"title": title, "body": text})

    return _fit_url(build, body, limit, note or "\n\n… truncated to fit the URL — the full bundle is the file on disk.")


def mailto_url(address: str, subject: str, body: str, limit: int = MAILTO_LIMIT, note: str = "") -> str:
    """A mailto: for the no-GitHub tier. The bundle file stays on disk: mailto cannot attach, so the
    body carries the digest as far as it fits and the user attaches the file if they want the rest."""
    base = f"mailto:{urllib.parse.quote(address)}?"

    def build(text: str) -> str:
        return base + urllib.parse.urlencode({"subject": subject, "body": text}, quote_via=urllib.parse.quote)

    return _fit_url(build, body, limit, note or "\n\n… truncated for the mail client — attach the bundle file for the rest.")


def transports(runner: Runner = _default_runner) -> list[dict[str, object]]:
    """The three tiers, each with its destination NAMED — the consent step reads this out loud."""
    ready, why = gh_ready(runner)
    mailbox = service_mailbox()
    return [
        {
            "name": "gh",
            "available": ready,
            "reason": why,
            "destination": f"a new PUBLIC issue at github.com/{REPO}",
        },
        {
            "name": "url",
            "available": True,
            "reason": "a pre-filled form — nothing is sent until you press Submit",
            "destination": f"your browser, at github.com/{REPO}/issues/new",
        },
        {
            "name": "mailto",
            "available": bool(mailbox),
            "reason": (
                f"addressed to {mailbox}"
                if mailbox
                else f"no service mailbox is configured (set ${_MAILBOX_ENV}); the project's domain "
                "publishes no MX record yet, so this tier would silently drop the report"
            ),
            "destination": f"your mail client, addressed to {mailbox}" if mailbox else "(unavailable)",
        },
    ]


# --- the command line ---------------------------------------------------------------------------------


def _load_bundle_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _cmd_collect(args: argparse.Namespace) -> int:
    # The module constants are read HERE rather than taken as default arguments: a default is bound
    # once at import, and these have to be the paths in force when the command actually runs.
    bundle = build_bundle(summary=args.summary, lines=args.lines, state_dir=_STATE_DIR, config_path=_CONFIG_PATH)
    text = render_digest(bundle)
    path = args.out or default_bundle_path(_STATE_DIR)
    write_bundle(path, text)
    if args.json:
        print(json.dumps({"path": path, "digest": text}, ensure_ascii=False, indent=2))
    else:
        # stdout is the bundle and NOTHING else — "what you are about to send" has to be byte-exact,
        # so where the file landed goes to stderr rather than becoming a line the user cannot see is
        # not part of it.
        print(text, end="")
        print(f"bundle written to {redact(path)}", file=sys.stderr)
    return 0


def _cmd_transports(args: argparse.Namespace) -> int:
    rows = transports()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print(f"{row['name']:<7} {'available' if row['available'] else 'unavailable':<12} {row['reason']}")
    return 0


def _cmd_url(args: argparse.Namespace) -> int:
    print(issue_url(args.title, _load_bundle_text(args.bundle)))
    return 0


def _cmd_mailto(args: argparse.Namespace) -> int:
    address = args.to or service_mailbox()
    if not address:
        print(
            f"no service mailbox is configured: set ${_MAILBOX_ENV} or pass --to. "
            "The project's domain publishes no MX record yet, so this tier is not live.",
            file=sys.stderr,
        )
        return 1
    print(mailto_url(address, args.title, _load_bundle_text(args.bundle)))
    return 0


def _cmd_gh(args: argparse.Namespace) -> int:
    ready, why = gh_ready()
    if not ready:
        print(why, file=sys.stderr)
        return 1
    ok, message = create_issue(args.title, args.bundle, args.repo)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report_bug.py",
        description="Collect a redacted voice-loop diagnostics bundle, and (only when told to) file it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="write the bundle and print the exact bytes it contains")
    collect.add_argument("--out", default="", help="bundle path (default: the state dir, timestamped)")
    collect.add_argument("--summary", default="", help="the user's own description of what went wrong")
    collect.add_argument("--lines", type=int, default=LOG_TAIL_LINES, help=f"log lines per log (default {LOG_TAIL_LINES})")
    collect.add_argument("--json", action="store_true", help="print {path, digest} as JSON")
    collect.set_defaults(func=_cmd_collect)

    where = sub.add_parser("transports", help="which of the three report transports this machine has")
    where.add_argument("--json", action="store_true")
    where.set_defaults(func=_cmd_transports)

    url = sub.add_parser("url", help="print a pre-filled GitHub new-issue URL for a bundle")
    url.add_argument("--title", required=True)
    url.add_argument("--bundle", required=True)
    url.set_defaults(func=_cmd_url)

    mail = sub.add_parser("mailto", help="print a mailto: URL for a bundle")
    mail.add_argument("--title", required=True)
    mail.add_argument("--bundle", required=True)
    mail.add_argument("--to", default="", help=f"override the service mailbox (default: ${_MAILBOX_ENV})")
    mail.set_defaults(func=_cmd_mailto)

    issue = sub.add_parser("gh", help="file the bundle as an issue with the gh CLI — SENDS, ask first")
    issue.add_argument("--title", required=True)
    issue.add_argument("--bundle", required=True)
    issue.add_argument("--repo", default=REPO)
    issue.set_defaults(func=_cmd_gh)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except OSError as err:  # a missing bundle file, an unwritable state dir — say which, not a traceback
        print(f"{type(err).__name__}: {redact(str(err))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
