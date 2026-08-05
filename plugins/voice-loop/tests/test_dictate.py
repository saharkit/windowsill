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

_DICTATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dictate.py"
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
    monkeypatch.setattr(dictate, "_TOGGLE_PATH", str(tmp_path / "dictate-last-toggle"))
    monkeypatch.setattr(dictate, "_FOCUS_PATH", str(tmp_path / "dictate-focus"))
    monkeypatch.setattr(dictate, "_PASTE_DENIED_PATH", str(tmp_path / "dictate-paste-denied"))
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
    assert s["debounce_ms"] == 750.0
    assert s["paste_target"] == "any"  # the power behaviour stays the default; the guard is opt-in


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


# --- paste_target: the same-window guard --------------------------------------------------------


def test_paste_target_resolves_the_two_documented_values(state):
    assert dictate.resolve_settings({}, "Linux")["paste_target"] == "any"
    assert dictate.resolve_settings({"dictate": {"paste_target": "any"}}, "Linux")["paste_target"] == "any"
    same = dictate.resolve_settings({"dictate": {"paste_target": "same-window"}}, "Linux")
    assert same["paste_target"] == "same-window"
    assert not (state / "dictate.log").exists()  # a known value is not worth a line


@pytest.mark.parametrize("bad", ["same_window", "samewindow", "window", True, 1])
def test_an_unknown_paste_target_resolves_to_the_cautious_side_and_says_so(state, bad):
    """The one setting whose typo does NOT fall back to the default: "any" is what an absent key
    already means, so anybody who wrote this key wrote it to ask for the guard. Falling back to the
    default would hand them the exact behaviour they were switching off, silently."""
    assert dictate.resolve_settings({"dictate": {"paste_target": bad}}, "Linux")["paste_target"] == "same-window"
    assert "not a known value" in (state / "dictate.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("unset", ["", None])
def test_an_absent_or_empty_paste_target_is_the_default_not_a_typo(state, unset):
    # bash-cfg parity: cfg() treats both as "unset", so the resolver never sees them
    assert dictate.resolve_settings({"dictate": {"paste_target": unset}}, "Linux")["paste_target"] == "any"
    assert not (state / "dictate.log").exists()


def test_focus_probe_asks_system_events_on_macos():
    argv = dictate.focus_probe_argv("Darwin", have("osascript"), wayland=False, display="")
    assert argv[:2] == ["osascript", "-e"]
    assert "frontmost is true" in argv[2]
    assert dictate.focus_probe_argv("Darwin", have_none, wayland=False, display="") == []


def test_focus_probe_uses_xdotool_on_x11_only():
    assert dictate.focus_probe_argv("Linux", have("xdotool"), wayland=False, display=":0") == [
        "xdotool",
        "getactivewindow",
    ]
    assert dictate.focus_probe_argv("Linux", have("xdotool"), wayland=False, display="") == []  # no X11 session
    assert dictate.focus_probe_argv("Linux", have_none, wayland=False, display=":0") == []


def test_focus_is_unknowable_on_wayland_even_with_xdotool_installed():
    """XWayland's xdotool answers for the X subset only: after a switch to a native Wayland window
    it reports the previous id, so it would suppress the paste exactly when focus did NOT move and
    allow it when it did. No answer beats a wrong one — the guard degrades to "any" instead."""
    assert dictate.focus_probe_argv("Linux", have("xdotool"), wayland=True, display=":0") == []


def test_focus_changed_only_when_both_ends_are_known_and_differ():
    assert dictate.focus_changed("claude", "slack") is True
    assert dictate.focus_changed("claude", "claude") is False
    # an unknown at either end is an unanswered question, and that degrades to "any": paste
    assert dictate.focus_changed("", "slack") is False
    assert dictate.focus_changed("claude", "") is False
    assert dictate.focus_changed("", "") is False


def test_the_guard_is_scoped_to_the_auto_paste_tier():
    """On the clipboard tier the human presses the paste key themselves, in whatever window they
    meant — there is nothing to guard, and no reason to spend a probe on every recording."""
    on = {"auto_paste": True, "paste_target": "same-window"}
    assert dictate.same_window_guard_on(on) is True
    assert dictate.same_window_guard_on({**on, "auto_paste": False}) is False
    assert dictate.same_window_guard_on({**on, "paste_target": "any"}) is False


def test_current_focus_returns_the_probe_output_stripped(state, monkeypatch):
    monkeypatch.setattr(dictate.shutil, "which", have("xdotool"))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    calls: list[tuple[list[str], float]] = []

    class Done:
        returncode = 0
        stdout = b" 44040199 \n"

    def fake_run(argv, **kw):
        calls.append((argv, kw["timeout"]))
        assert kw["check"] is False and kw["capture_output"] is True  # bounded, argv-only, no raise
        return Done()

    monkeypatch.setattr(dictate.subprocess, "run", fake_run)
    assert dictate.current_focus("Linux") == "44040199"
    assert calls == [(["xdotool", "getactivewindow"], dictate.FOCUS_PROBE_TIMEOUT)]


def test_current_focus_is_unknown_without_a_probe(state, monkeypatch):
    def no_spawn(*args, **kwargs):
        raise AssertionError("no probe exists on this platform — nothing may be spawned")

    monkeypatch.setattr(dictate.subprocess, "run", no_spawn)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert dictate.current_focus("Linux") == ""


@pytest.mark.parametrize(
    "outcome",
    [
        OSError("no such tool"),
        subprocess.TimeoutExpired(cmd="xdotool", timeout=2.0),  # a wedged probe is not a wedged toggle
        "nonzero",
    ],
)
def test_a_failed_probe_is_an_unknown_focus_not_a_crash(state, monkeypatch, outcome):
    monkeypatch.setattr(dictate.shutil, "which", have("xdotool"))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    class Failed:
        returncode = 1
        stdout = b""

    def fake_run(argv, **kw):
        if outcome == "nonzero":
            return Failed()
        raise outcome

    monkeypatch.setattr(dictate.subprocess, "run", fake_run)
    assert dictate.current_focus("Linux") == ""
    assert (state / "dictate.log").exists()  # unknown, but never silently


def test_the_start_focus_is_written_and_then_consumed_by_the_stop(state, monkeypatch):
    monkeypatch.setattr(dictate, "current_focus", lambda system: "Claude Code")
    dictate.remember_focus("Linux")
    assert (state / "dictate-focus").read_text(encoding="utf-8") == "Claude Code"
    assert dictate.take_remembered_focus() == "Claude Code"
    # consumed: an identity must never outlive its own recording
    assert not (state / "dictate-focus").exists()
    assert dictate.take_remembered_focus() == ""


def test_an_unwritable_focus_stamp_degrades_to_paste_at_focus(state, monkeypatch):
    monkeypatch.setattr(dictate, "current_focus", lambda system: "Claude Code")
    monkeypatch.setattr(dictate, "_FOCUS_PATH", str(state / "not-created-yet" / "dictate-focus"))
    dictate.remember_focus("Linux")  # fails open, exactly like the debounce stamp
    assert dictate.take_remembered_focus() == ""
    assert "focus not recorded" in (state / "dictate.log").read_text(encoding="utf-8")


class PasteRun:
    """What a stop that reached the paste decision actually did."""

    def __init__(self) -> None:
        self.clipboard: list[bytes] = []
        self.pastes: list[str] = []
        self.notes: list[str] = []


@pytest.fixture
def paste_run(state, monkeypatch):
    """Everything before the paste decision, out of the way: a clip past the min-clip guard, a
    transcript, a recorder already gone, a fake clipboard and a fake paste tool."""
    (state / "dictate.wav").write_bytes(b"\0" * (dictate.WAV_HEADER_BYTES + dictate.BYTES_PER_SECOND))
    run = PasteRun()
    monkeypatch.setattr(dictate, "transcribe", lambda s: "hello agent")
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: run.notes.append(message))
    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: run.clipboard.append(kw["input"]))
    monkeypatch.setattr(
        dictate, "_run_paste", lambda tool, paste_key, enter, sock: run.pastes.append(paste_key) or True
    )
    return run


