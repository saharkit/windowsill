#!/usr/bin/env python3
"""voice-loop — Stop hook: speak the assistant's marker-tagged lines.

This is the hook's whole logic; ``scripts/speak.sh`` is only a thin launcher (hooks.json keeps
invoking the .sh so the registration surface never changes). Stdlib only, Python 3.10+.

Convention: only lines whose first non-space character is the marker (default 🔊) are voiced;
everything else stays text. The model decides what is worth hearing.

Reads ~/.config/voice-loop/config.json. Never fails a turn: every path exits 0.

Deliberate behaviours, found by live debugging — do not "simplify" them away:

* flush race — Stop can fire BEFORE the final assistant message is written to the transcript.
  The transcript is read IMMEDIATELY; a retry happens only on the two real race signatures — an
  EMPTY extract, or an extract IDENTICAL to the previously spoken line — with adaptive backoff
  (0.15 → 1.0 s), so an already-flushed transcript costs zero sleep.
* dedup — a same-as-last read IS the stale previous turn, so it is dropped, not spoken twice.
* takeover — a fresher hook invocation supersedes a still-playing older one. Scoped precisely:
  the speaking chain records its PIDs (this process + the current player/command child) in a
  pidfile, and a new invocation SIGTERMs exactly those — nothing pattern-matched, nothing else.
* streaming — the marked text is split into sentence chunks (tiny sentences merged so a chunk is
  at least ~MIN_CHUNK_CHARS chars); chunk 1 starts playing as soon as IT is synthesized, and the
  next chunk synthesizes while the previous one plays. Perceived latency is one small synthesis,
  not the whole message.
* keys — the cloud API key comes from ``key_file`` (wins) or the named env var, is used only as an
  in-process HTTP header, and NEVER appears in argv, in the config, or in the log.
* timing — every spoken run logs ``timings extract_ms=… first_audio_ms=… total_ms=…`` so latency
  changes are measurable from the state log, before vs after.
"""

from __future__ import annotations

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

# Retry backoff for the flush race: adaptive, front-loaded — most races resolve within the first
# fraction of a second, so we probe early instead of sleeping a flat 5 x 0.7 s tail.
BACKOFF = (0.15, 0.3, 0.5, 0.7, 1.0)

# Streaming chunks below this length gain nothing (player spawn overhead dominates), so tiny
# sentences are merged up to at least this many characters.
MIN_CHUNK_CHARS = 40

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
_LOG_PATH = os.path.join(_STATE_DIR, "speak.log")
_LAST_PATH = os.path.join(_STATE_DIR, "last-spoken")
_PID_PATH = os.path.join(_STATE_DIR, "playing.pid")

