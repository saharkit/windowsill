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
* echo guard — starting a recording first stops any in-flight speak playback, so the microphone
  never records our own speakers (windowsill#3). Same pattern-scope as the shell version:
  pkill -f "voice-loop-speak" matches speak's player children by their temp-WAV path.
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
* keys — the cloud API key comes from ``key_file`` (wins) or the named env var, is used only as
  an in-process HTTP header, and NEVER appears in argv, in the config, or in the log.
"""

from __future__ import annotations

import json
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

# Every recorder in the table records 16 kHz mono S16 — 32000 bytes of PCM per second. The clip
# guard converts the WAV's size to seconds with this constant, so the two must move together.
RECORD_RATE = 16000
BYTES_PER_SECOND = RECORD_RATE * 2
WAV_HEADER_BYTES = 44
# Below this length there is no phrase, only a bounced hotkey; STT is skipped entirely.
MIN_CLIP_SECONDS = 0.3

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
_LOG_PATH = os.path.join(_STATE_DIR, "dictate.log")
_PID_PATH = os.path.join(_STATE_DIR, "dictate.pid")
_WAV_PATH = os.path.join(_STATE_DIR, "dictate.wav")
_LAST_WAV_PATH = os.path.join(_STATE_DIR, "dictate-last.wav")


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
    except (OSError, ValueError):
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
    """Every knob dictate-toggle.sh honoured, same names, same defaults, same precedence."""
    return {
        "mode": str(cfg(config, "dictate.mode", "send")),
        "paste_key": str(cfg(config, "dictate.paste_key", "cmd+v" if system == "Darwin" else "ctrl+shift+v")),
        # the shell compared against the literal string "true"; JSON true and "true" both count —
        # and NOTHING else (not 1: `1 in (True,)` is true in Python, so the check is explicit)
        "auto_paste": cfg(config, "dictate.auto_paste", False) is True
        or cfg(config, "dictate.auto_paste", False) == "true",
        "recorder": str(cfg(config, "dictate.recorder", "auto")),
        "clipboard": str(cfg(config, "dictate.clipboard", "auto")),
        "start_sound": str(cfg(config, "dictate.start_sound", "")),
        "stop_sound": str(cfg(config, "dictate.stop_sound", "")),
        "player": str(cfg(config, "speak.player", "afplay" if system == "Darwin" else "aplay -q")),
        "backend": str(cfg(config, "stt.backend", "lan")),
        "endpoint": str(cfg(config, "stt.endpoint", "http://127.0.0.1:8355")),
        # top-level "language" is the one the user sets; ".stt.language" is the advanced escape
        # for people who dictate in one language and listen in another.
        "language": str(cfg(config, "stt.language", cfg(config, "language", "ru"))),
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
        except OSError:
            pass
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


# --- runtime glue --------------------------------------------------------------------------------


def note(message: str, system: str) -> None:
    """Desktop notification — best-effort, never fatal."""
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "voice-loop"'],
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


def start_recording(s: dict, system: str) -> int:
    # echo guard: never record our own speakers — stop any in-flight speak playback first
    # (windowsill#3); the pattern matches speak's temp-WAV path in its player's argv
    try:
        subprocess.run(
            ["pkill", "-u", str(os.getuid()), "-f", "voice-loop-speak"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass

    recorder = resolve_recorder(s["recorder"], system, shutil.which)
    argv = recorder_argv(recorder, system, _WAV_PATH)
    if not argv:
        note("no recorder found — install pw-record/arecord (Linux) or sox/ffmpeg (macOS)", system)
        log("no recorder available")
        return 1
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as errlog:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=errlog)
    except OSError as err:
        note("recorder failed to start", system)
        log(f"recorder failed: {err}")
        return 1
    try:
        with open(_PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
    except OSError:
        pass
    log(f"recording via {recorder} pid={proc.pid}")
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

    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            recorder_pid = int(fh.read().strip() or "-1")
    except (OSError, ValueError):
        recorder_pid = -1

    if _pid_alive(recorder_pid):
        return stop_and_transcribe(s, system, mode, recorder_pid)
    try:
        os.unlink(_PID_PATH)  # a stale pidfile is removed, never obeyed
    except OSError:
        pass
    return start_recording(s, system)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # a hotkey script: surface via the log, never a traceback into nowhere
        try:
            log(f"unexpected error: {sys.exc_info()[1]!r:.200}")
        except Exception:
            pass
        sys.exit(1)