def _guarded(**overrides) -> dict:
    config = {"auto_paste": True, "paste_target": "same-window", "clipboard": "xclip", **overrides}
    return dictate.resolve_settings({"dictate": config}, "Linux")


def test_a_window_switch_mid_sentence_keeps_the_text_in_the_clipboard(state, monkeypatch, paste_run):
    """The reported footgun, guarded: dictation starts in the agent, the human switches to a chat
    while speaking, and the transcript must not be typed into that chat."""
    (state / "dictate-focus").write_text("Claude Code", encoding="utf-8")
    monkeypatch.setattr(dictate, "current_focus", lambda system: "Slack")
    assert dictate.stop_and_transcribe(_guarded(), "Linux", "send", 12345) == 0
    assert paste_run.pastes == []  # nothing was typed anywhere
    assert paste_run.clipboard == [b"hello agent", b"hello agent"]  # both selections, as always
    assert paste_run.notes[-1] == "focus moved — text is in the clipboard"
    assert "paste suppressed" in (state / "dictate.log").read_text(encoding="utf-8")


def test_the_guard_pastes_when_focus_stayed_put(state, monkeypatch, paste_run):
    (state / "dictate-focus").write_text("Claude Code", encoding="utf-8")
    monkeypatch.setattr(dictate, "current_focus", lambda system: "Claude Code")
    assert dictate.stop_and_transcribe(_guarded(), "Linux", "send", 12345) == 0
    assert paste_run.pastes == ["ctrl+shift+v"]