# state the SIGTERM handler (takeover by a fresher invocation) must be able to reach
_live: dict = {"proc": None, "files": set()}


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
    """Every knob speak.sh honoured, same names, same defaults, same precedence."""
    speaker = str(cfg(config, "tts.speaker", ""))
    provider = str(cfg(config, "tts.cloud.provider", "openai"))
    default_model = "eleven_multilingual_v2" if provider == "elevenlabs" else "tts-1"
    voice_settings = cfg(config, "tts.cloud.voice_settings", None)
    return {
        "enabled": cfg(config, "speak.enabled", True) not in (False, "false"),
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
        "language": str(cfg(config, "tts.language", cfg(config, "language", "ru"))),
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
        except OSError:
            pass
    return environ.get(key_env, "")


def extract_from_lines(lines, marker: str, limit: int) -> str:
    """Marker-tagged text of the LAST assistant message, joined and clipped — '' when none."""
    last = None
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
            last = "\n".join(parts)
    if not last:
        return ""
    out = [ln.lstrip()[len(marker):].strip() for ln in last.splitlines() if ln.lstrip().startswith(marker)]
    return " ".join(x for x in out if x)[:limit]


def extract(path: str, marker: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return extract_from_lines(fh, marker, limit)
    except OSError:
        return ""


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


def take_over() -> None:
    """A fresher line supersedes a still-playing older one: SIGTERM exactly the PIDs the previous
    chain recorded (its python process + its current player child) — nothing else."""
    try:
        with open(_PID_PATH, encoding="utf-8") as fh:
            pids = [int(tok) for tok in fh.read().split()]
    except (OSError, ValueError):
        return
    for pid in pids:
        if pid and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def _on_sigterm(signum, frame):  # noqa: ARG001 — signal-handler signature
    """We were superseded (or the harness timed us out): stop the player, drop temp files, exit 0."""
    proc = _live.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    for path in list(_live["files"]):
        try:
            os.unlink(path)
        except OSError:
            pass
    os._exit(0)


def _play_stream(chunks: list[str], s: dict, key: str) -> tuple[int, int, int, int | None]:
    """Synthesize chunk N+1 while chunk N plays. One player subprocess per chunk; the next Popen is
    issued the moment the previous .wait() returns, so the only gap is process spawn.

    Returns (chunks_played, total_bytes, first_audio_ms_offset, last_rc)."""
    player_argv = shlex.split(s["player"])
    proc: subprocess.Popen | None = None
    proc_wav: str | None = None
    played = 0
    total_bytes = 0
    first_audio_at: float | None = None
    rc: int | None = None
    start = time.monotonic()

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
        for part in chunks:
            audio = synthesize(part, s, key)  # overlaps with the previous chunk's playback
            if audio is None:
                break
            fd, wav = tempfile.mkstemp(prefix="voice-loop-speak-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(audio)
            _live["files"].add(wav)
            reap()  # let the previous chunk finish before starting this one
            if first_audio_at is None:
                first_audio_at = time.monotonic()
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

    first_ms = -1 if first_audio_at is None else int((first_audio_at - start) * 1000)
    return played, total_bytes, first_ms, rc


def main() -> int:
    t0 = time.monotonic()
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
    except OSError:
        pass

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
        return 0
    transcript = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not transcript or not os.path.isfile(transcript):
        return 0

    try:
        with open(_LAST_PATH, encoding="utf-8") as fh:
            prev = fh.read()
    except OSError:
        prev = ""

    # Flush race: read immediately; retry ONLY on the race signatures (empty, or same-as-last).
    text = extract(transcript, s["marker"], s["max_chars"])
    for pause in BACKOFF:
        if text and text != prev:
            break
        time.sleep(pause)
        text = extract(transcript, s["marker"], s["max_chars"])
    if not text:
        return 0
    if text == prev:  # dedup: the stale previous turn, dropped, not spoken twice
        return 0
    extract_ms = int((time.monotonic() - t0) * 1000)

    try:
        with open(_LAST_PATH, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
    log(f"text: {text[:80]}")

    signal.signal(signal.SIGTERM, _on_sigterm)
    take_over()
    _write_pidfile(os.getpid())

    try:
        if s["command"]:
            # tts.command: speak locally without any server (e.g. "say -v Milena" on macOS, or a
            # piper pipeline). The command receives the text on stdin and produces the sound itself
            # — synthesis and playback are one opaque step, so there is nothing to pipeline: the
            # whole text goes in one call.
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
                return 0
            _live["proc"] = proc
            _write_pidfile(os.getpid(), proc.pid)
            proc.communicate(input=text.encode("utf-8"))
            _live["proc"] = None
            log(f"local command rc={proc.returncode}")
            total_ms = int((time.monotonic() - t0) * 1000)
            log(f"timings extract_ms={extract_ms} first_audio_ms={first_ms} total_ms={total_ms}")
            return 0

        key = ""
        if s["backend"] == "cloud":
            key = read_key(s["key_file"], s["key_env"], os.environ)
            if not key:
                log(f"cloud tts: no key (key_file unset/unreadable and ${s['key_env']} empty)")
                return 0

        played, total_bytes, first_offset_ms, rc = _play_stream(chunk_sentences(text), s, key)
        total_ms = int((time.monotonic() - t0) * 1000)
        first_ms = -1 if first_offset_ms < 0 else extract_ms + first_offset_ms
        if played:
            log(f"played rc={rc} bytes={total_bytes} chunks={played}")
        log(f"timings extract_ms={extract_ms} first_audio_ms={first_ms} total_ms={total_ms}")
        return 0
    finally:
        try:
            with open(_PID_PATH, encoding="utf-8") as fh:
                if fh.read().split()[:1] == [str(os.getpid())]:
                    os.unlink(_PID_PATH)
        except (OSError, IndexError):
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # never fail the turn — a hook error must not surface into the session
        try:
            log(f"unexpected error: {sys.exc_info()[1]!r:.200}")
        except Exception:
            pass
        sys.exit(0)
