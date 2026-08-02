"""The dictation toggle's pure functions: config precedence, the recorder/clipboard/paste tables,
the min-clip guard, multipart building, response parsing — plus the audit-hardening seams: the
atomic pidfile claim (concurrent-toggle race), the pidfile-driven echo guard with its PID-reuse
identity check, the urllib-level cloud/LAN STT request shapes, and a real multipart round-trip
through the server's /stt.

dictate.py is glue around a recorder subprocess, an HTTP STT call and desktop paste tools, so its
full runtime contract is proven by real invocation (see TESTING.md). What is tested here never
reaches the network, a microphone, or the live state dir: every state path is monkeypatched into
tmp_path, every HTTP opener is faked at the urllib seam, and the only real subprocesses are
short-lived pythons owned by the tests themselves.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_DICTATE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "voice-loop" / "scripts" / "dictate.py"
_spec = importlib.util.spec_from_file_location("dictate", _DICTATE_PATH)
dictate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dictate)


def have_none(_name: str) -> bool:
    return False


def have(*names: str):
    return lambda name: name in names


@pytest.fixture
def state(monkeypatch, tmp_path):
    """Every state-dir path the script writes, owned by the test — never the live ~/.local/state."""
    monkeypatch.setattr(dictate, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dictate, "_LOG_PATH", str(tmp_path / "dictate.log"))
    monkeypatch.setattr(dictate, "_PID_PATH", str(tmp_path / "dictate.pid"))
    monkeypatch.setattr(dictate, "_WAV_PATH", str(tmp_path / "dictate.wav"))
    monkeypatch.setattr(dictate, "_LAST_WAV_PATH", str(tmp_path / "dictate-last.wav"))
    monkeypatch.setattr(dictate, "_SPEAK_PID_PATH", str(tmp_path / "playing.pid"))
    return tmp_path


# --- resolve_settings: the config-precedence table ----------------------------------------------


def test_defaults_with_empty_config_linux():
    s = dictate.resolve_settings({}, "Linux")
    assert s["mode"] == "send"
    assert s["paste_key"] == "ctrl+shift+v"
    assert s["auto_paste"] is False
    assert s["recorder"] == "auto"
    assert s["clipboard"] == "auto"
    assert s["player"] == "aplay -q"
    assert s["backend"] == "lan"
    assert s["endpoint"] == "http://127.0.0.1:8355"
    assert s["language"] == "en"  # explicit-language setups always write the key; the default is English
    assert s["stt_model"] == "whisper-1"
    assert s["stt_command"] == ""
    assert s["key_env"] == "VOICE_LOOP_STT_API_KEY"
    assert s["timeout"] == 60.0


def test_darwin_defaults_follow_the_platform():
    s = dictate.resolve_settings({}, "Darwin")
    assert s["paste_key"] == "cmd+v"
    assert s["player"] == "afplay"


@pytest.mark.parametrize("value", [True, "true"])
def test_auto_paste_accepts_json_true_and_the_shell_string(value):
    assert dictate.resolve_settings({"dictate": {"auto_paste": value}}, "Linux")["auto_paste"] is True


@pytest.mark.parametrize("value", [False, "false", "yes", 1])
def test_auto_paste_stays_off_for_anything_else(value):
    assert dictate.resolve_settings({"dictate": {"auto_paste": value}}, "Linux")["auto_paste"] is False


def test_stt_language_beats_top_level_language_beats_default():
    assert dictate.resolve_settings({"language": "ru"}, "Linux")["language"] == "ru"
    both = {"language": "ru", "stt": {"language": "de"}}
    assert dictate.resolve_settings(both, "Linux")["language"] == "de"


def test_key_env_precedence_cloud_over_stt_over_default():
    stt_level = {"stt": {"api_key_env": "STT_LEVEL"}}
    assert dictate.resolve_settings(stt_level, "Linux")["key_env"] == "STT_LEVEL"
    both = {"stt": {"api_key_env": "STT_LEVEL", "cloud": {"api_key_env": "CLOUD_LEVEL"}}}
    assert dictate.resolve_settings(both, "Linux")["key_env"] == "CLOUD_LEVEL"


def test_empty_string_falls_back_to_default():
    # bash-cfg parity: an empty value in the config behaves like an absent key
    assert dictate.resolve_settings({"stt": {"endpoint": ""}}, "Linux")["endpoint"] == "http://127.0.0.1:8355"


# --- read_key: key_file wins, whitespace stripped, never from argv ------------------------------


def test_key_file_wins_over_env(tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text(" sk-fromfile \n")
    assert dictate.read_key(str(key_file), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromfile"


def test_missing_key_file_falls_back_to_env(tmp_path):
    assert dictate.read_key(str(tmp_path / "absent"), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromenv"
    assert dictate.read_key("", "K_ENV", {}) == ""


# --- the recorder table: auto-selection and the exact device/format flags -----------------------


def test_explicit_recorder_is_taken_as_is():
    assert dictate.resolve_recorder("arecord", "Linux", have_none) == "arecord"


def test_linux_auto_prefers_pipewire_then_alsa_then_ffmpeg():
    assert dictate.resolve_recorder("auto", "Linux", have("pw-record", "arecord", "ffmpeg")) == "pw-record"
    assert dictate.resolve_recorder("auto", "Linux", have("arecord", "ffmpeg")) == "arecord"
    assert dictate.resolve_recorder("auto", "Linux", have("ffmpeg")) == "ffmpeg"
    assert dictate.resolve_recorder("auto", "Linux", have_none) == ""


def test_darwin_auto_prefers_sox_then_ffmpeg():
    assert dictate.resolve_recorder("auto", "Darwin", have("rec", "ffmpeg")) == "sox"
    assert dictate.resolve_recorder("auto", "Darwin", have("ffmpeg")) == "ffmpeg"
    assert dictate.resolve_recorder("auto", "Darwin", have_none) == ""


def test_recorder_argv_pins_16k_mono_s16():
    wav = "/state/dictate.wav"
    assert dictate.recorder_argv("pw-record", "Linux", wav) == [
        "pw-record", "--rate", "16000", "--channels", "1", wav
    ]
    assert dictate.recorder_argv("arecord", "Linux", wav) == [
        "arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", wav
    ]
    assert dictate.recorder_argv("sox", "Darwin", wav) == ["rec", "-q", "-r", "16000", "-c", "1", "-b", "16", wav]


def test_ffmpeg_argv_selects_the_platform_capture_device():
    linux = dictate.recorder_argv("ffmpeg", "Linux", "w.wav")
    mac = dictate.recorder_argv("ffmpeg", "Darwin", "w.wav")
    assert ["-f", "alsa", "-i", "default"] == linux[4:8]
    assert ["-f", "avfoundation", "-i", ":default"] == mac[4:8]
    for argv in (linux, mac):
        assert argv[-1] == "w.wav"
        assert ["-ar", "16000", "-ac", "1", "-y"] == argv[-6:-1]


def test_unknown_recorder_builds_no_argv():
    assert dictate.recorder_argv("parec", "Linux", "w.wav") == []


# --- the min-clip guard: byte math and the threshold --------------------------------------------


def test_clip_seconds_excludes_the_wav_header():
    assert dictate.clip_seconds(0) == 0.0
    assert dictate.clip_seconds(dictate.WAV_HEADER_BYTES) == 0.0
    assert dictate.clip_seconds(dictate.WAV_HEADER_BYTES + dictate.BYTES_PER_SECOND) == 1.0


def test_the_guard_threshold_splits_a_bounced_hotkey_from_a_word():
    just_under = dictate.WAV_HEADER_BYTES + int(dictate.MIN_CLIP_SECONDS * dictate.BYTES_PER_SECOND) - 1
    just_over = just_under + 2
    assert dictate.clip_seconds(just_under) < dictate.MIN_CLIP_SECONDS
    assert dictate.clip_seconds(just_over) >= dictate.MIN_CLIP_SECONDS


# --- the clipboard table ------------------------------------------------------------------------


def test_explicit_clipboard_is_taken_as_is():
    assert dictate.resolve_clipboard("xclip", "Linux", have_none, wayland=True) == "xclip"


def test_clipboard_auto_order():
    assert dictate.resolve_clipboard("auto", "Darwin", have_none, wayland=False) == "pbcopy"
    assert dictate.resolve_clipboard("auto", "Linux", have("wl-copy", "xclip"), wayland=True) == "wl-copy"
    assert dictate.resolve_clipboard("auto", "Linux", have("wl-copy", "xclip"), wayland=False) == "xclip"
    # installed wl-copy without $WAYLAND_DISPLAY still beats nothing (XWayland setups)
    assert dictate.resolve_clipboard("auto", "Linux", have("wl-copy"), wayland=False) == "wl-copy"
    assert dictate.resolve_clipboard("auto", "Linux", have_none, wayland=False) == ""


def test_clipboard_commands_fill_both_selections():
    assert dictate.clipboard_commands("pbcopy") == [["pbcopy"]]
    assert dictate.clipboard_commands("wl-copy") == [["wl-copy"], ["wl-copy", "--primary"]]
    assert dictate.clipboard_commands("xclip") == [
        ["xclip", "-selection", "clipboard"],
        ["xclip", "-selection", "primary"],
    ]
    assert dictate.clipboard_commands("") == []


# --- the paste table: tool pick and keystroke argv ----------------------------------------------


def test_paste_tool_pick_follows_the_platform_table():
    assert dictate.pick_paste_tool("Darwin", have("osascript"), False, "") == "osascript"
    assert dictate.pick_paste_tool("Darwin", have_none, False, "") == ""
    # ydotool needs its daemon socket, not just the binary
    assert dictate.pick_paste_tool("Linux", have("ydotool", "wtype"), True, "") == "ydotool"
    assert dictate.pick_paste_tool("Linux", have("ydotool", "wtype"), False, "") == "wtype"
    # xdotool is the X11 tail: only with a $DISPLAY
    assert dictate.pick_paste_tool("Linux", have("xdotool"), False, ":0") == "xdotool"
    assert dictate.pick_paste_tool("Linux", have("xdotool"), False, "") == ""


def test_osascript_plan_maps_the_paste_key_to_a_using_clause():
    steps = dictate.paste_plan("osascript", "ctrl+shift+v", enter=False)
    assert len(steps) == 1
    _, argv, required = steps[0]
    assert required is True
    assert "{control down, shift down}" in argv[-1]
    # no Insert key on mac keyboards — shift+insert becomes the native paste
    steps = dictate.paste_plan("osascript", "shift+insert", enter=False)
    assert "{command down}" in steps[0][1][-1]


def test_osascript_plan_presses_return_via_key_code_36():
    steps = dictate.paste_plan("osascript", "cmd+v", enter=True)
    assert len(steps) == 2
    delay, argv, required = steps[1]
    assert delay == 0.25
    assert "key code 36" in argv[-1]
    assert required is False  # best-effort, like the shell's || true


def test_ydotool_plan_uses_named_combos_with_the_settle_delays():
    steps = dictate.paste_plan("ydotool", "ctrl+shift+v", enter=True)
    assert steps[0] == (0.15, ["ydotool", "key", "ctrl+shift+v"], True)
    assert steps[1] == (0.25, ["ydotool", "key", "enter"], False)


def test_wtype_plan_presses_and_releases_modifiers():
    steps = dictate.paste_plan("wtype", "ctrl+shift+v", enter=False)
    assert steps[0][1] == ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]
    steps = dictate.paste_plan("wtype", "shift+insert", enter=True)
    assert steps[0][1] == ["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"]
    assert steps[1][1] == ["wtype", "-k", "Return"]
    fallback = dictate.paste_plan("wtype", "something-else", enter=False)
    assert fallback[0][1] == ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]


def test_xdotool_plan_capitalizes_insert():
    assert dictate.paste_plan("xdotool", "shift+insert", enter=False)[0][1] == ["xdotool", "key", "shift+Insert"]
    assert dictate.paste_plan("xdotool", "ctrl+v", enter=True)[1][1] == ["xdotool", "key", "Return"]


def test_unknown_paste_tool_builds_no_plan():
    assert dictate.paste_plan("", "ctrl+shift+v", enter=True) == []


# --- multipart building and response parsing ----------------------------------------------------


def test_multipart_form_frames_fields_then_the_wav():
    body = dictate.multipart_form({"model": "whisper-1"}, "file", "dictate.wav", b"RIFFwav", "BOUND")
    text_head, _, tail = body.partition(b"RIFFwav")
    assert b'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n' in text_head
    assert b'Content-Disposition: form-data; name="file"; filename="dictate.wav"\r\n' in text_head
    assert b"Content-Type: audio/wav\r\n\r\n" in text_head
    assert text_head.count(b"--BOUND\r\n") == 2
    assert tail == b"\r\n--BOUND--\r\n"


def test_multipart_form_with_no_fields_is_just_the_file_part():
    body = dictate.multipart_form({}, "audio", "dictate.wav", b"RIFFwav", "BOUND")
    assert body.startswith(b"--BOUND\r\n")
    assert body.count(b"--BOUND\r\n") == 1
    assert b'name="audio"' in body


def test_transcript_from_response_reads_text_and_strips():
    assert dictate.transcript_from_response(b'{"text": " hello world \\n", "language": "en"}') == "hello world"


def test_transcript_from_response_is_empty_on_anything_malformed():
    assert dictate.transcript_from_response(None) == ""
    assert dictate.transcript_from_response(b"") == ""
    assert dictate.transcript_from_response(b"<html>bad gateway</html>") == ""
    assert dictate.transcript_from_response(b'{"detail": "no text key"}') == ""
    assert dictate.transcript_from_response(b'["not", "a", "dict"]') == ""


# --- corrupt config / key files: ignored loudly, never a crash ----------------------------------


def test_corrupt_json_config_is_ignored_and_logged(state):
    bad = state / "config.json"
    bad.write_text("{not json", encoding="utf-8")
    assert dictate.load_config(str(bad)) == {}
    logged = (state / "dictate.log").read_text(encoding="utf-8")
    assert "config ignored" in logged and "JSONDecodeError" in logged


def test_non_utf8_config_is_ignored_and_logged(state):
    bad = state / "config.json"
    bad.write_bytes(b'\xff\xfe{"a": 1}')
    assert dictate.load_config(str(bad)) == {}
    assert "UnicodeDecodeError" in (state / "dictate.log").read_text(encoding="utf-8")


def test_absent_config_stays_silent(state):
    assert dictate.load_config(str(state / "absent.json")) == {}
    assert not (state / "dictate.log").exists()


def test_non_utf8_key_file_falls_back_to_env_and_never_logs_content(state):
    key_file = state / "k"
    key_file.write_bytes(b"\xff\xfe topsecretbytes")
    assert dictate.read_key(str(key_file), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromenv"
    logged = (state / "dictate.log").read_text(encoding="utf-8")
    assert "UnicodeDecodeError" in logged
    assert "topsecret" not in logged


# --- applescript_escape: config-controlled text cannot break out of the literal -----------------


def test_applescript_escape_handles_quotes_and_backslashes():
    assert dictate.applescript_escape('press "ctrl+v" to paste') == 'press \\"ctrl+v\\" to paste'
    assert dictate.applescript_escape("back\\slash") == "back\\\\slash"
    # backslash first, then the quote — the other order would double-escape
    assert dictate.applescript_escape('mix\\"ed') == 'mix\\\\\\"ed'
    assert dictate.applescript_escape("plain text") == "plain text"


def test_note_interpolates_the_escaped_message_on_darwin(monkeypatch):
    runs = []
    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: runs.append(argv))
    dictate.note('copied — press "ctrl+v" to paste', "Darwin")
    assert runs[0][:2] == ["osascript", "-e"]
    assert 'display notification "copied — press \\"ctrl+v\\" to paste" with title "voice-loop"' == runs[0][2]


# --- the PID-reuse identity check (duplicated helper — kept in sync with speak.py) --------------


def test_pid_identity_accepts_the_voice_loop_chain_on_linux():
    player = "aplay -q /tmp/voice-loop-speak-abc123"
    assert dictate.pid_looks_like_speak(9, read_cmdline=lambda pid: player, platform_id="linux") is True
    python_half = "python3 /repo/plugins/voice-loop/scripts/speak.py"
    assert dictate.pid_looks_like_speak(9, read_cmdline=lambda pid: python_half, platform_id="linux") is True


def test_pid_identity_rejects_a_reused_or_gone_pid_on_linux():
    assert dictate.pid_looks_like_speak(9, read_cmdline=lambda pid: "sshd: user@pts/0", platform_id="linux") is False
    assert dictate.pid_looks_like_speak(9, read_cmdline=lambda pid: None, platform_id="linux") is False


def test_pid_identity_check_is_skipped_off_linux():
    def never(pid):
        raise AssertionError("cmdline must not be read off Linux")

    assert dictate.pid_looks_like_speak(9, read_cmdline=never, platform_id="darwin") is True


# --- the echo guard: pidfile-scoped kills, pkill only as the no-pidfile fallback ----------------


def test_echo_guard_kills_exactly_the_pids_speak_recorded(state, monkeypatch):
    (state / "playing.pid").write_text("100 200", encoding="utf-8")
    monkeypatch.setattr(dictate, "pid_looks_like_speak", lambda pid: True)
    kills = []
    monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    def no_pkill(*args, **kwargs):
        raise AssertionError("the pkill fallback must not run when the pidfile exists")

    monkeypatch.setattr(dictate.subprocess, "run", no_pkill)
    dictate.stop_speak_playback()
    assert kills == [(100, dictate.signal.SIGTERM), (200, dictate.signal.SIGTERM)]


def test_echo_guard_skips_unverified_pids_and_garbage_tokens(state, monkeypatch):
    (state / "playing.pid").write_text(f"abc -5 0 {os.getpid()} 300 400", encoding="utf-8")
    monkeypatch.setattr(dictate, "pid_looks_like_speak", lambda pid: pid == 300)
    kills = []
    monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    dictate.stop_speak_playback()
    assert kills == [(300, dictate.signal.SIGTERM)]  # never itself, never an unverified pid


def test_echo_guard_falls_back_to_pkill_only_without_a_pidfile(state, monkeypatch):
    runs = []
    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: runs.append(argv))

    def no_kill(pid, sig):
        raise AssertionError("nothing to signal without a pidfile")

    monkeypatch.setattr(dictate.os, "kill", no_kill)
    dictate.stop_speak_playback()
    assert runs == [["pkill", "-u", str(os.getuid()), "-f", "voice-loop-speak"]]


# --- the concurrent-invocation race: one claim, one recorder ------------------------------------


class FakeProc:
    pid = 4242


def _write_config(monkeypatch, tmp_path, config: dict) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(cfg_file))


def test_claim_pidfile_admits_exactly_one_winner(state):
    barrier = threading.Barrier(8)
    results: list[int | None] = []

    def contend():
        barrier.wait(timeout=5)
        results.append(dictate.claim_pidfile())

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    winners = [fd for fd in results if fd is not None]
    assert len(results) == 8
    assert len(winners) == 1
    os.close(winners[0])


def test_concurrent_toggles_start_exactly_one_recorder(state, monkeypatch):
    """The injected race window: both invocations pass the pidfile-liveness check and reach the
    claim together — the O_EXCL create is the only arbiter, and it must pick exactly one."""
    barrier = threading.Barrier(2)
    real_claim = dictate.claim_pidfile

    def racing_claim():
        barrier.wait(timeout=5)
        return real_claim()

    monkeypatch.setattr(dictate, "claim_pidfile", racing_claim)
    spawned: list[list[str]] = []
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    notes: list[str] = []
    monkeypatch.setattr(dictate, "note", lambda message, system: notes.append(message))
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord"}})

    rcs: list[int] = []
    threads = [threading.Thread(target=lambda: rcs.append(dictate.main(["dictate.py"]))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(rcs) == [0, 0]
    assert len(spawned) == 1  # the loser spawned NOTHING onto the shared WAV
    assert (state / "dictate.pid").read_text(encoding="utf-8") == "4242"
    assert sum("already starting" in message for message in notes) == 1
    assert notes.count("recording…") == 1


def test_a_claim_in_progress_is_not_adopted_or_cleared(state, monkeypatch):
    # a freshly created, still-empty pidfile IS another invocation mid-claim: lose politely
    (state / "dictate.pid").write_text("", encoding="utf-8")

    def no_spawn(*args, **kwargs):
        raise AssertionError("the losing invocation must not start a recorder")

    monkeypatch.setattr(dictate.subprocess, "Popen", no_spawn)
    notes: list[str] = []
    monkeypatch.setattr(dictate, "note", lambda message, system: notes.append(message))
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(state / "absent.json"))
    assert dictate.main(["dictate.py"]) == 0
    assert (state / "dictate.pid").exists()  # the winner's claim was left alone
    assert any("already starting" in message for message in notes)


def test_dead_garbage_claim_older_than_the_grace_window_is_cleared(state, monkeypatch):
    pidfile = state / "dictate.pid"
    pidfile.write_text("not-a-pid", encoding="utf-8")
    stale = time.time() - 60
    os.utime(pidfile, (stale, stale))
    spawned: list[list[str]] = []
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord"}})
    assert dictate.main(["dictate.py"]) == 0
    assert len(spawned) == 1
    assert pidfile.read_text(encoding="utf-8") == "4242"


def test_a_stale_dead_pid_is_removed_and_the_start_proceeds(state, monkeypatch):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])  # real child, before Popen is faked
    dead.wait()
    (state / "dictate.pid").write_text(str(dead.pid), encoding="utf-8")
    spawned: list[list[str]] = []
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord"}})
    assert dictate.main(["dictate.py"]) == 0
    assert len(spawned) == 1
    assert (state / "dictate.pid").read_text(encoding="utf-8") == "4242"


def test_start_failure_releases_the_claim(state, monkeypatch):
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    fd = dictate.claim_pidfile()
    assert fd is not None
    s = dictate.resolve_settings({"dictate": {"recorder": "no-such-recorder"}}, "Linux")
    assert dictate.start_recording(s, "Linux", fd) == 1  # unknown recorder -> no argv
    assert not (state / "dictate.pid").exists()  # the claim was released, the toggle is not wedged


def test_stop_removes_the_pidfile_before_signalling(state, monkeypatch):
    pidfile = state / "dictate.pid"
    pidfile.write_text("12345", encoding="utf-8")
    seen: list[tuple[int, int, bool]] = []

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError  # _pid_alive: the recorder is already gone
        seen.append((pid, sig, pidfile.exists()))

    monkeypatch.setattr(dictate.os, "kill", fake_kill)
    monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    s = dictate.resolve_settings({}, "Linux")
    assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0
    # the slot was freed BEFORE the signal: a racing start claims fresh, never adopts a dying recorder
    assert seen == [(12345, dictate.signal.SIGINT, False)]


# --- the cloud/LAN STT request shapes, mocked at the urllib seam --------------------------------


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[tuple[object, float | None]] = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return FakeResponse(self.body)


@pytest.fixture
def opener(monkeypatch):
    holder: dict[str, FakeOpener] = {}
    monkeypatch.setattr(dictate.urllib.request, "build_opener", lambda *handlers: holder["opener"])

    def install(body: bytes) -> FakeOpener:
        holder["opener"] = FakeOpener(body)
        return holder["opener"]

    return install


def test_cloud_stt_posts_the_documented_openai_transcription_shape(state, monkeypatch, opener):
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": " hello "}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings({"stt": {"backend": "cloud", "endpoint": "https://api.example.com"}}, "Linux")
    assert dictate.transcribe(s) == "hello"

    request, timeout = fake.requests[0]
    assert request.full_url == "https://api.example.com/v1/audio/transcriptions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer sk-secret"
    assert timeout == 60.0
    content_type = request.get_header("Content-type")
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode("ascii")
    body = request.data
    assert body.count(b"--" + boundary + b"\r\n") == 3  # model, language, file
    assert b'name="model"\r\n\r\nwhisper-1\r\n' in body
    assert b'name="language"\r\n\r\nen\r\n' in body
    assert b'name="file"; filename="dictate.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFFfakewav' in body
    assert body.endswith(b"\r\n--" + boundary + b"--\r\n")


def test_lan_stt_posts_the_audio_field_with_a_language_query(state, opener):
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "ok"}')
    s = dictate.resolve_settings({}, "Linux")
    assert dictate.transcribe(s) == "ok"
    request, _ = fake.requests[0]
    assert request.full_url == "http://127.0.0.1:8355/stt?language=en"
    assert request.get_header("Authorization") is None  # the LAN server never sees a key
    assert b'name="audio"; filename="dictate.wav"' in request.data


# --- the cross-module framing loop: dictate's multipart through the real /stt -------------------


def test_multipart_round_trip_through_the_server_stt(client, fake_whisper):
    boundary = "roundtripboundary"
    body = dictate.multipart_form({}, "audio", "dictate.wav", b"RIFF-not-really-wav", boundary)
    response = client.post(
        "/stt?language=en",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "hello world"
    assert fake_whisper.calls[0]["language"] == "en"