def test_an_unknowable_focus_pastes_rather_than_wedging_dictation(state, monkeypatch, paste_run):
    """Wayland, a missing xdotool, a probe that failed: the guard degrades to "any" — the whole
    point of the documented fallback. Suppressing here would be dictation that stops pasting on a
    whole class of desktops with a notification claiming focus moved when it did not."""
    monkeypatch.setattr(dictate, "current_focus", lambda system: "")
    assert dictate.stop_and_transcribe(_guarded(), "Linux", "send", 12345) == 0
    assert paste_run.pastes == ["ctrl+shift+v"]


def test_the_default_target_never_probes_focus_at_all(state, monkeypatch, paste_run):
    (state / "dictate-focus").write_text("Claude Code", encoding="utf-8")

    def never(system):
        raise AssertionError('paste_target "any" must not probe focus')

    monkeypatch.setattr(dictate, "current_focus", never)
    assert dictate.stop_and_transcribe(_guarded(paste_target="any"), "Linux", "send", 12345) == 0
    assert paste_run.pastes == ["ctrl+shift+v"]
    assert not (state / "dictate-focus").exists()  # consumed anyway: no identity outlives its run


def test_a_stop_that_never_reaches_the_paste_decision_still_consumes_the_focus(state, monkeypatch):
    """A clip below the min-clip guard returns early. If the identity survived that, the NEXT
    recording (started while the guard was off, so writing nothing) would be compared against a
    window from minutes ago and suppressed for no reason."""
    (state / "dictate-focus").write_text("Claude Code", encoding="utf-8")
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    assert dictate.stop_and_transcribe(_guarded(), "Linux", "send", 12345) == 0  # no WAV: clip too short
    assert not (state / "dictate-focus").exists()


def test_start_records_the_focus_only_for_a_guarded_run(state, monkeypatch):
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    monkeypatch.setattr(dictate, "current_focus", lambda system: "Claude Code")

    unguarded = dictate.resolve_settings({"dictate": {"recorder": "arecord", "auto_paste": True}}, "Linux")
    assert dictate.start_recording(unguarded, "Linux", dictate.claim_pidfile()) == 0
    assert not (state / "dictate-focus").exists()

    os.unlink(state / "dictate.pid")
    assert dictate.start_recording(_guarded(recorder="arecord"), "Linux", dictate.claim_pidfile()) == 0
    assert (state / "dictate-focus").read_text(encoding="utf-8") == "Claude Code"


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


# --- paste denial: macOS Accessibility permission detection and graceful degradation -------------


def test_paste_denied_returns_false_when_marker_absent(state):
    """An invocation that has never seen a denial is not blocked."""
    assert dictate._paste_denied() is False


