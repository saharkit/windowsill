"""Shared bounded readers for voice-loop's cross-script state contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

MAX_CONFIG_BYTES = 1 << 20
MAX_STATE_BYTES = 4 << 10


def config_path(environ=os.environ) -> str:
    """Return the one relocatable config path used by every voice-loop script."""
    return environ.get(
        "VOICE_LOOP_CONFIG",
        os.path.join(environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "voice-loop", "config.json"),
    )


def read_bounded_text(path: str, limit: int = MAX_CONFIG_BYTES) -> str:
    """Read UTF-8 text with one bounded read and a sentinel byte."""
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"over {limit} bytes")
    return raw.decode("utf-8")


def resolve_number(value, default, setting: str, log, *, minimum=None, maximum=None, integer=False):
    """Coerce a typed setting or log once and return its safe default."""
    try:
        number = int(value) if integer else float(value)
        valid = True
        if isinstance(number, float) and not __import__("math").isfinite(number):
            valid = False
        if minimum is not None and number < minimum:
            valid = False
        if maximum is not None and number > maximum:
            valid = False
        if not valid:
            raise ValueError
        return number
    except (TypeError, ValueError, OverflowError):
        log(f"{setting} rejected {value!r} — using default {default!r}")
        return default


def read_config(path: str):
    """Read JSON while preserving a non-object value for diagnostic callers."""
    return json.loads(read_bounded_text(path, MAX_CONFIG_BYTES))


def load_config(path: str, on_error=None, *, strict: bool = False) -> dict:
    """Read config with the normal zero-setup fallback.

    ``strict`` is for contour_poll, whose caller must distinguish a broken monitor config from an
    absent one. Other callers retain their established ``{}`` fallback and optional diagnostic
    callback for malformed input.
    """
    try:
        loaded = read_config(path)
        if not isinstance(loaded, dict):
            if strict:
                raise ValueError(f"must hold a JSON object, not {type(loaded).__name__}")
            return {}
        return loaded
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as err:
        if strict:
            raise
        if on_error is not None:
            on_error(err)
        return {}


@dataclass(frozen=True)
class PlayingPid:
    """The identities recorded in ``playing.pid``; process groups are never ordinary PIDs."""

    pids: tuple[int, ...]
    pgids: tuple[int, ...]


def read_playing_pid(path: str) -> PlayingPid | None:
    """Parse the bounded ``playing.pid`` grammar, returning None for absent/bad records."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_STATE_BYTES:
        return None
    try:
        tokens = raw.decode("ascii").split()
    except UnicodeDecodeError:
        return None
    pids: list[int] = []
    pgids: list[int] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "pg":
            if index + 1 >= len(tokens):
                index += 1
                continue
            value = tokens[index + 1]
            index += 2
            if not value.isdigit() or int(value) <= 0:
                continue
            pgids.append(int(value))
            continue
        if token.isdigit() and int(token) > 0:
            pids.append(int(token))
        index += 1
    return PlayingPid(tuple(pids), tuple(pgids))


def _cmdline_of(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read(MAX_STATE_BYTES + 1)
    except OSError:
        return None
    raw = raw[:MAX_STATE_BYTES]
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _ps_cmdline_of(pid: int) -> str | None:
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


def _windows_process_is_live(pid: int) -> bool:
    """Probe a Windows process without os.kill(pid, 0)."""
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        return False


def pid_looks_like_speak(pid: int, read_cmdline=_cmdline_of, platform_id: str | None = None) -> bool:
    """Check a recorded process against the voice-loop speaking identity seam."""
    default_platform = platform_id is None
    platform_id = sys.platform if default_platform else platform_id
    if platform_id == "win32":
        return default_platform and _windows_process_is_live(pid)
    if platform_id.startswith("linux"):
        cmdline = read_cmdline(pid)
    elif platform_id == "darwin":
        cmdline = _ps_cmdline_of(pid) if read_cmdline is _cmdline_of else read_cmdline(pid)
    else:
        return False
    return cmdline is not None and ("voice-loop-speak" in cmdline or "speak.py" in cmdline)