def test_paste_denied_returns_true_when_marker_exists(state):
    (state / "dictate-paste-denied").write_text("denied", encoding="utf-8")
    assert dictate._paste_denied() is True


def test_mark_paste_denied_creates_the_marker(state):
    assert not (state / "dictate-paste-denied").exists()
    dictate._mark_paste_denied()
    assert (state / "dictate-paste-denied").exists()


def test_mark_paste_denied_is_best_effort_on_unwritable_state_dir(state, monkeypatch):
    monkeypatch.setattr(dictate, "_PASTE_DENIED_PATH", str(state / "not-created" / "denied"))
    dictate._mark_paste_denied()  # does not raise
    assert not (state / "not-created").exists()


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (b"execution error: System Events got an error: osascript is not allowed to send keystrokes. (-1743)", True),
        (b"osascript is not allowed to send keystrokes. (1002)", True),
        (b"Not authorized to send Apple events to System Events.", True),
        (b"", False),
        (b"some other error", False),
        (b"env: osascript: No such file or directory", False),
    ],
)
def test_is_accessibility_denial_matches_the_known_phrases(stderr, expected):
    assert dictate._is_accessibility_denial(stderr) is expected


def test_is_accessibility_denial_is_case_insensitive():
    assert dictate._is_accessibility_denial(b"NOT ALLOWED TO SEND KEYSTROKES") is True


def test_run_paste_skips_osascript_when_denial_is_marked(state, monkeypatch):
    """A toggle after a denial skips the keystroke path entirely — no subprocess is run."""
    (state / "dictate-paste-denied").write_text("denied", encoding="utf-8")
    spawned = []

    def no_spawn(*_args, **_kw):
        raise AssertionError("a denied paste must not spawn osascript")

    monkeypatch.setattr(dictate.subprocess, "run", no_spawn)
    assert dictate._run_paste("osascript", "cmd+v", enter=False, ydotool_socket="") is False


def test_run_paste_marks_denial_on_accessibility_error(state, monkeypatch):
    """The first osascript failure with a denial-shaped error creates the marker and returns False."""
    denial_stderr = b"execution error: System Events got an error: osascript is not allowed to send keystrokes. (-1743)"

    class Failed:
        returncode = 1
        stderr = denial_stderr

    monkeypatch.setattr(dictate.subprocess, "run", lambda *args, **kw: Failed)
    monkeypatch.setattr(dictate, "_log_stderr", lambda stderr_bytes: None)

    assert not (state / "dictate-paste-denied").exists()
    assert dictate._run_paste("osascript", "cmd+v", enter=False, ydotool_socket="") is False
    assert (state / "dictate-paste-denied").exists()
    log = (state / "dictate.log").read_text(encoding="utf-8")
    assert "accessibility permission denied" in log


def test_run_paste_does_not_mark_denial_on_non_accessibility_error(state, monkeypatch):
    """A transient osascript failure (e.g. a badly formed argument) must not create the denial
    marker — otherwise one legitimate error retires the keystroke path forever."""
    non_denial_stderr = b"0:1: syntax error: A identifier can't go after this identifier. (-2740)"

    class Failed:
        returncode = 1
        stderr = non_denial_stderr

    monkeypatch.setattr(dictate.subprocess, "run", lambda *args, **kw: Failed)
    monkeypatch.setattr(dictate, "_log_stderr", lambda stderr_bytes: None)

    assert dictate._run_paste("osascript", "cmd+v", enter=False, ydotool_socket="") is False
    assert not (state / "dictate-paste-denied").exists()  # marker NOT created
    log_path = state / "dictate.log"
    if log_path.exists():
        assert "permission denied" not in log_path.read_text(encoding="utf-8")


def test_run_paste_marks_denial_only_on_required_steps(state, monkeypatch):
    """The Enter-key step is best-effort (required=False); a denial-shaped error there must not
    create the marker — only the required paste-keystroke step counts."""
    denial_stderr = b"osascript is not allowed to send keystrokes. (1002)"

    class Failed:
        returncode = 1
        stderr = denial_stderr

    monkeypatch.setattr(dictate.subprocess, "run", lambda *args, **kw: Failed)
    monkeypatch.setattr(dictate, "_log_stderr", lambda stderr_bytes: None)

    # Enter-only plan: a single step, required=False (key code 36).
    # _run_paste with enter=True builds a two-step plan: the required paste keystroke first,
    # then the best-effort Enter press. We patch subprocess to fail ONLY the Enter step.
    call_count = [0]

    class PasteOkEnterFailed:
        returncode = 1
        stderr = denial_stderr

    class PasteOk:
        returncode = 0
        stderr = b""

    def staged_run(argv, **kw):
        call_count[0] += 1
        if call_count[0] == 2:  # the Enter step, required=False
            return PasteOkEnterFailed()
        return PasteOk()

    monkeypatch.setattr(dictate.subprocess, "run", staged_run)
    monkeypatch.setattr(dictate, "_log_stderr", lambda stderr_bytes: None)

    # enter=True builds two steps: the paste keystroke (required) + Enter (best-effort)
    result = dictate._run_paste("osascript", "cmd+v", enter=True, ydotool_socket="")
    # The paste keystroke succeeded, so the function returns True — the Enter step is ignored
    assert result is True
    assert not (state / "dictate-paste-denied").exists()


def test_stop_and_transcribe_shows_denial_note_on_first_denial(state, monkeypatch, paste_run):
    """The first time paste is denied, the notification says why rather than the generic
    'copied — press …' message. Subsequent toggles get the generic message because the
    denial was already explained."""
    (state / "dictate.wav").write_bytes(b"\0" * (dictate.WAV_HEADER_BYTES + dictate.BYTES_PER_SECOND))
    monkeypatch.setattr(dictate, "transcribe", lambda s: "hello agent")
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: paste_run.clipboard.append(kw["input"]))
    # The test runs on Linux where osascript is not installed; force the paste tool to osascript
    monkeypatch.setattr(dictate, "pick_paste_tool", lambda system, have, sock_ok, display: "osascript")

    notes: list[str] = []
    monkeypatch.setattr(dictate, "note", lambda message, system: notes.append(message))

    # First toggle: _run_paste detects denial AND marks it
    def deny_then_return(_tool, _key, _enter, _sock):
        dictate._mark_paste_denied()
        return False

    monkeypatch.setattr(dictate, "_run_paste", deny_then_return)
    s = dictate.resolve_settings({"dictate": {"auto_paste": True, "clipboard": "xclip"}}, "Darwin")
    assert dictate.stop_and_transcribe(s, "Darwin", "send", 12345) == 0
    assert notes[-1] == "accessibility permission denied — text is on the clipboard"
    assert "Accessibility permission not granted" in (state / "dictate.log").read_text(encoding="utf-8")


def test_subsequent_denial_shows_regular_clipboard_note(state, monkeypatch, paste_run):
    """Once the denial has been explained, further toggles get the normal clipboard message."""
    (state / "dictate.wav").write_bytes(b"\0" * (dictate.WAV_HEADER_BYTES + dictate.BYTES_PER_SECOND))
    monkeypatch.setattr(dictate, "transcribe", lambda s: "hello agent")
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: paste_run.clipboard.append(kw["input"]))
    monkeypatch.setattr(dictate, "pick_paste_tool", lambda system, have, sock_ok, display: "osascript")

    notes: list[str] = []
    monkeypatch.setattr(dictate, "note", lambda message, system: notes.append(message))

    # The denial marker already exists — _run_paste returns False immediately
    (state / "dictate-paste-denied").write_text("denied", encoding="utf-8")

    def already_denied(_tool, _key, _enter, _sock):
        return False

    monkeypatch.setattr(dictate, "_run_paste", already_denied)
    s = dictate.resolve_settings({"dictate": {"auto_paste": True, "clipboard": "xclip"}}, "Darwin")
    assert dictate.stop_and_transcribe(s, "Darwin", "send", 12345) == 0
    assert notes[-1] == "copied — press cmd+v to paste"


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
    claim together — the O_EXCL create is the only arbiter, and it must pick exactly one.

    debounce_ms is 0 on purpose: the key-repeat guard sits in front of this race and would drop the
    second invocation before it ever reached the claim, which is a different (also tested)
    property. Turning it off is how the claim itself stays reachable."""
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
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord", "debounce_ms": 0}})

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


# --- the key-repeat guard: a held hotkey is one toggle, not a stream of them --------------------


def test_debounce_admits_the_first_toggle_and_drops_a_refire_inside_the_window(state):
    assert dictate.debounce_toggle(0.5, now=1000.0) is None  # nothing before it: proceed
    age = dictate.debounce_toggle(0.5, now=1000.25)  # OS autorepeat, ~4 Hz
    assert age is not None
    assert age == pytest.approx(0.25, abs=0.01)


def test_debounce_admits_a_toggle_past_the_window(state):
    assert dictate.debounce_toggle(0.5, now=1000.0) is None
    assert dictate.debounce_toggle(0.5, now=1000.5) is None  # the boundary belongs to the tap
    # and the stamp moved with it, so the window is measured from the LAST admitted toggle
    assert dictate.debounce_toggle(0.5, now=1000.9) is not None


def test_a_dropped_refire_refreshes_the_stamp(state):
    """The debounce contract, and the whole of windowsill#49: the window is measured from the last
    FIRE, not the last admitted toggle. Otherwise a held key re-admits every window forever — a
    rate limiter, which is the reported bug re-timed rather than fixed."""
    assert dictate.debounce_toggle(0.5, now=1000.0) is None
    for tick in (1000.1, 1000.2, 1000.3, 1000.4):
        assert dictate.debounce_toggle(0.5, now=tick) is not None
    # 0.6 s after the ADMITTED toggle but only 0.2 s after the last repeat: still the same hold
    assert dictate.debounce_toggle(0.5, now=1000.6) is not None
    assert (state / "dictate-last-toggle").read_text(encoding="utf-8") == "1000.600000"
    # …and the quiet period runs from the release, so the next real tap gets through
    assert dictate.debounce_toggle(0.5, now=1001.2) is None


@pytest.mark.parametrize(
    "delay,interval",
    [(0.5, 0.03), (0.375, 0.09), (0.66, 0.04)],  # GNOME, macOS, X11 defaults: repeat delay, then interval
)
def test_a_held_key_is_one_toggle_however_long_it_is_held(state, delay, interval):
    """The acceptance shape the ticket describes, on every desktop's real autorepeat timing: the
    OS's repeat DELAY, then its repeat interval, for three seconds of hold. Exactly one fire may be
    admitted — a rate limiter re-admits every window instead (~4 more toggles here), and its first
    re-admission is the repeat that lands past the delay."""
    window = dictate.DEBOUNCE_SECONDS
    fires = [1000.0] + [1000.0 + delay + interval * i for i in range(int(3.0 / interval))]
    admitted = [now for now in fires if dictate.debounce_toggle(window, now=now) is None]
    assert admitted == [1000.0]
    assert dictate.debounce_toggle(window, now=fires[-1] + window + 0.01) is None  # released


def test_a_zero_window_turns_the_guard_off_and_writes_no_stamp(state):
    assert dictate.debounce_toggle(0.0) is None
    assert dictate.debounce_toggle(-1.0) is None
    assert not (state / "dictate-last-toggle").exists()


def test_a_garbage_stamp_is_read_as_no_previous_toggle(state):
    (state / "dictate-last-toggle").write_text("not-a-timestamp", encoding="utf-8")
    assert dictate.debounce_toggle(0.5, now=1000.0) is None
    assert (state / "dictate-last-toggle").read_text(encoding="utf-8") == "1000.000000"


def test_a_stamp_a_hair_in_the_future_still_debounces(state):
    """Writing the stamp rounds; two fires microseconds apart can therefore read a stamp marginally
    AHEAD of their own clock. Testing `age >= 0` there would wave through exactly the autorepeat
    the guard exists to catch, so the window is measured on the absolute age."""
    (state / "dictate-last-toggle").write_text("1000.0005", encoding="utf-8")
    assert dictate.debounce_toggle(0.5, now=1000.0) == 0.0


def test_a_stamp_from_the_future_never_wedges_the_toggle(state):
    # a clock stepped backwards (ntp, a suspend/resume) by more than the window must not lock
    # dictation out until wall-clock catches up
    (state / "dictate-last-toggle").write_text("99999999999", encoding="utf-8")
    assert dictate.debounce_toggle(0.5, now=1000.0) is None


def test_the_guard_fails_open_when_the_stamp_cannot_be_opened(state, monkeypatch):
    monkeypatch.setattr(dictate, "_TOGGLE_PATH", str(state))  # a directory: O_RDWR cannot open it
    assert dictate.debounce_toggle(0.5) is None
    assert dictate.debounce_toggle(0.5) is None  # a guard that cannot keep time never blocks a tap
    assert "debounce stamp unavailable" in (state / "dictate.log").read_text(encoding="utf-8")


def test_the_guard_fails_open_when_the_state_dir_is_missing(state, monkeypatch):
    """The realistic shape of the previous test's failure — a first run with no state dir yet, so
    O_CREAT gets ENOENT rather than the EISDIR a tmp_path can produce."""
    monkeypatch.setattr(dictate, "_TOGGLE_PATH", str(state / "not-created-yet" / "dictate-last-toggle"))
    assert dictate.debounce_toggle(0.5) is None
    assert "debounce stamp unavailable" in (state / "dictate.log").read_text(encoding="utf-8")


def test_main_creates_the_state_dir_before_the_guard_reads_it(state, monkeypatch, tmp_path):
    """Ordering regression guard: the guard runs earlier in main than anything else that touches
    the state dir, so if the makedirs ever moved below it, every FIRST toggle on a clean machine
    would run unguarded and log 'stamp unavailable'."""
    fresh = tmp_path / "never-existed"
    monkeypatch.setattr(dictate, "_STATE_DIR", str(fresh))
    monkeypatch.setattr(dictate, "_LOG_PATH", str(fresh / "dictate.log"))
    monkeypatch.setattr(dictate, "_PID_PATH", str(fresh / "dictate.pid"))
    monkeypatch.setattr(dictate, "_TOGGLE_PATH", str(fresh / "dictate-last-toggle"))
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord"}})

    assert dictate.main(["dictate.py"]) == 0
    assert (fresh / "dictate-last-toggle").exists()  # the guard stamped a real first toggle
    assert "debounce stamp unavailable" not in (fresh / "dictate.log").read_text(encoding="utf-8")


def test_a_stamp_that_cannot_be_rewritten_never_blocks_the_toggle(state, monkeypatch):
    """The write half of fail-open: the file opens but the refresh fails (a full or read-only
    filesystem). The guard says so and admits the toggle — a debounce that cannot record time must
    never become a hotkey that records no audio."""

    def refuse(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dictate.os, "ftruncate", refuse)
    assert dictate.debounce_toggle(0.5, now=1000.0) is None
    assert dictate.debounce_toggle(0.5, now=1000.1) is None  # the stamp never moved: still open
    assert "debounce stamp not written" in (state / "dictate.log").read_text(encoding="utf-8")


@pytest.mark.skipif(dictate.fcntl is None, reason="POSIX flock only")
def test_a_toggle_that_cannot_take_the_stamp_lock_is_a_refire(state):
    """Losing the non-blocking flock means another invocation is stamping RIGHT NOW — microseconds
    apart is autorepeat, not two taps."""
    holder = os.open(dictate._TOGGLE_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    dictate.fcntl.flock(holder, dictate.fcntl.LOCK_EX | dictate.fcntl.LOCK_NB)
    try:
        assert dictate.debounce_toggle(0.5) == 0.0
    finally:
        os.close(holder)
    # the 0.0 above is a verdict, not a measurement, so the log says which of the two it was —
    # main can only report it as "0 ms after the previous one", same as a future stamp
    assert "locked by another toggle" in (state / "dictate.log").read_text(encoding="utf-8")
    assert dictate.debounce_toggle(0.5) is None  # the lock is gone with the fd: back to normal


def test_a_repeat_burst_through_main_produces_exactly_one_recording_cycle(state, monkeypatch):
    """The acceptance case (windowsill#49) end to end: key-repeat fires the whole toggle several
    times over; none of the repeats may reach either branch.

    debounce_ms is pinned wide rather than left at its default because this test runs on the REAL
    clock — under `-n auto` on a loaded runner a default-sized window is a race against how long
    eight `main()` invocations take. The window semantics that a wide window cannot show (measured
    from the last fire, not the last admitted toggle) are proven with an injected clock in
    `test_a_held_key_is_one_toggle_however_long_it_is_held`, and on the real clock by the
    dictation-contract leg of selftest.yml, whose repeat train outlasts its window."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    notes: list[str] = []
    monkeypatch.setattr(dictate, "note", lambda message, system: notes.append(message))
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord", "debounce_ms": 30000}})

    assert dictate.main(["dictate.py"]) == 0
    for _ in range(7):  # the OS repeats, immediately after
        assert dictate.main(["dictate.py"]) == 0

    assert len(spawned) == 1  # one recorder…
    assert kills == []  # …never signalled: no repeat reached the stop branch at all
    assert (state / "dictate.pid").read_text(encoding="utf-8") == "4242"  # still recording
    assert notes.count("recording…") == 1
    log = (state / "dictate.log").read_text(encoding="utf-8")
    assert log.count("toggle ignored — key repeat") == 7


def test_a_second_press_past_the_window_still_stops_the_recording(state, monkeypatch):
    """The guard must debounce autorepeat without breaking the toggle it protects."""
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: pid == FakeProc.pid)
    stopped: list[int] = []
    monkeypatch.setattr(dictate, "stop_and_transcribe", lambda s, system, mode, pid: stopped.append(pid) or 0)
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord"}})

    assert dictate.main(["dictate.py"]) == 0
    stamp = state / "dictate-last-toggle"
    stamp.write_text(f"{time.time() - 5:.3f}", encoding="utf-8")  # the human pauses, then presses again
    assert dictate.main(["dictate.py"]) == 0
    assert stopped == [FakeProc.pid]


@pytest.mark.parametrize("bad", ["soon", [], {}, "", None, float("inf"), "nan"])
def test_an_unusable_debounce_ms_falls_back_instead_of_killing_the_hotkey(state, bad):
    """A float() straight off the config would raise here — before the pidfile is read, before any
    branch runs — and the user's only symptom would be a dead hotkey with an EMPTY log, which
    troubleshooting.md teaches them to read as a broken key binding."""
    resolved = dictate.resolve_settings({"dictate": {"debounce_ms": bad}}, "Linux")["debounce_ms"]
    assert resolved == dictate.DEBOUNCE_SECONDS * 1000
    if bad not in ("", None):  # cfg() treats those two as "unset" and never reaches the coercion
        assert "not a usable number" in (state / "dictate.log").read_text(encoding="utf-8")


def test_a_negative_debounce_ms_is_off_not_a_wedged_toggle(state):
    """`window <= 0` already disabled the guard; clamping makes the config value say what SKILL.md
    documents — 0 and below are the same "off"."""
    assert dictate.resolve_settings({"dictate": {"debounce_ms": -250}}, "Linux")["debounce_ms"] == 0.0


def test_debounce_ms_is_configurable_and_zero_disables_it(state, monkeypatch):
    assert dictate.resolve_settings({"dictate": {"debounce_ms": 150}}, "Linux")["debounce_ms"] == 150.0
    assert dictate.resolve_settings({"dictate": {"debounce_ms": 0}}, "Linux")["debounce_ms"] == 0.0

    spawned: list[list[str]] = []
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: pid == FakeProc.pid)
    stopped: list[int] = []
    monkeypatch.setattr(dictate, "stop_and_transcribe", lambda s, system, mode, pid: stopped.append(pid) or 0)
    _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord", "debounce_ms": 0}})

    assert dictate.main(["dictate.py"]) == 0
    assert dictate.main(["dictate.py"]) == 0  # with the guard off, back to the old raw behaviour
    assert len(spawned) == 1
    assert stopped == [FakeProc.pid]


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
