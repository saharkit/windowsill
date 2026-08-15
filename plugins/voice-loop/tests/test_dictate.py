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
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_wsclient import Server, accept_for, parse_client_frame, read_http_head, server_frame

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
    monkeypatch.setattr(dictate, "_PASTE_LOCK_PATH", str(tmp_path / "dictate-paste-lock"))
    monkeypatch.setattr(dictate, "_STREAM_PID_PATH", str(tmp_path / "dictate-stream.pid"))
    monkeypatch.setattr(dictate, "_STREAM_RESULT_PATH", str(tmp_path / "dictate-stream.json"))
    monkeypatch.setattr(dictate, "_PREVIEW_PATH", str(tmp_path / "dictate-preview.json"))
    return tmp_path


# --- resolve_settings: the config-precedence table ----------------------------------------------


def test_defaults_with_empty_config_linux():
    s = dictate.resolve_settings({}, "Linux")
    assert s["mode"] == "send"
    assert s["paste_key"] == "ctrl+shift+v"
    assert s["auto_paste"] is False
    assert s["recorder"] == "auto"
    assert s["source"] == ""  # no echo-cancel source by default
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


def test_stt_cloud_provider_defaults_to_openai():
    s = dictate.resolve_settings({}, "Linux")
    assert s["stt_provider"] == "openai"
    assert s["stt_model"] == "whisper-1"  # the provider-dependent model default


def test_stt_cloud_provider_elevenlabs_switches_model_default():
    s = dictate.resolve_settings({"stt": {"cloud": {"provider": "elevenlabs"}}}, "Linux")
    assert s["stt_provider"] == "elevenlabs"
    assert s["stt_model"] == "scribe_v1"


def test_stt_model_explicit_overrides_provider_default():
    s = dictate.resolve_settings(
        {"stt": {"cloud": {"provider": "elevenlabs"}, "model": "custom-model"}}, "Linux"
    )
    assert s["stt_provider"] == "elevenlabs"
    assert s["stt_model"] == "custom-model"


def test_stt_cloud_endpoint_defaults_to_empty():
    assert dictate.resolve_settings({}, "Linux")["cloud_endpoint"] == ""


# --- read_key: key_file wins, whitespace stripped, never from argv ------------------------------


def test_key_file_wins_over_env(tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text(" sk-fromfile \n")
    assert dictate.read_key(str(key_file), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromfile"


def test_oversized_key_file_falls_back_without_reading_the_whole_file(tmp_path):
    """L2: a hostile key file must not become an unbounded read or a credential candidate."""
    key_file = tmp_path / "k"
    key_file.write_bytes(b"x" * (dictate.MAX_KEY_BYTES + 1))
    assert dictate.read_key(str(key_file), "K_ENV", {"K_ENV": "from-env"}) == "from-env"


def test_oversized_config_is_ignored(tmp_path):
    """L2: a malformed giant config must fail closed instead of consuming the hotkey process."""
    config = tmp_path / "config.json"
    config.write_bytes(b"{" + b"x" * dictate.MAX_CONFIG_BYTES)
    assert dictate.load_config(str(config)) == {}


def test_missing_key_file_falls_back_to_env(tmp_path):
    assert dictate.read_key(str(tmp_path / "absent"), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromenv"
    assert dictate.read_key("", "K_ENV", {}) == ""


def test_bounded_text_rejects_a_file_that_cannot_be_read(state, tmp_path):
    """L2: an input that exists but cannot be read is rejected, never half-accepted."""
    unreadable = tmp_path / "adir"
    unreadable.mkdir()
    assert dictate._read_bounded_text(str(unreadable), 16, label="config") is None
    assert "bounded input: config unreadable" in (state / "dictate.log").read_text(encoding="utf-8")


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


def test_pw_record_targets_echo_cancel_source_when_configured():
    """L2: pw-record --target routes capture to the AEC source; without it the gap is an echo leak."""
    argv = dictate.recorder_argv("pw-record", "Linux", "/tmp/w.wav", source="Echo-Cancel Source")
    assert argv == ["pw-record", "--rate", "16000", "--channels", "1", "--target", "Echo-Cancel Source", "/tmp/w.wav"]


def test_pw_record_no_target_when_source_is_empty():
    """L2: an empty source must produce the exact argv the pre-AEC code produced — no regression."""
    argv = dictate.recorder_argv("pw-record", "Linux", "/tmp/w.wav", source="")
    assert argv == ["pw-record", "--rate", "16000", "--channels", "1", "/tmp/w.wav"]


def test_source_is_only_applied_to_pw_record():
    """L2: arecord and ffmpeg must not pick up the source — --target is pw-record-only."""
    assert dictate.recorder_argv("arecord", "Linux", "w.wav", source="Echo-Cancel Source") == [
        "arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "w.wav"
    ]
    # ffmpeg should be unchanged
    linux = dictate.recorder_argv("ffmpeg", "Linux", "w.wav", source="Echo-Cancel Source")
    assert "--target" not in linux


# --- the belt-and-suspenders echo guard (windowsill#101) ----------------------------------------


def test_echo_normalization_collapses_whitespace_and_case_and_punctuation():
    """L2: the recognizer varies these axes; normalization must erase that variance."""
    assert dictate._normalize_for_echo_check("Hello, World!") == "hello world"
    assert dictate._normalize_for_echo_check("  Привет…  мир  ") == "привет мир"
    assert dictate._normalize_for_echo_check("") == ""


def test_exact_match_is_detected_as_echo():
    """L2: verbatim capture of the spoken line (the staged experiment, confidence 0.98)."""
    assert dictate._is_echo_of_last_spoken("Hello world", "Hello world") is True
    # Normalization erases case and punctuation differences
    assert dictate._is_echo_of_last_spoken("Hello, World!", "hello world") is True


def test_spoken_line_contained_in_longer_transcript_is_echo():
    """L2: the production incident — the assistant's full sentence leaked into a real dictation."""
    spoken = "Ключник — это привратник с грамотой, а не сейф с ключом"
    transcript = f"я думаю что {spoken} наверное"
    assert dictate._is_echo_of_last_spoken(transcript, spoken) is True


def test_short_spoken_text_is_not_contained_checked():
    """L2: the length floor keeps short words from muting every dictation that contains them."""
    assert dictate._is_echo_of_last_spoken("the cat sat", "the") is False
    assert dictate._is_echo_of_last_spoken("yes", "yes") is True  # exact match still catches short


def test_normal_speech_that_differs_is_not_echo():
    """L2: non-echo speech must reach the prompt — a false positive here mutes the user."""
    assert dictate._is_echo_of_last_spoken("какая сегодня погода", "ключник это привратник") is False
    assert dictate._is_echo_of_last_spoken("hello world", "goodbye") is False


def test_empty_inputs_are_never_echo():
    assert dictate._is_echo_of_last_spoken("", "something") is False
    assert dictate._is_echo_of_last_spoken("something", "") is False
    assert dictate._is_echo_of_last_spoken("", "") is False


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


def test_clipboard_wsl_clipexe_is_not_a_candidate():
    """L3 (two-way falsification) for #179 defect 3, corrected: ``clip.exe`` is on PATH inside
    WSL through Windows interop, but it was NEVER a candidate — the tiers are pbcopy/wl-copy/
    xclip only, so no probe order can select it. What the WSL case must prove is that the
    ordinary Linux order keeps holding there: a live Wayland session (WSLg exports
    $WAYLAND_DISPLAY like any Wayland desktop) takes wl-copy, and a clip.exe-only PATH
    resolves to "" rather than to the interop tool.

    Without $WAYLAND_DISPLAY the wl-copy-beats-xclip swap the ticket described cannot be
    right: an unset WAYLAND_DISPLAY means no Wayland socket to reach, and the xclip tier
    (XWayland) is the honest answer there.
    """
    # WSLg session: $WAYLAND_DISPLAY set, wl-copy installed -> wl-copy, clip.exe on PATH
    # notwithstanding (it is not a tier).
    assert (
        dictate.resolve_clipboard("auto", "Linux", have("wl-copy", "xclip", "clip.exe"), wayland=True)
        == "wl-copy"
    )
    # WAYLAND_DISPLAY unset (XWayland setup): xclip wins — both inside and outside WSL, since
    # the order is the same order.
    assert (
        dictate.resolve_clipboard("auto", "Linux", have("wl-copy", "xclip", "clip.exe"), wayland=False)
        == "xclip"
    )
    # clip.exe alone on PATH: NOT a candidate — "" is the answer, the "no clipboard tool"
    # diagnostic, not the interop passthrough.
    assert dictate.resolve_clipboard("auto", "Linux", have("clip.exe"), wayland=False) == ""


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


# --- Windows native dictation (windowsill#177) ---------------------------------------------------
#
# Native Windows is a separate code path: dshow capture, clip.exe clipboard, SendKeys paste. Each
# `if system == "Windows"` branch in the recorder/clipboard/paste decision tables is the seam the
# acceptance criterion reads off `dictate.log`, so each branch gets its own test at the lowest
# level that pins it. The dshow parser is a pure helper and gets full-coverage tests; the
# discovery step is impure (subprocess) and is tested by faking that one call.


def test_windows_ffmpeg_argv_uses_dshow_with_the_named_device():
    """L2 gap: an `avfoundation` argv on Windows targets a macOS-only input and produces silence
    on the user's mic — the failure shape is exactly the dictation that records but never hears.
    The dshow branch must include ``-f dshow -i audio=<device>`` and pass the device name
    through as a single argv element (spaces in the device name are part of the value, not a
    word boundary)."""
    argv = dictate.recorder_argv("ffmpeg", "Windows", "/tmp/w.wav", source="Microphone (Realtek Audio)")
    assert ["-f", "dshow", "-i", "audio=Microphone (Realtek Audio)"] == argv[4:8]
    assert argv[-1] == "/tmp/w.wav"


def test_windows_ffmpeg_no_device_refuses_argv():
    """L2 gap: an empty source on Windows must return [], not silently fall back to alsa. DirectShow
    has no platform default like alsa's `default`, and an ffmpeg argv without -i would fail at the
    `-f dshow` step the moment the recorder starts — the visible symptom being a recorder that
    starts and captures nothing, exactly the failure shape dictation must refuse to ship."""
    assert dictate.recorder_argv("ffmpeg", "Windows", "/tmp/w.wav") == []
    assert dictate.recorder_argv("ffmpeg", "Windows", "/tmp/w.wav", source="") == []


def test_windows_ffmpeg_source_is_not_misread_as_pw_record_target():
    """L2 gap: `--target` is pw-record-only. Reusing `source` as the Windows device name must not
    inject the pw-record flag on the ffmpeg branch — `-target` is a dshow option, not a flag."""
    argv = dictate.recorder_argv("ffmpeg", "Windows", "/tmp/w.wav", source="Mic")
    assert "--target" not in argv
    assert "audio=Mic" in argv


def test_parse_dshow_audio_devices_reads_first_device_under_audio_header():
    """L2 gap: ffmpeg writes 'DirectShow audio devices' then `  "..."` lines under it; the parser
    must skip non-device lines (headers, the camera section), capture the FIRST quoted device
    name, and ignore every device name that appears later in the catalog."""
    stderr = (
        "ffmpeg version 6.0 Copyright (c) 2000-2024 the FFmpeg developers\n"
        "[dshow @ 0x55c8b0a0c0] DirectShow audio devices\n"
        '[dshow @ 0x55c8b0a0c0]  "Microphone (Realtek Audio)"\n'
        '[dshow @ 0x55c8b0a0c0]  "Stereo Mix (Realtek Audio)"\n'
        "[dshow @ 0x55c8b0a0c0] DirectShow video devices\n"
        '[dshow @ 0x55c8b0a0c0]  "Integrated Camera"\n'
        "dummy: Immediate exit requested\n"
    )
    assert dictate._parse_dshow_audio_devices(stderr) == "Microphone (Realtek Audio)"


def test_parse_dshow_audio_devices_ignores_devices_under_video_header():
    """L2 gap: without the audio/video header toggle, the first video device wins. A wrong device
    captures a still image stream instead of audio — silent clip, exactly the failure class
    dictation must reject."""
    stderr = (
        "[dshow @ 0x55c8b0a0c0] DirectShow video devices\n"
        '[dshow @ 0x55c8b0a0c0]  "Integrated Camera"\n'
    )
    assert dictate._parse_dshow_audio_devices(stderr) == ""


def test_parse_dshow_audio_devices_empty_or_unknown_stderr_returns_empty():
    """L2 gap: a rig with no audio devices (or a missing ffmpeg) yields '' rather than crashing."""
    assert dictate._parse_dshow_audio_devices("") == ""
    assert dictate._parse_dshow_audio_devices("random ffmpeg noise") == ""


def test_discover_dshow_mic_returns_empty_when_stderr_has_no_listing(monkeypatch):
    """L2 gap: a non-zero exit with no stderr is the canonical 'ffmpeg not found' signal — must
    return '', not raise."""
    calls: list[dict[str, object]] = []

    class Done:
        returncode = 1
        stderr = ""

    def fake_run(argv, **kw):
        calls.append({"argv": argv, "kw": kw})
        return Done()

    monkeypatch.setattr(dictate.subprocess, "run", fake_run)
    assert dictate._discover_dshow_mic() == ""
    assert len(calls) == 1
    assert calls[0]["argv"] == dictate._DSHOW_DISCOVERY_ARGV
    assert calls[0]["kw"]["timeout"] == dictate._DSHOW_DISCOVERY_TIMEOUT
    assert calls[0]["kw"]["check"] is False


def test_discover_dshow_mic_returns_parsed_first_device(monkeypatch):
    """End-to-end: stderr arrives, the parser returns the first audio device, the helper
    surfaces it as the chosen mic."""
    expected_stderr = (
        "[dshow @ 0x55c8b0a0c0] DirectShow audio devices\n"
        '[dshow @ 0x55c8b0a0c0]  "Real Mic"\n'
    )

    class Done:
        returncode = 0
        stderr = expected_stderr

    monkeypatch.setattr(dictate.subprocess, "run", lambda *a, **kw: Done())
    assert dictate._discover_dshow_mic() == "Real Mic"


def test_clipboard_native_windows_prefers_clip_when_present():
    """L2 gap: a Windows tier must be present for native-Windows install. Without it the resolution
    falls through to '' (the documented "no clipboard tool" diagnostic) — the recording completes,
    but the user's clipboard is empty, which is the dictation-equivalent of #93 (an empty
    transcript reachable via the success path)."""
    assert dictate.resolve_clipboard("auto", "Windows", have("clip.exe"), wayland=False) == "clip"
    assert dictate.resolve_clipboard("auto", "Windows", have_none, wayland=False) == ""


def test_clipboard_native_windows_takes_explicit_clip_name():
    """An explicit `clip` works without `clip.exe` on PATH (the user pre-confirmed the install)."""
    assert dictate.resolve_clipboard("clip", "Windows", have_none, wayland=False) == "clip"


def test_clipboard_native_windows_ignores_wayland_flag():
    """The `wayland` parameter is Linux-only; on Windows the absence or presence of a Wayland
    session is irrelevant. A branch that re-ordered on `wayland` would miss the Windows tier
    when a runner had $WAYLAND_DISPLAY set."""
    assert dictate.resolve_clipboard("auto", "Windows", have("clip.exe"), wayland=True) == "clip"


def test_clipboard_commands_for_clip_returns_clip_exe_invocation():
    """clip.exe is the only argv under the new tier; a single-element list matches the pbcopy
    shape (one canonical command, no primary/clipboard split — Windows has no secondary
    selection)."""
    assert dictate.clipboard_commands("clip") == [["clip.exe"]]


def test_paste_tool_pick_windows_uses_sendkeys():
    """L2 gap: a Windows paste tool must be present. The Linux branches don't fire on Windows,
    so without this branch `pick_paste_tool` returns '' and auto-paste silently falls back to
    the clipboard tier — the dictation lands in the clipboard but never in the prompt."""
    assert dictate.pick_paste_tool("Windows", have("powershell.exe"), False, "") == "sendkeys"
    assert dictate.pick_paste_tool("Windows", have_none, False, "") == ""


def test_sendkeys_argv_uses_noprofile_and_wscript_shell():
    """L2 gap: -NoProfile avoids a PowerShell profile load on every paste (visible lag); the
    COM-call shape is the auto-paste bug-free automation surface — every alternative (Add-Type
    + SendKeys, start-process) has either a profile load or a longer cold path."""
    argv = dictate._win_sendkeys_argv("^v")
    assert argv[0] == "powershell.exe"
    assert "-NoProfile" in argv
    assert "-Command" in argv
    assert argv[-1] == "(New-Object -ComObject WScript.Shell).SendKeys('^v')"


def test_paste_plan_sendkeys_renders_ctrl_shift_v_as_caret_caret_v():
    """L2 gap: `^+v` is simultaneous Ctrl+Shift+V. Dropping the `+` makes some apps fire Ctrl+V
    twice (a paste, then a paste-while-pasting race). The `+` must precede the modifier it
    applies to, and a separate modifier pair must surround the key for it to be a chord."""
    steps = dictate.paste_plan("sendkeys", "ctrl+shift+v", enter=False)
    assert len(steps) == 1
    _, argv, required = steps[0]
    assert required is True
    assert argv[-1].endswith("SendKeys('^+v')")


def test_paste_plan_sendkeys_renders_shift_insert_with_braces():
    """L2 gap: '+Insert' (without braces) means Shift+I, n, s, e, r, t — six keys instead of one.
    The braces tell SendKeys to look up the literal key name, not type the letters."""
    steps = dictate.paste_plan("sendkeys", "shift+insert", enter=False)
    assert steps[0][1][-1].endswith("SendKeys('+{Insert}')")


def test_paste_plan_sendkeys_uses_single_braces_around_enter():
    """L2 gap: PowerShell single-quoted strings are VERBATIM — there is no brace-doubling escape
    in them. A doubled `{{Enter}}` reaches SendKeys as the literal `{{Enter}}`, which SendKeys
    parses as the token `{Enter` and rejects; and because the Enter step is required=False, that
    failure is silent — the default `send` mode pastes the transcript but never submits it. The
    assertion matches the `+{Insert}` shape in the same table: single braces, the SendKeys
    special-key name."""
    steps = dictate.paste_plan("sendkeys", "ctrl+v", enter=True)
    assert len(steps) == 2
    delay, argv, required = steps[1]
    assert delay == 0.25
    assert argv[-1].endswith("SendKeys('{Enter}')")
    assert required is False  # best-effort, like the other tools' Enter


def test_paste_plan_sendkeys_falls_back_to_plain_ctrl_v_for_unknown_paste_key():
    """An unknown paste_key maps to `^v` — the safe default that the Linux branches also end
    up at via the .get(paste_key, default) pattern."""
    steps = dictate.paste_plan("sendkeys", "garbage", enter=False)
    assert steps[0][1][-1].endswith("SendKeys('^v')")


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


def test_accessibility_probe_reads_frontmost_windows_not_just_process_name():
    """The probe must cross Accessibility, not merely Automation.

    Mutation gap: replacing the window query with the old process-name query would still return 0
    in the stubbed subprocess test, so this assertion catches the wrong TCC permission probe.
    """
    assert "get name of every window" in dictate._ACCESSIBILITY_PROBE[2]
    assert "first application process whose frontmost is true" in dictate._ACCESSIBILITY_PROBE[2]


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


def test_oversized_stt_response_is_rejected(state, monkeypatch, opener):
    """L2: an endpoint cannot force dictation to buffer an unbounded response."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    opener(b"x" * (dictate.MAX_RESPONSE_BYTES + 1))
    s = dictate.resolve_settings({}, "Linux")
    assert dictate.transcribe(s) == ""
    assert "response rejected" in (state / "dictate.log").read_text(encoding="utf-8")


class _HTTPErrorOpener:
    """An opener whose .open() raises an HTTPError carrying a body — the diagnosis path."""

    def __init__(self, error) -> None:
        self._error = error

    def open(self, request, timeout=None):
        raise self._error


def test_post_bytes_refuses_an_oversized_audio_body(state, monkeypatch):
    """L2: the outbound guard refuses a request over the ceiling before it opens a socket."""
    monkeypatch.setattr(dictate, "MAX_AUDIO_BYTES", 4)

    def fail_if_called(*handlers):
        raise AssertionError("the guard must refuse before building an opener")

    monkeypatch.setattr(dictate.urllib.request, "build_opener", fail_if_called)
    assert dictate._post_bytes("http://127.0.0.1:8355/stt", {}, b"12345", "audio/wav", 60.0) is None
    assert "stt request rejected" in (state / "dictate.log").read_text(encoding="utf-8")


def test_post_bytes_returns_the_error_body_on_an_http_error(state, monkeypatch):
    """L2: an HTTP error's body is the diagnosis — returned, never swallowed."""
    err = dictate.urllib.error.HTTPError("http://x", 500, "boom", {}, io.BytesIO(b"server said no"))
    monkeypatch.setattr(dictate.urllib.request, "build_opener", lambda *handlers: _HTTPErrorOpener(err))
    assert dictate._post_bytes("http://x", {}, b"data", "audio/wav", 60.0) == b"server said no"


def test_post_bytes_rejects_an_oversized_error_body(state, monkeypatch):
    """L2: even the diagnosis is bounded — an error body over the ceiling is dropped, not buffered."""
    err = dictate.urllib.error.HTTPError(
        "http://x", 500, "boom", {}, io.BytesIO(b"x" * (dictate.MAX_RESPONSE_BYTES + 1))
    )
    monkeypatch.setattr(dictate.urllib.request, "build_opener", lambda *handlers: _HTTPErrorOpener(err))
    assert dictate._post_bytes("http://x", {}, b"data", "audio/wav", 60.0) == b""
    assert "response rejected" in (state / "dictate.log").read_text(encoding="utf-8")


def test_local_stt_command_is_argv_only_and_bounded(state, monkeypatch):
    """L2: config text cannot become shell syntax, and a local engine gets a wall-clock bound."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    calls = []

    class Done:
        returncode = 0
        stdout = "local transcript"
        stderr = ""

    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or Done())
    s = dictate.resolve_settings({"stt": {"command": "whisper-cli --model 'my model'"}}, "Linux")
    assert dictate.transcribe(s) == "local transcript"
    assert calls[0][0] == ["whisper-cli", "--model", "my model", str(state / "dictate.wav")]
    assert calls[0][1]["timeout"] == 60.0
    assert calls[0][1]["check"] is False
    assert "shell" not in calls[0][1]


def test_malformed_local_stt_command_fails_closed(state):
    """L2: an unterminated quoted command must refuse execution rather than invoke a shell."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    s = dictate.resolve_settings({"stt": {"command": "whisper-cli 'unterminated"}}, "Linux")
    assert dictate.transcribe(s) == ""
    assert "command rejected" in (state / "dictate.log").read_text(encoding="utf-8")


def test_local_stt_command_failure_is_reported_with_stderr(state, monkeypatch):
    """L2: a local engine that exits non-zero is logged with its stderr, never half-trusted."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")

    class Failed:
        returncode = 3
        stdout = ""
        stderr = "engine exploded\n"

    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: Failed())
    s = dictate.resolve_settings({"stt": {"command": "whisper-cli"}}, "Linux")
    assert dictate.transcribe(s) == ""
    log = (state / "dictate.log").read_text(encoding="utf-8")
    assert "command returned 3" in log
    assert "engine exploded" in log


def test_local_stt_command_survives_a_missing_engine(state, monkeypatch):
    """L2: an engine that cannot be spawned is a logged empty transcript, not a traceback."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")

    def no_such_engine(argv, **kw):
        raise OSError("no such engine")

    monkeypatch.setattr(dictate.subprocess, "run", no_such_engine)
    s = dictate.resolve_settings({"stt": {"command": "whisper-cli"}}, "Linux")
    assert dictate.transcribe(s) == ""
    assert "command failed" in (state / "dictate.log").read_text(encoding="utf-8")


def test_local_stt_command_that_times_out_is_logged(state, monkeypatch):
    """L2: a wedged local engine is cut off by the wall-clock bound, not waited on forever."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")

    def hang(argv, **kw):
        raise dictate.subprocess.TimeoutExpired(cmd=argv, timeout=60.0)

    monkeypatch.setattr(dictate.subprocess, "run", hang)
    s = dictate.resolve_settings({"stt": {"command": "whisper-cli"}}, "Linux")
    assert dictate.transcribe(s) == ""
    assert "command failed" in (state / "dictate.log").read_text(encoding="utf-8")


def test_oversized_recording_is_rejected_before_stt(state, monkeypatch):
    """L2: a WAV that grew past the ceiling is dropped, not uploaded or transcribed."""
    (state / "dictate.wav").write_bytes(b"RIFF" + b"x" * 64)
    monkeypatch.setattr(dictate, "MAX_AUDIO_BYTES", 16)
    s = dictate.resolve_settings({}, "Linux")
    assert dictate.transcribe(s) == ""
    assert "recording rejected" in (state / "dictate.log").read_text(encoding="utf-8")


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
        (b"System Events: Zugriff verweigert (-1743)", True),
        (b"System Events: permiso denegado (1002)", True),
        (b"a version number 10020 is not a denial", False),
        (b"a different status -17430 is not a denial", False),
        (b'execution error: Can\'t open file "/tmp/voice-loop-1002.wav". (-43)', False),
        (b"execution error: a different error (-1002)", False),
        ("System Events: отказано (-1743)".encode("utf-8"), True),
        ("System Events: отказано (1002)".encode("utf-8"), True),
        (b"", False),
        (b"some other error", False),
        (b"env: osascript: No such file or directory", False),
    ],
)
def test_is_accessibility_denial_matches_the_known_phrases(stderr, expected):
    assert dictate._is_accessibility_denial(stderr) is expected


def test_is_accessibility_denial_is_case_insensitive():
    assert dictate._is_accessibility_denial(b"NOT ALLOWED TO SEND KEYSTROKES") is True


@pytest.mark.parametrize("outcome", [0, 1])
def test_accessibility_permission_probe_returns_the_exit_status(monkeypatch, outcome):
    class Result:
        returncode = outcome

    calls = []
    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or Result())
    assert dictate._accessibility_permission_regranted() is (outcome == 0)
    assert calls[0][0] == dictate._ACCESSIBILITY_PROBE
    assert calls[0][1]["timeout"] == dictate.FOCUS_PROBE_TIMEOUT
    assert calls[0][1]["check"] is False


@pytest.mark.parametrize("error", [OSError("missing osascript"), subprocess.TimeoutExpired(cmd="osascript", timeout=2)])
def test_accessibility_permission_probe_fails_closed(monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(dictate.subprocess, "run", fail)
    assert dictate._accessibility_permission_regranted() is False


def test_clear_paste_denied_is_best_effort(state, monkeypatch):
    (state / "dictate-paste-denied").write_text("denied", encoding="utf-8")
    dictate._clear_paste_denied()
    assert not (state / "dictate-paste-denied").exists()

    monkeypatch.setattr(dictate.os, "unlink", lambda path: (_ for _ in ()).throw(OSError("read-only")))
    dictate._clear_paste_denied()


def test_run_paste_skips_osascript_when_denial_is_marked(state, monkeypatch):
    """A denied paste skips the keystroke path when the permission remains unavailable."""
    (state / "dictate-paste-denied").write_text("denied", encoding="utf-8")
    monkeypatch.setattr(dictate, "_accessibility_permission_regranted", lambda: False)

    def no_spawn(*_args, **_kw):
        raise AssertionError("a denied paste must not spawn the paste keystroke")

    monkeypatch.setattr(dictate.subprocess, "run", no_spawn)
    assert dictate._run_paste("osascript", "cmd+v", enter=False, ydotool_socket="") is False


def test_run_paste_clears_stale_denial_after_accessibility_is_regranted(state, monkeypatch):
    """A later Accessibility grant clears the marker and retries the paste keystroke."""
    (state / "dictate-paste-denied").write_text("denied", encoding="utf-8")
    monkeypatch.setattr(dictate, "_accessibility_permission_regranted", lambda: True)

    class Pasted:
        returncode = 0
        stderr = b""

    spawned = []
    monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: spawned.append(argv) or Pasted())

    assert dictate._run_paste("osascript", "cmd+v", enter=False, ydotool_socket="") is True
    assert not (state / "dictate-paste-denied").exists()
    assert spawned == [["osascript", "-e", 'tell application "System Events" to keystroke "v" using {command down}']]


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
    # Pin the plan shape this test depends on: with enter=True the osascript plan is exactly two
    # steps — the required paste keystroke, then a best-effort Enter (required=False). Without this,
    # the assertions below hold even if the Enter step vanished or became required, so the test would
    # pass while testing nothing about the required=False path its name advertises.
    plan = dictate.paste_plan("osascript", "cmd+v", enter=True)
    assert [required for _, _, required in plan] == [True, False]
    denial_stderr = b"osascript is not allowed to send keystrokes. (1002)"

    class PasteOkEnterFailed:
        returncode = 1
        stderr = denial_stderr

    class PasteOk:
        returncode = 0
        stderr = b""

    def staged_run(argv, **kw):
        if argv[-1] == 'tell application "System Events" to key code 36':
            return PasteOkEnterFailed()
        return PasteOk()

    monkeypatch.setattr(dictate.subprocess, "run", staged_run)
    monkeypatch.setattr(dictate, "_log_stderr", lambda stderr_bytes: None)

    result = dictate._run_paste("osascript", "cmd+v", enter=True, ydotool_socket="")
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


# --- paste-lock guard (windowsill#50): one paste at a time ---------------------------------------


class TestPasteLock:
    def test_no_lock_file_means_not_locked(self, state):
        assert dictate._paste_locked() is False

    def test_live_lock_is_detected(self, state, monkeypatch):
        """A lock file whose pid is alive means another process is transcribing/pasting."""
        (state / "dictate-paste-lock").write_text("99999", encoding="utf-8")
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: pid == 99999)
        assert dictate._paste_locked() is True

    def test_stale_lock_is_cleared(self, state, monkeypatch):
        """A lock whose pid is dead is stale garbage — removed so the toggle is not wedged."""
        (state / "dictate-paste-lock").write_text("99999", encoding="utf-8")
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
        assert dictate._paste_locked() is False
        assert not (state / "dictate-paste-lock").exists()

    def test_own_pid_is_not_a_lock(self, state):
        """This process's own lock must not block itself: each toggle is a separate process, but in
        tests main() and stop_and_transcribe() run in the same one."""
        (state / "dictate-paste-lock").write_text(str(os.getpid()), encoding="utf-8")
        assert dictate._paste_locked() is False

    @pytest.mark.parametrize("content", ["", "not-a-pid", "-1", "0"])
    def test_garbage_lock_file_is_not_a_lock(self, state, content):
        (state / "dictate-paste-lock").write_text(content, encoding="utf-8")
        assert dictate._paste_locked() is False

    def test_claim_and_release_cycle(self, state):
        dictate._claim_paste_lock()
        assert (state / "dictate-paste-lock").exists()
        assert (state / "dictate-paste-lock").read_text(encoding="utf-8") == str(os.getpid())
        dictate._release_paste_lock()
        assert not (state / "dictate-paste-lock").exists()

    def test_claim_is_best_effort_on_unwritable_state_dir(self, state, monkeypatch):
        monkeypatch.setattr(dictate, "_PASTE_LOCK_PATH", str(state / "not-created" / "paste-lock"))
        dictate._claim_paste_lock()  # does not raise
        assert "paste lock not written" in (state / "dictate.log").read_text(encoding="utf-8")

    def test_release_is_best_effort(self, state):
        """Releasing a lock that does not exist is a no-op, not a crash."""
        assert not (state / "dictate-paste-lock").exists()
        dictate._release_paste_lock()  # does not raise

    def test_main_ignores_toggle_when_paste_locked(self, state, monkeypatch):
        """The acceptance case: a start-toggle during transcription/paste is ignored with a log line
        and an audible cue — the previous paste cannot land mid-recording."""
        (state / "dictate-paste-lock").write_text("99999", encoding="utf-8")
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: pid == 99999)
        sounds: list[str] = []
        monkeypatch.setattr(dictate, "sound", lambda path, player: sounds.append(path))
        # No pidfile — this would normally be a start, but the paste lock blocks it.
        assert not (state / "dictate.pid").exists()

        _write_config(monkeypatch, state, {"dictate": {"debounce_ms": 0}})
        assert dictate.main(["dictate.py"]) == 0

        log_text = (state / "dictate.log").read_text(encoding="utf-8")
        assert "toggle ignored — previous dictation still finishing" in log_text
        assert sounds != []  # the audible cue fired

    def test_main_admits_toggle_when_lock_is_stale(self, state, monkeypatch):
        """A stale lock is cleaned by _paste_locked and the toggle proceeds normally."""
        (state / "dictate-paste-lock").write_text("99999", encoding="utf-8")
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)

        class FakeProc:
            pid = 4242

        spawned: list[list[str]] = []
        monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or FakeProc())
        monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
        monkeypatch.setattr(dictate, "note", lambda message, system: None)
        _write_config(monkeypatch, state, {"dictate": {"recorder": "arecord", "debounce_ms": 0}})

        assert dictate.main(["dictate.py"]) == 0
        assert len(spawned) == 1  # the recording started
        assert not (state / "dictate-paste-lock").exists()  # stale lock was cleaned

    def test_stop_and_transcribe_claims_the_lock(self, state, monkeypatch):
        """The stop path records its pid so a concurrent start is blocked."""
        (state / "dictate.wav").write_bytes(b"\0" * (dictate.WAV_HEADER_BYTES + dictate.BYTES_PER_SECOND))
        monkeypatch.setattr(dictate, "transcribe", lambda s: "hello")
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(dictate, "note", lambda message, system: None)
        monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: None)
        # Suppress atexit registration so handlers don't accumulate across tests.
        registered: list = []
        monkeypatch.setattr(dictate.atexit, "register", lambda fn: registered.append(fn))

        s = dictate.resolve_settings({"dictate": {"clipboard": "xclip"}}, "Linux")
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0

        assert (state / "dictate-paste-lock").exists()
        assert (state / "dictate-paste-lock").read_text(encoding="utf-8") == str(os.getpid())
        assert len(registered) == 1  # atexit handler registered

    def test_stop_reads_last_spoken_with_a_bounded_read(self, state, monkeypatch):
        """The echo guard's state read is bounded like every other state file."""
        (state / "dictate.wav").write_bytes(b"\0" * (dictate.WAV_HEADER_BYTES + dictate.BYTES_PER_SECOND))
        monkeypatch.setattr(dictate, "_LAST_SPOKEN_PATH", str(state / "last-spoken"))
        (state / "last-spoken").write_text("some other spoken line", encoding="utf-8")
        monkeypatch.setattr(dictate, "transcribe", lambda s: "hello")
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(dictate, "note", lambda message, system: None)
        monkeypatch.setattr(dictate.subprocess, "run", lambda argv, **kw: None)
        registered: list = []
        monkeypatch.setattr(dictate.atexit, "register", lambda fn: registered.append(fn))

        s = dictate.resolve_settings({"dictate": {"clipboard": "xclip"}}, "Linux")
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0
        assert "transcript: hello" in (state / "dictate.log").read_text(encoding="utf-8")


# --- macOS sound defaults (windowsill#50) ---------------------------------------------------------


def test_darwin_sound_defaults_use_system_sounds():
    """macOS system sounds are the natural pacing cue — default ON so the user hears when a toggle
    was ignored during the paste-lock window."""
    s = dictate.resolve_settings({}, "Darwin")
    assert s["start_sound"] == "/System/Library/Sounds/Pop.aiff"
    assert s["stop_sound"] == "/System/Library/Sounds/Blow.aiff"


def test_linux_sound_defaults_stay_empty():
    """No single default sound works across Linux distros — silence is the safe default."""
    s = dictate.resolve_settings({}, "Linux")
    assert s["start_sound"] == ""
    assert s["stop_sound"] == ""


def test_explicit_sound_overrides_the_default():
    s = dictate.resolve_settings({"dictate": {"start_sound": "/my/beep.wav"}}, "Darwin")
    assert s["start_sound"] == "/my/beep.wav"
    assert s["stop_sound"] == "/System/Library/Sounds/Blow.aiff"  # only the overridden one


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


def test_cmdline_of_reads_its_own_process_and_returns_none_for_a_dead_pid():
    own = dictate._cmdline_of(os.getpid())
    assert isinstance(own, str) and own
    assert dictate._cmdline_of(10**9) is None


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
    monkeypatch.setattr(dictate, "_pid_looks_like_recorder", lambda pid, recorder, platform_id: True)
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


def test_live_unrelated_pid_is_refused_before_signal(state, monkeypatch):
    """L2: a recycled pid must not turn a toggle into a signal for an unrelated process."""
    (state / "dictate.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(dictate, "_pid_looks_like_recorder", lambda *args: False)
    assert dictate.main(["dictate.py"]) == 1
    assert "failed identity check" in (state / "dictate.log").read_text(encoding="utf-8")


def test_pid_guard_rejects_a_non_positive_pid_or_blank_recorder():
    assert dictate._pid_looks_like_recorder(0, "pw-record", "linux") is False
    assert dictate._pid_looks_like_recorder(-1, "pw-record", "linux") is False
    assert dictate._pid_looks_like_recorder(4242, "", "linux") is False


def test_pid_guard_matches_the_recorder_executable(state, monkeypatch):
    monkeypatch.setattr(
        dictate, "_cmdline_of", lambda pid: "pw-record --rate 16000 --channels 1 /s/dictate.wav"
    )
    assert dictate._pid_looks_like_recorder(4242, "pw-record", "linux") is True


def test_pid_guard_matches_a_path_shadowing_script_by_basename(state, monkeypatch):
    """The CI fake recorder runs as `bash /path/pw-record …`: its argv carries the script path,
    not the bare name. A bare-name match would refuse that legitimate recorder and wedge the
    recording — the exact failure this branch's own dictation contract hit."""
    monkeypatch.setattr(
        dictate,
        "_cmdline_of",
        lambda pid: "bash /tmp/dictate-fake/pw-record --rate 16000 /s/dictate.wav",
    )
    assert dictate._pid_looks_like_recorder(4242, "pw-record", "linux") is True


def test_pid_guard_maps_sox_to_the_rec_front_end(state, monkeypatch):
    monkeypatch.setattr(dictate, "_cmdline_of", lambda pid: "rec -q -r 16000 -c 1 -b 16 /s/dictate.wav")
    assert dictate._pid_looks_like_recorder(4242, "sox", "linux") is True


def test_pid_guard_refuses_an_unrelated_command(state, monkeypatch):
    monkeypatch.setattr(dictate, "_cmdline_of", lambda pid: "sshd: user@pts/0")
    assert dictate._pid_looks_like_recorder(4242, "pw-record", "linux") is False


def test_pid_guard_refuses_when_the_cmdline_is_unreadable(state, monkeypatch):
    monkeypatch.setattr(dictate, "_cmdline_of", lambda pid: None)
    assert dictate._pid_looks_like_recorder(4242, "pw-record", "linux") is False


def test_pid_guard_delegates_to_the_windows_handle_check(monkeypatch):
    monkeypatch.setattr(dictate, "_win_pid_is_recorder", lambda pid, recorder: True)
    assert dictate._pid_looks_like_recorder(4242, "pw-record", "win32") is True


def test_pid_guard_keeps_the_raw_signal_contract_off_linux():
    assert dictate._pid_looks_like_recorder(4242, "pw-record", "darwin") is True


def test_debounce_ms_is_configurable_and_zero_disables_it(state, monkeypatch):
    assert dictate.resolve_settings({"dictate": {"debounce_ms": 150}}, "Linux")["debounce_ms"] == 150.0
    assert dictate.resolve_settings({"dictate": {"debounce_ms": 0}}, "Linux")["debounce_ms"] == 0.0

    spawned: list[list[str]] = []
    monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or FakeProc())
    monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
    monkeypatch.setattr(dictate, "note", lambda message, system: None)
    monkeypatch.setattr(dictate, "_pid_alive", lambda pid: pid == FakeProc.pid)
    monkeypatch.setattr(dictate, "_pid_looks_like_recorder", lambda pid, recorder, platform_id: True)
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


def test_elevenlabs_scribe_posts_the_documented_shape(state, monkeypatch, opener):
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "hello agent", "language_code": "en", "language_probability": 0.99}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "xi-secret")
    s = dictate.resolve_settings(
        {"stt": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}}}, "Linux"
    )
    assert dictate.transcribe(s) == "hello agent"

    request, timeout = fake.requests[0]
    assert request.full_url == "https://api.elevenlabs.io/v1/speech-to-text"
    assert request.get_method() == "POST"
    assert request.get_header("Xi-api-key") == "xi-secret"
    assert timeout == 60.0
    body = request.data
    assert b'name="model_id"\r\n\r\nscribe_v1\r\n' in body
    # windowsill#93: the configured language reaches Scribe under ITS name, the way the OpenAI
    # request above carries the same value as `language`
    assert b'name="language_code"\r\n\r\nen\r\n' in body
    assert b'name="file"; filename="dictate.wav"' in body


def test_an_explicitly_empty_stt_language_reaches_scribe_with_no_language_code(state, monkeypatch, opener):
    """windowsill#159: the skill writes ``stt.language: ""`` to ask Scribe to auto-detect mixed
    speech. ``cfg`` used to collapse that empty value to the top-level ``language`` (``ru`` on the
    operator's machine), so Scribe was handed ``language_code=ru`` — the opposite of auto-detect, and
    exactly the pinned hint that drops the second tongue. ``test_an_empty_language_leaves_scribe_to_
    auto_detect`` in test_providers.py proved the builder omits ``language_code`` for an empty value
    but built ``s`` by hand, so it passed while the real path never produced that empty value. This
    one goes through ``resolve_settings`` from an actual config dict, the path the unit test skipped."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "mixed speech"}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "xi-secret")
    s = dictate.resolve_settings(
        {"language": "ru", "stt": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}, "language": ""}},
        "Linux",
    )
    assert s["language"] == ""  # the empty escape hatch reached settings, not the top-level "ru"
    assert dictate.transcribe(s) == "mixed speech"
    assert b"language_code" not in fake.requests[0][0].data  # no hint pinned — Scribe auto-detects


def test_an_explicitly_empty_stt_language_reaches_the_local_server_unpinned(state, opener):
    """windowsill#159, the local-whisper half: a pinned ``ru`` is exactly the configuration that
    drops English words from a mixed sentence, so the empty escape hatch must reach the LAN request
    as an empty ``?language=`` rather than the top-level ``ru``. The local server auto-detects when
    no language is pinned; the bug would have produced ``?language=ru`` here."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "mixed speech"}')
    s = dictate.resolve_settings({"language": "ru", "stt": {"language": ""}}, "Linux")
    assert s["language"] == ""  # the empty escape hatch reached settings, not the top-level "ru"
    assert dictate.transcribe(s) == "mixed speech"
    # the bug collapsed "" to "ru" and pinned the recogniser; the fix leaves the query empty
    assert fake.requests[0][0].full_url == "http://127.0.0.1:8355/stt?language="


def test_stt_prompt_in_config_reaches_the_openai_cloud_request(state, monkeypatch, opener):
    """windowsill#162, and the #159 shape on purpose: the builder-level include is pinned in
    test_providers.py (hand-built ``s``), but THIS test goes through ``resolve_settings`` from a real
    config dict — the exact shortcut that let #159's defect (a resolver collapsing a key) survive its
    own test. A resolver that drops ``stt_prompt`` would pass the builder test and fail here."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "ok"}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings(
        {"stt": {"backend": "cloud", "endpoint": "https://api.example.com", "prompt": "kubectl, Acme"}},
        "Linux",
    )
    assert s["stt_prompt"] == "kubectl, Acme"  # the resolver carried the key through
    assert dictate.transcribe(s) == "ok"
    assert b'name="prompt"\r\n\r\nkubectl, Acme\r\n' in fake.requests[0][0].data


def test_stt_prompt_reaches_the_local_lan_request_as_a_query_parameter(state, opener):
    """windowsill#162 — the unification deliverable: ONE config key reaches BOTH paths. On the local
    path ``stt.prompt`` rides as a ``?prompt=`` query the server feeds to faster-whisper's
    initial_prompt, so a ``local``/``lan`` user sets it in config.json instead of hand-editing the
    server's systemd unit (``VOICE_LOOP_STT_HINT``). A resolver or ``_transcribe_lan`` wiring bug
    would leave the lexicon unprimed and no unit test that builds ``s`` by hand would catch it."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "ok"}')
    s = dictate.resolve_settings({"stt": {"prompt": "kubectl, Acme"}}, "Linux")
    assert s["stt_prompt"] == "kubectl, Acme"
    assert dictate.transcribe(s) == "ok"
    assert fake.requests[0][0].full_url == (
        "http://127.0.0.1:8355/stt?language=en&prompt=kubectl%2C%20Acme"
    )


def test_an_unset_stt_prompt_leaves_the_lan_url_unchanged(state, opener):
    """L3 — two-way falsification of the omit-when-empty rule on the local path. The common user sets
    no ``stt.prompt``; the LAN request must stay exactly ``?language=en`` (no stray ``&prompt=``),
    and the server then falls back to its own ``VOICE_LOOP_STT_HINT``. An always-append mutant would
    alter every LAN request and only the existing exact-URL assertion would notice — this pins the
    decision at its own tier."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "ok"}')
    s = dictate.resolve_settings({}, "Linux")
    assert s["stt_prompt"] == ""
    assert dictate.transcribe(s) == "ok"
    assert fake.requests[0][0].full_url == "http://127.0.0.1:8355/stt?language=en"


def test_elevenlabs_stt_falls_back_to_tts_key(state, monkeypatch, opener):
    """When VOICE_LOOP_STT_API_KEY is not set, ElevenLabs STT tries the TTS key —
    one credentials home, not a second one."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "hello from shared key"}')
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "shared-xi-key")
    monkeypatch.delenv("VOICE_LOOP_STT_API_KEY", raising=False)
    s = dictate.resolve_settings(
        {"stt": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}}}, "Linux"
    )
    assert dictate.transcribe(s) == "hello from shared key"
    request, _ = fake.requests[0]
    assert request.get_header("Xi-api-key") == "shared-xi-key"


def test_elevenlabs_stt_with_no_key_at_all_degrades_to_whisper(state, monkeypatch, opener):
    """No STT key and no TTS key: the cloud path returns None, transcribe degrades to whisper."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    monkeypatch.delenv("VOICE_LOOP_STT_API_KEY", raising=False)
    monkeypatch.delenv("VOICE_LOOP_TTS_API_KEY", raising=False)
    # The cloud call will return None (no key), then the degrade LAN call succeeds.
    fake = opener(b'{"text": "whisper fallback"}')
    s = dictate.resolve_settings(
        {"stt": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}}}, "Linux"
    )
    assert dictate.transcribe(s) == "whisper fallback"
    # One request: only the LAN degrade path made it past the key check
    assert len(fake.requests) == 1
    assert "/stt?language=en" in fake.requests[0][0].full_url
    log_text = (state / "dictate.log").read_text(encoding="utf-8")
    assert "no key" in log_text.lower()
    assert "cloud stt failed — falling back to local whisper" in log_text


def test_deepgram_stt_goes_through_transcribe_with_no_branch_in_the_way(state, monkeypatch, opener):
    """The proof that adding a provider is one ENTRY: Deepgram was added to the registry and
    nothing in this dispatch path learned its name — yet a configured `deepgram` reaches its own
    host, with its own auth scheme, its own body encoding, and its own response nesting.

    The response body here is the pinned fixture, so this case and test_providers.py's parser case
    fail together if Deepgram's shape drifts (windowsill#94, criterion 4)."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fixture = (Path(__file__).resolve().parent / "fixtures" / "deepgram_listen_response.json").read_bytes()
    fake = opener(fixture)
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "dg-secret")
    s = dictate.resolve_settings({"stt": {"backend": "cloud", "cloud": {"provider": "deepgram"}}}, "Linux")
    assert s["stt_model"] == "nova-3"  # the default model came off the entry
    assert dictate.transcribe(s) == "Hello agent, this is the dictation contract."

    request, timeout = fake.requests[0]
    assert request.full_url.startswith("https://api.deepgram.com/v1/listen?")
    assert "language=en" in request.full_url  # a QUERY parameter here, a form field for OpenAI
    assert request.get_header("Authorization") == "Token dg-secret"  # Token, not Bearer
    assert request.get_header("Content-type") == "audio/wav"  # the WAV is the whole body
    assert request.data == b"RIFFfakewav"
    assert timeout == 60.0


def test_a_deepgram_error_document_degrades_with_deepgrams_own_reason(state, monkeypatch, opener):
    """A quota or auth error must name itself. Deepgram puts the reason in `err_msg`, not in
    `detail` — reading the wrong field is how a degrade becomes a mystery."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"err_code": "INVALID_AUTH", "err_msg": "Token is invalid", "request_id": "abc"}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "dg-wrong")
    s = dictate.resolve_settings({"stt": {"backend": "cloud", "cloud": {"provider": "deepgram"}}}, "Linux")
    assert dictate.transcribe(s) == ""  # cloud said no, whisper is not running in this test
    log_text = (state / "dictate.log").read_text(encoding="utf-8")
    assert "cloud stt returned an error: Token is invalid" in log_text
    assert "cloud stt failed — falling back to local whisper" in log_text
    assert len(fake.requests) == 2  # the cloud attempt, then the one-shot degrade


def test_an_unknown_stt_provider_falls_back_to_the_default_and_says_so(state):
    """A typo used to land on the OpenAI arm of an if/else in silence. Same destination now — the
    historical behaviour — but the log names the typo, which is the whole difference between a
    five-minute fix and a bug report."""
    s = dictate.resolve_settings({"stt": {"cloud": {"provider": "deepgrma"}}}, "Linux")
    assert s["stt_provider"] == "openai"
    assert s["stt_model"] == "whisper-1"
    log_text = (state / "dictate.log").read_text(encoding="utf-8")
    assert "stt.cloud.provider is not a known provider" in log_text
    assert "'deepgrma'" in log_text


def test_the_no_key_message_names_the_provider_and_every_env_it_tried(state, monkeypatch, opener):
    """One message for every provider, listing that provider's own credential chain — so the
    ElevenLabs-only wording (and its LOG_RULES row) does not have to be duplicated per provider."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    monkeypatch.delenv("VOICE_LOOP_STT_API_KEY", raising=False)
    monkeypatch.delenv("VOICE_LOOP_TTS_API_KEY", raising=False)
    opener(b'{"text": "whisper fallback"}')
    s = dictate.resolve_settings({"stt": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}}}, "Linux")
    assert dictate.transcribe(s) == "whisper fallback"
    log_text = (state / "dictate.log").read_text(encoding="utf-8")
    assert "cloud stt: no key for elevenlabs" in log_text
    assert "$VOICE_LOOP_STT_API_KEY" in log_text and "$VOICE_LOOP_TTS_API_KEY" in log_text


def test_a_provider_without_the_shared_credentials_home_names_only_its_own_env(state, monkeypatch, opener):
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    monkeypatch.delenv("VOICE_LOOP_STT_API_KEY", raising=False)
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "an-elevenlabs-key-that-deepgram-must-not-be-handed")
    opener(b'{"text": "whisper fallback"}')
    s = dictate.resolve_settings({"stt": {"backend": "cloud", "cloud": {"provider": "deepgram"}}}, "Linux")
    assert dictate.transcribe(s) == "whisper fallback"
    log_text = (state / "dictate.log").read_text(encoding="utf-8")
    assert "cloud stt: no key for deepgram" in log_text
    assert "VOICE_LOOP_TTS_API_KEY" not in log_text  # not this provider's key to borrow


class _FailingOpener:
    """An opener whose .open() raises — simulating an unreachable server."""
    def open(self, request, timeout=None):
        raise OSError("Network unreachable")


class _FailingConnect:
    """A connect whose call raises WebSocketError — simulating a refused socket, without actually
    opening one. The warning fires BEFORE connect, so a test that only cares about the log can
    skip the network."""
    def __init__(self, reason: str = "connection refused"):
        self._reason = reason

    def __call__(self, url, headers, *, timeout=10.0, connector=None, context=None):
        raise dictate.wsclient.WebSocketError(self._reason)


def test_cloud_network_failure_degrades_to_local_whisper(state, monkeypatch):
    """On a network error the cloud path returns None, and the caller degrades to the
    local whisper server with a logged reason — never a silent dead mic."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")

    # First call (cloud) -> FailingOpener, second call (LAN degrade) -> FakeOpener
    openers = [_FailingOpener(), FakeOpener(b'{"text": "degraded transcript"}')]

    def rotating_build(*args):
        return openers.pop(0)

    monkeypatch.setattr(dictate.urllib.request, "build_opener", rotating_build)
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings({"stt": {"backend": "cloud"}}, "Linux")
    assert dictate.transcribe(s) == "degraded transcript"

    log_text = (state / "dictate.log").read_text(encoding="utf-8")
    assert "cloud stt failed — falling back to local whisper" in log_text


def test_cloud_error_response_degrades_to_local_whisper(state, monkeypatch, opener):
    """An API error document (no `text` field) is a cloud failure, not a silent empty transcript."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    # First response: cloud API error. Second response: LAN degrade success.
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")

    # Build a two-response opener sequence
    class _TwoResponseOpener:
        def __init__(self, responses):
            self._responses = responses
            self.requests: list = []

        def open(self, request, timeout=None):
            self.requests.append((request, timeout))
            return self._responses.pop(0)

    opener_seq = _TwoResponseOpener([
        FakeResponse(b'{"error": {"message": "insufficient_quota"}}'),
        FakeResponse(b'{"text": "whisper stepped in"}'),
    ])
    monkeypatch.setattr(dictate.urllib.request, "build_opener", lambda *handlers: opener_seq)
    s = dictate.resolve_settings({"stt": {"backend": "cloud"}}, "Linux")
    assert dictate.transcribe(s) == "whisper stepped in"

    assert len(opener_seq.requests) == 2
    log_text = (state / "dictate.log").read_text(encoding="utf-8")
    assert "cloud stt failed — falling back to local whisper" in log_text


# --- plaintext endpoint warning: http:// on non-loopback -----------------------------------------


def test_non_loopback_http_batch_endpoint_is_warned(state, monkeypatch, opener):
    """A user-configured http:// endpoint resolves to plaintext — the API key, the raw audio
    and the transcript all travel in the clear. Warn once, and let the call proceed."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": " hello "}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings(
        {"stt": {"backend": "cloud", "cloud": {"endpoint": "http://192.168.1.100:8080"}}}, "Linux"
    )
    assert dictate.transcribe(s) == "hello"
    log_text = _log_of(state)
    assert "cloud stt endpoint is http:// to 192.168.1.100" in log_text
    assert "api key" in log_text.lower()


def test_loopback_http_batch_endpoint_is_silent(state, monkeypatch, opener):
    """The local voice-loop server on http://127.0.0.1 is the DEFAULT and must never warn."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": " hello "}')
    s = dictate.resolve_settings({}, "Linux")  # default: stt.endpoint = http://127.0.0.1:8355
    assert dictate.transcribe(s) == "hello"
    assert "api key" not in _log_of(state).lower()


def test_https_batch_endpoint_is_silent(state, monkeypatch, opener):
    """A correctly configured https endpoint warrants no warning."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": " hello "}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings(
        {"stt": {"backend": "cloud", "endpoint": "https://api.example.com"}}, "Linux"
    )
    assert dictate.transcribe(s) == "hello"
    assert "api key" not in _log_of(state).lower()


# --- the degrade, on every shape of answer that is not a transcript -----------------------------
#
# The whole design of the cloud path is: cloud fails -> degrade to local whisper, logged, never a
# silent dead mic. These cases are the ones where it used to do something else.


def _log_of(state) -> str:
    """The log as text — '' when the run never wrote one, which is itself an assertion some of the
    cases below make (a silent clip must not produce a line at all)."""
    path = state / "dictate.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.parametrize(
    "provider, body",
    [
        ("openai", b'["insufficient_quota", "billing"]'),
        ("elevenlabs", b'"rate limited"'),
        ("deepgram", b'[{"err_msg": "nope"}]'),
    ],
)
def test_a_non_dict_error_document_degrades_rather_than_raising(state, monkeypatch, opener, provider, body):
    """A JSON error document that decodes to a LIST (or a bare string) has no `.get`, and the
    reader that called one raised AttributeError — which `except ValueError` never caught, so
    transcribe() ABORTED on the one path whose whole promise is that it degrades instead.

    Every entry's parsers isinstance-guard now, and this is that promise as a test: a body no
    provider can read still ends at the local whisper server, under a log line naming what came
    back."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(body)
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings({"stt": {"backend": "cloud", "cloud": {"provider": provider}}}, "Linux")

    assert dictate.transcribe(s) == ""  # no whisper server in this test — but it was ASKED

    log_text = _log_of(state)
    assert "cloud stt returned an error" in log_text
    assert "cloud stt failed — falling back to local whisper" in log_text
    assert len(fake.requests) == 2  # the cloud attempt, then the one-shot degrade
    assert fake.requests[1][0].full_url == "http://127.0.0.1:8355/stt?language=en"


@pytest.mark.parametrize("body", [b"", b"null", b"<html>502 Bad Gateway</html>"])
def test_a_body_with_no_document_in_it_degrades_and_says_what_came_back(state, monkeypatch, opener, body):
    """Nothing decodable — an empty body, a JSON `null`, an HTML page from a proxy. All three are
    "the cloud did not answer with a transcript", so all three degrade; the log carries the first
    200 bytes so the operator can tell a proxy page from a null."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(body)
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings({"stt": {"backend": "cloud"}}, "Linux")

    assert dictate.transcribe(s) == ""
    log_text = _log_of(state)
    assert f"cloud stt returned undecodable response: {body[:200]!r}" in log_text
    assert "cloud stt failed — falling back to local whisper" in log_text
    assert len(fake.requests) == 2


@pytest.mark.parametrize(
    "provider, body",
    [
        ("openai", b'{"text": ""}'),
        ("elevenlabs", b'{"text": "   "}'),
        ("deepgram", b'{"results": {"channels": [{"alternatives": [{"transcript": ""}]}]}}'),
    ],
)
def test_a_silent_clip_is_an_empty_transcript_not_a_cloud_failure(state, monkeypatch, opener, provider, body):
    """windowsill#93: a toggle that recorded silence gets `{"text": ""}` back, and that is the
    cloud working. Reading it as an error logged a failure that never happened AND posted the clip
    a second time — a spurious localhost round trip on every empty toggle."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(body)
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings({"stt": {"backend": "cloud", "cloud": {"provider": provider}}}, "Linux")

    assert dictate.transcribe(s) == ""

    assert len(fake.requests) == 1  # the cloud answered; there is nothing to degrade to
    log_text = _log_of(state)
    assert "cloud stt returned an error" not in log_text
    assert "cloud stt failed" not in log_text


@pytest.mark.parametrize(
    "provider, path",
    [
        ("openai", "/v1/audio/transcriptions"),
        ("elevenlabs", "/v1/speech-to-text"),
        ("deepgram", "/v1/listen"),
    ],
)
def test_stt_cloud_endpoint_redirects_the_post_for_every_provider(state, monkeypatch, opener, provider, path):
    """The knob exists for the self-hosted and gateway cases, and a wiring break in it is invisible
    without this: the request still succeeds, it just goes to the vendor instead of where the user
    pointed it. Asserted per provider because the host is resolved on the ENTRY (an explicit
    endpoint beats the provider's own default host, which openai does not even have)."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    fake = opener(b'{"text": "hi", "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]}}')
    monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "sk-secret")
    s = dictate.resolve_settings(
        {"stt": {"backend": "cloud", "cloud": {"provider": provider, "endpoint": "https://gateway.internal"}}},
        "Linux",
    )
    assert s["cloud_endpoint"] == "https://gateway.internal"
    assert dictate.transcribe(s) == "hi"

    request, _ = fake.requests[0]
    assert request.full_url.startswith(f"https://gateway.internal{path}")
    assert len(fake.requests) == 1  # it landed: no degrade


def test_openai_stt_with_no_key_at_all_degrades_to_whisper(state, monkeypatch, opener):
    """The ElevenLabs no-key degrade has been covered since #54; this is the other provider's, and
    it is not the same code path — openai has no `key_env_fallbacks`, so the loop that tries the
    shared TTS key ends after one name. The behaviour it must reach is identical: return None, log
    which env vars were tried, and let transcribe() fall back."""
    (state / "dictate.wav").write_bytes(b"RIFFfakewav")
    monkeypatch.delenv("VOICE_LOOP_STT_API_KEY", raising=False)
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "an-elevenlabs-key-that-openai-must-not-be-handed")
    fake = opener(b'{"text": "whisper fallback"}')
    s = dictate.resolve_settings({"stt": {"backend": "cloud"}}, "Linux")

    assert dictate.transcribe(s) == "whisper fallback"

    assert len(fake.requests) == 1  # only the LAN degrade got past the key check
    assert fake.requests[0][0].full_url == "http://127.0.0.1:8355/stt?language=en"
    log_text = _log_of(state)
    assert "cloud stt: no key for openai" in log_text
    assert "$VOICE_LOOP_STT_API_KEY" in log_text
    assert "VOICE_LOOP_TTS_API_KEY" not in log_text  # not this provider's key to borrow


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


# --- streaming dictation (windowsill#99): the live socket beside the batch POST ------------------
#
# The batch flow makes a long dictation pay twice — speak for a minute, then wait while the whole
# clip uploads and transcribes. The streaming variant forwards the recording to the provider's
# websocket WHILE the microphone is open, so by stop-time the transcript is already assembled.
#
# What is real in these tests and what is not: the socket is REAL (a listening socket on loopback,
# speaking the protocol by hand — see tests/test_wsclient.py, whose server helpers are reused here),
# the URL is built by the REAL registry entry, and the WAV is a real RIFF file. Only the clocks and
# the process table are faked. Nothing reaches the network, a microphone, or the live state dir.


class TestStreamingIsOptIn:
    """Three conditions, none of them a provider name in an `if`."""

    def test_streaming_is_off_by_default(self):
        assert dictate.resolve_settings({}, "Linux")["streaming"] is False

    @pytest.mark.parametrize("value", [True, "true"])
    def test_it_accepts_json_true_and_the_shell_string(self, value):
        s = dictate.resolve_settings({"stt": {"cloud": {"streaming": value}}}, "Linux")
        assert s["streaming"] is True

    @pytest.mark.parametrize("value", [False, "false", "yes", 1, None])
    def test_nothing_else_turns_it_on(self, value):
        s = dictate.resolve_settings({"stt": {"cloud": {"streaming": value}}}, "Linux")
        assert s["streaming"] is False

    def test_the_pcm_shape_travels_with_the_settings(self):
        """A live URL declares what the client is ABOUT to send, and only the client knows that."""
        assert dictate.resolve_settings({}, "Linux")["stream_rate"] == dictate.RECORD_RATE

    def test_a_streaming_provider_with_the_opt_in_and_the_cloud_backend_streams(self, state):
        s = dictate.resolve_settings(
            {"stt": {"backend": "cloud", "cloud": {"provider": "deepgram", "streaming": True}}}, "Linux"
        )
        assert dictate.streaming_wanted(s) is True
        assert _log_of(state) == ""  # the happy path says nothing

    def test_the_opt_in_alone_is_not_enough_without_the_cloud_backend(self):
        s = dictate.resolve_settings(
            {"stt": {"backend": "lan", "cloud": {"provider": "deepgram", "streaming": True}}}, "Linux"
        )
        assert dictate.streaming_wanted(s) is False

    def test_a_local_stt_command_has_no_socket_to_open(self):
        s = dictate.resolve_settings(
            {
                "stt": {
                    "backend": "cloud",
                    "command": "whisper-cli -f",
                    "cloud": {"provider": "deepgram", "streaming": True},
                }
            },
            "Linux",
        )
        assert dictate.streaming_wanted(s) is False

    def test_a_provider_without_a_streaming_variant_says_so_instead_of_ignoring_the_setting(self, state):
        """A silently ignored setting is how a user concludes the feature is broken. The batch path
        still runs — the recording is never at risk — but the log names why."""
        s = dictate.resolve_settings(
            {"stt": {"backend": "cloud", "cloud": {"provider": "openai", "streaming": True}}}, "Linux"
        )
        assert dictate.streaming_wanted(s) is False
        assert "stt.cloud.streaming is on but openai has no streaming variant" in _log_of(state)


class TestWavDataOffset:
    """Where the PCM starts. A live socket is fed RAW samples, so the header must be SKIPPED — and
    skipping a fixed 44 bytes is only right for the canonical layout."""

    def _wav(self, extra_chunks: bytes = b"", data: bytes = b"\x01\x02") -> bytes:
        body = b"WAVEfmt " + struct.pack("<I", 16) + b"\x00" * 16 + extra_chunks
        body += b"data" + struct.pack("<I", len(data)) + data
        return b"RIFF" + struct.pack("<I", len(body) + 4) + body

    def test_a_canonical_header_is_forty_four_bytes(self):
        assert dictate.wav_data_offset(self._wav()) == 44

    def test_a_writer_that_puts_a_chunk_before_data_is_still_found(self):
        """ffmpeg writes a LIST/INFO chunk first, and 44 bytes into one of those files is the
        middle of a metadata chunk — the first quarter-second of every dictation transcribed as
        noise. The `data` chunk is FOUND, never assumed."""
        listing = b"LIST" + struct.pack("<I", 10) + b"INFOhello\x00"
        assert dictate.wav_data_offset(self._wav(listing)) == 44 + len(listing)

    @pytest.mark.parametrize(
        "head",
        [b"", b"not a wav at all", b"RIFF" + b"\x00" * 4 + b"AVI ", b"RIFF" + b"\x00" * 4 + b"WAVEfmt "],
    )
    def test_anything_that_is_not_a_readable_wav_header_is_minus_one(self, head):
        assert dictate.wav_data_offset(head) == -1

    def test_a_data_chunk_whose_size_field_has_not_landed_yet_is_not_a_header(self):
        """The recorder writes the header in pieces; a `data` tag with no length behind it is a
        file caught mid-write, which is a wait, not a failure."""
        assert dictate.wav_data_offset(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"data\x00") == -1


class TestAssembleStreamText:
    def test_finals_join_in_arrival_order(self):
        assert dictate.assemble_stream_text(["Привет,", "это диктовка."]) == "Привет, это диктовка."

    def test_the_empty_finals_a_pause_produces_are_dropped(self):
        """A live socket emits empty finals for the pauses between phrases; joining them naively is
        how a transcript arrives full of double spaces."""
        assert dictate.assemble_stream_text(["one", "", "  ", " two "]) == "one two"

    def test_a_session_that_heard_nothing_assembles_to_nothing(self):
        assert dictate.assemble_stream_text([]) == ""


# --- the session itself, against a fake provider on a real loopback socket -----------------------

# The modules dictate.py itself imported — same objects, so a constant or an error type compared
# here is the one the code under test raised, not a second copy of it.
_OP = dictate.wsclient
providers = dictate.providers


def _results(text: str, *, final: bool) -> bytes:
    """A Deepgram live message, in the shape the registry entry parses."""
    return json.dumps(
        {"type": "Results", "is_final": final, "channel": {"alternatives": [{"transcript": text}]}}
    ).encode("utf-8")


def _wav_bytes(pcm: bytes) -> bytes:
    """A real, canonical 44-byte-header WAV around some PCM."""
    body = b"WAVEfmt " + struct.pack("<I", 16) + b"\x00" * 16 + b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body) + 4) + body


class FakeDeepgram:
    """A listening socket that answers like a live-transcription API.

    It speaks once the audio starts flowing (an interim, then a final — proving interims are not
    assembled twice), and answers CloseStream with the last final the server still owed, a
    Metadata message and a close frame. That order IS the contract the drain exists for: the tail
    of a dictation arrives AFTER the client has stopped sending.
    """

    def __init__(self, *, close_early: bool = False, reset_early: bool = False) -> None:
        self.audio = bytearray()
        self.close_early = close_early
        # reset_early is the DEATH the close frame is not: SO_LINGER 0 makes close() send a TCP
        # RST, so the client meets a dead socket rather than a polite goodbye — which is what a
        # provider dropping out mid-dictation actually looks like.
        self.reset_early = reset_early
        self.server = Server(self._handle)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    def _handle(self, server, conn) -> None:
        head = read_http_head(conn)
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Sec-WebSocket-Accept: " + accept_for(head).encode() + b"\r\n\r\n"
        )
        spoke = False
        buf = bytearray()
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                frame = parse_client_frame(buf)
                if frame is None:
                    break
                opcode, payload = frame
                server.frames.append(frame)
                if opcode == _OP.OP_BINARY:
                    self.audio += payload
                    if self.reset_early:
                        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        conn.close()
                        return
                    if self.close_early:
                        conn.sendall(server_frame(_OP.OP_CLOSE, struct.pack("!H", 1011)))
                        return
                    if not spoke:
                        spoke = True
                        conn.sendall(server_frame(_OP.OP_TEXT, _results("привет это", final=False)))
                        conn.sendall(server_frame(_OP.OP_TEXT, _results("Привет, это", final=True)))
                elif opcode == _OP.OP_TEXT and b"CloseStream" in payload:
                    conn.sendall(server_frame(_OP.OP_TEXT, _results("диктовка.", final=True)))
                    conn.sendall(server_frame(_OP.OP_TEXT, b'{"type":"Metadata","duration":1.5}'))
                    conn.sendall(server_frame(_OP.OP_CLOSE, struct.pack("!H", 1000)))
                    return


def _streaming_settings(endpoint: str) -> dict:
    """Real settings for the real Deepgram entry, pointed at a loopback server."""
    return dictate.resolve_settings(
        {
            "stt": {
                "backend": "cloud",
                "model": "nova-2",
                "language": "ru",
                "cloud": {"provider": "deepgram", "streaming": True, "endpoint": endpoint},
            }
        },
        "Linux",
    )


def _stop_after(calls: int):
    """A stop toggle that arrives after N passes of the loop."""
    seen = [0]

    def stopping() -> bool:
        seen[0] += 1
        return seen[0] > calls

    return stopping


class AdvanceableClock:
    """A clock the test advances explicitly; reading it never consumes a finite script."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TestOpenPcm:
    """The wait for a header the recorder has not finished writing — a wait of milliseconds, and
    the difference between streaming a dictation and declaring the recorder broken."""

    def test_a_header_that_lands_one_poll_late_is_waited_for(self, state):
        wav = state / "dictate.wav"
        wav.write_bytes(b"RIFF" + struct.pack("<I", 0))  # the recorder, caught mid-write
        pcm = b"\x07\x08" * 64
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            wav.write_bytes(_wav_bytes(pcm))  # …and now the header (and its samples) are there

        clock = AdvanceableClock()

        def advance(seconds):
            clock.advance(seconds)
            sleep(seconds)

        handle = dictate._open_pcm(str(wav), advance, clock)
        assert handle is not None
        try:
            assert slept == [dictate.STREAM_IDLE_POLL]  # exactly one poll of patience, not zero
            assert handle.read() == pcm  # and positioned at the first SAMPLE, not the header
        finally:
            handle.close()

    def test_a_recorder_that_never_writes_a_header_gives_up_at_its_bound(self, state):
        clock = AdvanceableClock()
        assert dictate._open_pcm(
            str(state / "never.wav"), lambda seconds: clock.advance(seconds), clock
        ) is None


class TestStreamSessionRoundTrip:
    """The whole streaming path, end to end, over a socket that really exists."""

    def test_the_recording_is_forwarded_as_pcm_and_the_finals_come_back_assembled(self, state):
        pcm = bytes(range(256)) * 80  # 20480 bytes: three chunks and a bit
        (state / "dictate.wav").write_bytes(_wav_bytes(pcm))
        fake = FakeDeepgram()
        s = _streaming_settings(fake.endpoint)
        entry = providers.STT_PROVIDERS["deepgram"]

        result = dictate.run_stream_session(
            s,
            entry,
            "dg-secret",
            stopping=_stop_after(4),
            recorder_alive=lambda: True,
            wav_path=str(state / "dictate.wav"),
        )
        fake.server.stop()

        assert result["status"] == "ok"
        # the interim was NOT assembled — only the two finals, in arrival order
        assert result["text"] == "Привет, это диктовка."
        assert result["finals"] == 2
        assert result["messages"] == 3  # the interim counts as "the server said something"
        assert result["audio_bytes"] == len(pcm)
        # the WAV HEADER never reached the wire: raw samples only, exactly as the URL declared
        assert bytes(fake.audio) == pcm

    def test_the_tail_written_after_the_stop_toggle_is_still_sent(self, state):
        """The stop toggle beats the recorder's last flush by design (there is a 0.2 s settle in
        stop_and_transcribe for exactly that). Whatever landed in the file after the loop broke
        must go out BEFORE CloseStream, or the last words are transcribed by nobody."""
        wav = state / "dictate.wav"
        pcm = b"\x11\x22" * 100
        wav.write_bytes(_wav_bytes(pcm))
        tail = b"\x33\x44" * 4000

        def stopping() -> bool:
            with wav.open("ab") as fh:  # the recorder's flush, landing between two polls
                fh.write(tail)
            return True

        fake = FakeDeepgram()
        result = dictate.run_stream_session(
            _streaming_settings(fake.endpoint),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=stopping,
            recorder_alive=lambda: True,
            wav_path=str(wav),
        )
        fake.server.stop()
        assert result["status"] == "ok"
        assert bytes(fake.audio) == pcm + tail

    def test_a_recorder_that_is_gone_and_a_file_that_stopped_growing_ends_the_session(self, state):
        """The worker's own evidence, for the stop toggle that never comes: no SIGTERM, no
        stopping() — just a dead recorder and a file at rest, on a clock the test owns."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        clock = AdvanceableClock()
        fake = FakeDeepgram()

        def stopping():
            clock.advance(0.5)
            return False

        result = dictate.run_stream_session(
            _streaming_settings(fake.endpoint),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=stopping,
            recorder_alive=lambda: False,
            wav_path=str(state / "dictate.wav"),
            sleep=lambda seconds: clock.advance(seconds),
            clock=clock,
        )
        fake.server.stop()
        assert result["status"] == "ok"
        assert result["text"] == "Привет, это диктовка."

    def test_a_server_that_hangs_up_mid_recording_is_a_degrade_with_a_reason(self, state):
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake = FakeDeepgram(close_early=True)
        result = dictate.run_stream_session(
            _streaming_settings(fake.endpoint),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=_stop_after(20),
            recorder_alive=lambda: True,
            wav_path=str(state / "dictate.wav"),
        )
        fake.server.stop()
        assert result["status"] == "failed"
        assert "closed the stream" in result["reason"]

    def test_a_provider_that_dies_mid_stream_is_a_degrade_with_the_socket_s_own_reason(self, state):
        """THE arms that no other test reaches: the `except wsclient.WebSocketError` around the
        session body, and the send-failed/read-failed raises inside the client that feed it.

        A close frame is a POLITE end and is already covered; this is the other one — SO_LINGER 0
        and close(), i.e. a TCP RST, which is what a provider dropping out actually looks like. The
        next send (or the next poll) meets a dead socket, and the whole point is that the caller
        sees a *result document* rather than a traceback: the recording is on disk and the batch
        path is still there.
        """
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 5000))
        fake = FakeDeepgram(reset_early=True)

        result = dictate.run_stream_session(
            _streaming_settings(fake.endpoint),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=_stop_after(40),
            recorder_alive=lambda: True,
            wav_path=str(state / "dictate.wav"),
        )
        fake.server.stop()

        assert result["status"] == "failed"
        # named by the socket that died, not by a generic shrug — "send failed"/"read failed" are
        # the two ways the client can meet an RST depending on which syscall gets there first
        assert "failed" in result["reason"]
        assert any(word in result["reason"] for word in ("send", "read", "closed")), result["reason"]
        assert result["audio_bytes"] > 0  # it really was mid-stream, not a failure to start

        # …and the stop toggle turns that document into the batch path, with the clip intact.
        _plant_worker(state, result)
        assert dictate.finish_stream_worker() is None
        assert "streaming stt failed" in _log_of(state)

    def test_a_session_nobody_ever_stops_ends_at_its_own_ceiling(self, state):
        """STREAM_MAX_SECONDS, the last resort against a metered socket held open by a hotkey
        nobody pressed again: no stop toggle, a recorder that never dies, and a file at rest. The
        session must end ANYWAY — and end cleanly, because the audio it did send was transcribed."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        # A clock that stands still while the header is opened (STREAM_HEADER_TIMEOUT is three
        # seconds and would otherwise expire before the first read) and then moves in two-minute
        # steps, so the hour-long ceiling is reached in a handful of polls. The BOUND under test is
        # the comparison, not the wall time.
        clock = AdvanceableClock()
        fake = FakeDeepgram()

        def stopping():
            clock.advance(120.0)
            return False

        result = dictate.run_stream_session(
            _streaming_settings(fake.endpoint),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=stopping,                # the stop toggle never comes
            recorder_alive=lambda: True,     # and the recorder never dies
            wav_path=str(state / "dictate.wav"),
            sleep=lambda seconds: clock.advance(120.0),
            clock=clock,
        )
        fake.server.stop()

        # The ceiling is an END, not an abort: what the server had already said is kept and the
        # session closes cleanly. (Only the finals it owed AFTER CloseStream are missing here, and
        # that is this fake clock's doing — the drain is a wall-clock window and a two-minute tick
        # steps straight over it. The drain has its own test in the round trip above.)
        assert result["status"] == "ok"
        assert result["text"] == "Привет, это диктовка."
        assert result["audio_bytes"] > 0

    def test_a_socket_that_will_not_open_is_a_degrade_and_never_an_exception(self, state):
        """Nothing about a failed socket may reach the caller as a traceback: the caller's answer
        to every one of these is the same batch fallback, and a recording is never lost to it."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        result = dictate.run_stream_session(
            _streaming_settings("http://127.0.0.1:1"),  # nothing is listening there, ever
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=lambda: True,
            recorder_alive=lambda: False,
            wav_path=str(state / "dictate.wav"),
        )
        assert result["status"] == "failed"
        assert "could not reach" in result["reason"]
        assert result["text"] == ""

    def test_a_recording_that_never_produced_a_header_is_a_degrade(self, state):
        """The recorder died before writing a WAV at all. The socket opened, so it is closed
        again — and the clip (such as it is) is still on disk for the batch path."""
        fake = FakeDeepgram()
        clock = AdvanceableClock()
        result = dictate.run_stream_session(
            _streaming_settings(fake.endpoint),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=lambda: True,
            recorder_alive=lambda: False,
            wav_path=str(state / "never-written.wav"),
            sleep=lambda seconds: clock.advance(seconds),
            clock=clock,
        )
        fake.server.stop()
        assert result["status"] == "failed"
        assert "no readable WAV header" in result["reason"]

    def test_a_long_quiet_stretch_sends_a_keepalive_rather_than_losing_the_socket(self, state):
        """A vendor closes an idle live socket after ~10 s of silence. A thinking pause in the
        middle of a dictation is exactly that, and it must not end the dictation."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 50))
        clock = AdvanceableClock()
        fake = FakeDeepgram()
        seen = [0]

        def stopping():
            seen[0] += 1
            clock.advance(1.0)
            return seen[0] > 12

        dictate.run_stream_session(
            _streaming_settings(fake.endpoint),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=stopping,
            recorder_alive=lambda: True,
            wav_path=str(state / "dictate.wav"),
            sleep=lambda seconds: clock.advance(1.0),
            clock=clock,
        )
        fake.server.stop()
        keepalives = [payload for opcode, payload in fake.server.frames if b"KeepAlive" in payload]
        assert keepalives, "an idle stretch sent no keepalive — the vendor would have hung up"

    def test_plaintext_non_loopback_streaming_endpoint_is_warned(self, state):
        """A ws:// URL to a non-loopback host carries credentials in the clear — warn before
        the connect attempt, so the warning always reaches the log."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake_connect = _FailingConnect("connection refused")

        result = dictate.run_stream_session(
            _streaming_settings("http://192.168.1.100:8080"),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=lambda: True,
            recorder_alive=lambda: False,
            wav_path=str(state / "dictate.wav"),
            connect=fake_connect,
        )
        assert result["status"] == "failed"
        log_text = _log_of(state)
        assert "streaming stt endpoint is ws:// to 192.168.1.100" in log_text
        assert "api key" in log_text.lower()

    def test_loopback_ws_streaming_endpoint_is_silent(self, state):
        """The local voice-loop server on ws://127.0.0.1 is the DEFAULT local path — never warn."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake_connect = _FailingConnect("connection refused")

        result = dictate.run_stream_session(
            _streaming_settings("http://127.0.0.1:8355"),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=lambda: True,
            recorder_alive=lambda: False,
            wav_path=str(state / "dictate.wav"),
            connect=fake_connect,
        )
        assert result["status"] == "failed"
        assert "api key" not in _log_of(state).lower()

    def test_wss_streaming_endpoint_is_silent(self, state):
        """A correctly configured wss:// endpoint warrants no warning."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake_connect = _FailingConnect("connection refused")

        result = dictate.run_stream_session(
            _streaming_settings("https://api.deepgram.com"),
            providers.STT_PROVIDERS["deepgram"],
            "dg-secret",
            stopping=lambda: True,
            recorder_alive=lambda: False,
            wav_path=str(state / "dictate.wav"),
            connect=fake_connect,
        )
        assert result["status"] == "failed"
        assert "api key" not in _log_of(state).lower()


# --- the worker's lifecycle: spawned by start, stopped by stop, never outliving either -----------


def _plant_worker(state, result: dict | None, *, pid: int = 999999, wrote: int | None = None) -> None:
    """A worker that has already run: its pidfile, and optionally the answer it left behind.

    ``wrote`` is the pid stamped INTO the result document — the fencing token the reader checks.
    It defaults to the worker's own pid (what a real worker writes); a different value is the
    late-document case, where a worker from an earlier recording wrote after this one started.
    """
    (state / "dictate-stream.pid").write_text(str(pid), encoding="utf-8")
    if result is not None:
        document = {**result, "pid": pid if wrote is None else wrote}
        (state / f"dictate-stream.{pid}.json").write_text(json.dumps(document), encoding="utf-8")


class TestFinishStreamWorker:
    """Every way the stop toggle can end up on the batch path, and the one way it does not."""

    @pytest.fixture(autouse=True)
    def _no_signals(self, state, monkeypatch):
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: False)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)

    def test_no_worker_at_all_is_silence_and_the_batch_path(self, state):
        """Streaming was off for this recording: nothing to stop, nothing to say, no degrade to
        announce. A log line here would fire on every dictation of every default install."""
        assert dictate.finish_stream_worker() is None
        assert _log_of(state) == ""

    def test_a_streamed_transcript_is_handed_back_and_the_state_is_cleared(self, state):
        _plant_worker(state, {"status": "ok", "text": "Привет, это диктовка.", "finals": 2, "messages": 3})
        assert dictate.finish_stream_worker() == "Привет, это диктовка."
        assert "streaming stt done: finals=2" in _log_of(state)
        # cleared on the way out: no later recording can adopt this one's transcript (#50)
        assert not list(state.glob("dictate-stream.*.json"))
        assert not (state / "dictate-stream.pid").exists()

    def test_a_failed_session_degrades_with_its_reason(self, state):
        _plant_worker(state, {"status": "failed", "reason": "could not reach api.example:443", "text": ""})
        assert dictate.finish_stream_worker() is None
        assert "streaming stt failed (could not reach" in _log_of(state)

    def test_a_silent_clip_is_a_success_and_not_a_second_transcription(self, state):
        """windowsill#93's distinction, at the streaming seam: the server answered, and what it
        heard was nothing. Posting the same silence to a second engine would be the old bug."""
        _plant_worker(state, {"status": "ok", "text": "", "finals": 0, "messages": 4})
        assert dictate.finish_stream_worker() == ""
        assert "heard nothing back" not in _log_of(state)

    def test_a_stream_that_carried_nothing_at_all_falls_back(self, state):
        """Connected, closed cleanly, and the server never said one word. That is not a silent
        clip — it is a stream that carried nothing, and the clip is still on disk."""
        _plant_worker(state, {"status": "ok", "text": "", "finals": 0, "messages": 0})
        assert dictate.finish_stream_worker() is None
        assert "heard nothing back from the server" in _log_of(state)

    def test_a_worker_that_misses_its_bound_is_killed_and_the_clip_is_used(self, state, monkeypatch):
        """The #50 property: the stop toggle's wait for the child is BOUNDED. A worker wedged on a
        socket must not hold the tail of a dictation open — and must not be left running either."""
        killed: list[int] = []
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: True)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: killed.append(sig))
        clock = AdvanceableClock()
        monkeypatch.setattr(dictate.time, "monotonic", clock)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: clock.advance(seconds))
        _plant_worker(state, None, pid=1234)

        assert dictate.finish_stream_worker() is None
        assert "did not finish within 5s" in _log_of(state)
        assert killed == [dictate.signal.SIGTERM, dictate.signal.SIGKILL]  # asked, then made to
        assert not (state / "dictate-stream.pid").exists()

    def test_a_recycled_pid_is_never_signalled_by_the_stop_toggle(self, state, monkeypatch):
        """The pidfile outlives its process and the kernel hands the number to somebody else. The
        guard costs one /proc read and is the difference between stopping our own child and
        SIGTERMing a stranger of the same user."""
        signalled: list[int] = []
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: False)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: signalled.append(sig))
        clock = AdvanceableClock()
        monkeypatch.setattr(dictate.time, "monotonic", clock)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: clock.advance(seconds))
        _plant_worker(state, None, pid=1234)

        assert dictate.finish_stream_worker() is None  # no answer ever came: the clip is used
        assert signalled == []

    def test_a_document_written_by_an_earlier_worker_is_not_this_stops_answer(self, state, monkeypatch):
        """The fencing check. A worker that outstayed its own recording (a short clip discards it
        without waiting) writes its document LATE — possibly after the next recording has started.
        Its pid is not the one this stop holds, so the text is not adopted: pasting an earlier
        dictation into a later recording is exactly the #50 class of bug."""
        clock = AdvanceableClock()
        monkeypatch.setattr(dictate.time, "monotonic", clock)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: clock.advance(seconds))
        _plant_worker(state, {"status": "ok", "text": "words from the recording before this one"}, wrote=4242)

        assert dictate.finish_stream_worker() is None
        assert "did not finish within" in _log_of(state)

    def test_a_half_written_result_reads_as_no_result_rather_than_as_an_empty_one(self, state, monkeypatch):
        """The worker writes temp-then-replace precisely so this cannot happen — this pins the
        reader's side of that contract: unparseable is 'not finished yet', never 'heard nothing'."""
        _plant_worker(state, None)
        (state / "dictate-stream.999999.json").write_text('{"status": "ok", "text": "half', encoding="utf-8")
        clock = AdvanceableClock()
        monkeypatch.setattr(dictate.time, "monotonic", clock)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: clock.advance(seconds))
        assert dictate.finish_stream_worker() is None
        assert "did not finish within" in _log_of(state)


class TestStreamWorkerState:
    def test_clear_stream_state_sweeps_every_result_document_and_stops_a_survivor(self, state, monkeypatch):
        """Forgetting a stream leaves no transcript behind — including the one nobody holds a pid
        for any more. A worker that wrote its document LATE named it after ITS pid, and by the time
        this runs the pidfile is somebody else's; removing only the pid we hold would leave that
        file, dictated words and all, on disk for good. The sweep is by PATTERN, and the neighbour
        below is why it is anchored on the digits rather than on `dictate-stream.*`."""
        signalled: list[tuple[int, int]] = []
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: True)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
        _plant_worker(state, {"status": "ok", "text": "from a previous recording"}, pid=4321)
        # a predecessor's document, written after its own stop had already cleared up: the pidfile
        # names 4321, so nothing on disk connects this file to any live worker
        orphan = state / "dictate-stream.4242.json"
        orphan.write_text(json.dumps({"status": "ok", "text": "words from two recordings ago"}), encoding="utf-8")
        neighbour = state / "last-spoken"
        neighbour.write_text("a spoken line the sweep has no business touching", encoding="utf-8")

        dictate.clear_stream_state()

        assert signalled == [(4321, dictate.signal.SIGTERM)]
        assert not (state / "dictate-stream.pid").exists()
        assert not (state / "dictate-stream.4321.json").exists()
        assert not orphan.exists()
        assert neighbour.read_text(encoding="utf-8").startswith("a spoken line")

    def test_a_recycled_pid_is_not_signalled(self, state, monkeypatch):
        """Same PID-reuse guard the echo guard uses: a pidfile outlives its process, and the
        kernel hands the number to somebody else."""
        signalled: list[int] = []
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: False)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: signalled.append(pid))
        _plant_worker(state, None, pid=4321)
        dictate.clear_stream_state()
        assert signalled == []

    def test_the_identity_guard_reads_the_workers_own_argv(self):
        argv = "/usr/bin/python3 /opt/voice-loop/scripts/dictate.py stream-worker 4242 "
        assert dictate.pid_looks_like_stream_worker(1, lambda pid: argv, "linux") is True
        assert dictate.pid_looks_like_stream_worker(1, lambda pid: "/usr/bin/python3 speak.py", "linux") is False
        assert dictate.pid_looks_like_stream_worker(1, lambda pid: None, "linux") is False
        # off Linux there is no /proc to ask, and the historical behaviour is kept
        assert dictate.pid_looks_like_stream_worker(1, lambda pid: None, "darwin") is True

    def test_start_spawns_the_worker_with_its_own_argv_and_records_its_pid(self, state, monkeypatch):
        spawned: list[list[str]] = []

        class _Proc:
            pid = 5150

        monkeypatch.setattr(
            dictate.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)) or _Proc()
        )
        dictate.start_stream_worker(4242)

        argv, kwargs = spawned[0]
        assert argv[1].endswith("dictate.py")
        assert argv[2:] == [dictate.STREAM_WORKER_ARG, "4242"]
        assert kwargs["start_new_session"] is True  # a launcher exiting must not end the socket
        assert (state / "dictate-stream.pid").read_text(encoding="utf-8") == "5150"
        assert "stream worker started pid=5150" in _log_of(state)

    def test_a_worker_that_will_not_spawn_is_a_logged_degrade_not_a_failed_recording(self, state, monkeypatch):
        def refuse(argv, **kwargs):
            raise OSError("no fork for you")

        monkeypatch.setattr(dictate.subprocess, "Popen", refuse)
        dictate.start_stream_worker(4242)
        assert "stream worker did not start" in _log_of(state)
        assert not (state / "dictate-stream.pid").exists()  # so the stop toggle uses the clip

    def test_a_worker_whose_pid_cannot_be_recorded_is_stopped_immediately(self, state, monkeypatch):
        """Without the pidfile the stop toggle has no handle on the child at all — and a child
        with a metered socket must never be the thing we lose track of."""
        terminated: list[bool] = []

        class _Proc:
            pid = 5150

            def terminate(self):
                terminated.append(True)

        monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: _Proc())
        monkeypatch.setattr(dictate, "_STREAM_PID_PATH", str(state / "nope" / "dictate-stream.pid"))
        dictate.start_stream_worker(4242)
        assert terminated == [True]
        assert "stream worker pid not recorded" in _log_of(state)

    def test_the_result_is_written_atomically_and_readable_back(self, state):
        dictate._write_stream_result({"status": "ok", "text": "round trip", "finals": 1, "messages": 1})
        assert dictate._read_stream_result()["text"] == "round trip"
        assert not list(state.glob("voice-loop-stream-*"))  # renamed over, never left half-written
        assert oct(os.stat(state / f"dictate-stream.{os.getpid()}.json").st_mode)[-3:] == "600"

    def test_an_unwritable_state_dir_is_logged_and_never_raises(self, state, monkeypatch):
        monkeypatch.setattr(dictate, "_STREAM_RESULT_PATH", str(state / "nope" / "dictate-stream.json"))
        dictate._write_stream_result({"status": "ok"})
        assert "stream result not written" in _log_of(state)

    def test_an_oversized_result_document_is_rejected_not_adopted(self, state, monkeypatch):
        monkeypatch.setattr(dictate, "MAX_STATE_BYTES", 8)
        (state / f"dictate-stream.{os.getpid()}.json").write_bytes(b"x" * 20)
        assert dictate._read_stream_result() is None
        assert "stream result rejected" in _log_of(state)


class TestTheStopToggleUsesWhatTheStreamAssembled:
    """The integration the whole ticket is about, driven through the real stop_and_transcribe."""

    def test_a_streamed_transcript_is_pasted_and_the_clip_is_never_posted(self, state, monkeypatch, paste_run):
        def never(s):
            raise AssertionError("a streamed transcript must not be transcribed a second time")

        monkeypatch.setattr(dictate, "transcribe", never)
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: False)
        _plant_worker(state, {"status": "ok", "text": "стриминговая диктовка", "finals": 1, "messages": 2})

        s = dictate.resolve_settings(
            {"dictate": {"clipboard": "xclip"}, "stt": {"backend": "cloud", "cloud": {"streaming": True}}}, "Linux"
        )
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0

        # both selections, and the words are the SOCKET's, not the batch server's
        assert paste_run.clipboard == ["стриминговая диктовка".encode()] * 2
        log_text = _log_of(state)
        assert "dictation latency stop_to_paste_ms=" in log_text
        assert "via=stream" in log_text
        # the salvage discipline is untouched: the clip is still moved aside, streamed or not
        assert (state / "dictate-last.wav").exists()

    def test_a_failed_stream_falls_back_to_the_recorded_clip(self, state, monkeypatch, paste_run):
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: False)
        _plant_worker(state, {"status": "failed", "reason": "TLS failed for api.example", "text": ""})

        s = dictate.resolve_settings(
            {"dictate": {"clipboard": "xclip"}, "stt": {"backend": "cloud", "cloud": {"streaming": True}}}, "Linux"
        )
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0

        assert paste_run.clipboard == [b"hello agent", b"hello agent"]  # the batch transcript
        log_text = _log_of(state)
        assert "streaming stt failed" in log_text
        assert "via=batch" in log_text

    def test_a_streamed_silent_clip_reports_its_latency_too(self, state, monkeypatch, paste_run):
        """A silent clip is a RESULT, and a streamed one is the fastest result this file produces.
        Leaving it out of the latency ledger is how a path gets a reputation it never earned — and
        it is the one outcome nobody would notice was missing, because the clipboard stays empty."""

        def never(s):
            raise AssertionError("a stream that heard the server fine must not be re-transcribed")

        monkeypatch.setattr(dictate, "transcribe", never)
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: False)
        # status ok, no words, and the server DID speak — windowsill#93's silent clip, streamed
        _plant_worker(state, {"status": "ok", "text": "", "finals": 0, "messages": 4})

        s = dictate.resolve_settings(
            {"dictate": {"clipboard": "xclip"}, "stt": {"backend": "cloud", "cloud": {"streaming": True}}}, "Linux"
        )
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0

        assert paste_run.clipboard == []  # nothing to paste, and nothing pretended otherwise
        log_text = _log_of(state)
        assert "empty transcription" in log_text
        assert "dictation latency stop_to_paste_ms=" in log_text
        assert "via=stream to=nothing" in log_text

    def test_the_batch_path_reports_its_latency_too_so_the_two_are_comparable(self, state, paste_run):
        assert dictate.stop_and_transcribe(_guarded(paste_target="any"), "Linux", "send", 12345) == 0
        assert "dictation latency stop_to_paste_ms=" in _log_of(state)
        assert "via=batch to=paste" in _log_of(state)

    def test_a_clip_below_the_guard_stops_the_worker_without_waiting_for_it(self, state, monkeypatch):
        """A bounced hotkey must stay as cheap as it has always been: there is no transcript
        anybody wants, so the worker is stopped and NOT waited for — while still leaving nothing
        behind for the next recording to adopt."""
        signalled: list[tuple[int, int]] = []
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(dictate, "note", lambda message, system: None)
        monkeypatch.setattr(dictate, "pid_looks_like_stream_worker", lambda pid, **kw: True)
        _plant_worker(state, {"status": "ok", "text": "from the clip that was too short", "messages": 1}, pid=4321)

        assert dictate.stop_and_transcribe(dictate.resolve_settings({}, "Linux"), "Linux", "send", 1) == 0

        assert (4321, dictate.signal.SIGTERM) in signalled  # stopped
        assert "clip too short" in _log_of(state)
        assert "streaming stt" not in _log_of(state)  # and never waited on for an answer
        assert not (state / "dictate-stream.pid").exists()
        assert not list(state.glob("dictate-stream.*.json"))


class TestTheWorkerEntryPoint:
    """`dictate.py stream-worker <pid>` — this script calling itself, and nothing else may."""

    def test_the_subcommand_dispatches_before_the_toggle_machinery(self, state, monkeypatch, tmp_path):
        """Above the debounce and above the pidfile mutex on purpose: the worker is not a toggle
        and never competes for the recording slot (#50)."""
        seen: list[list[str]] = []
        monkeypatch.setattr(dictate, "stream_worker", lambda s, args: seen.append(args) or 0)
        monkeypatch.setattr(dictate, "debounce_toggle", lambda window, now=None: pytest.fail("debounced"))
        _write_config(monkeypatch, tmp_path, {})

        assert dictate.main(["dictate.py", dictate.STREAM_WORKER_ARG, "4242"]) == 0
        assert seen == [["4242"]]
        assert not (state / "dictate.pid").exists()  # it claimed no recording slot

    def test_a_worker_with_no_key_writes_the_reason_and_stops(self, state, monkeypatch):
        monkeypatch.delenv("VOICE_LOOP_STT_API_KEY", raising=False)
        s = dictate.resolve_settings(
            {"stt": {"backend": "cloud", "cloud": {"provider": "deepgram", "streaming": True}}}, "Linux"
        )
        assert dictate.stream_worker(s, ["4242"]) == 1
        result = dictate._read_stream_result()
        assert result["status"] == "failed"
        assert "$VOICE_LOOP_STT_API_KEY" in result["reason"]  # which env var it looked in
        assert "no key" in result["reason"]

    def test_a_worker_for_a_provider_with_no_streaming_variant_stops_before_the_key(self, state, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "unused")
        s = dictate.resolve_settings(
            {"stt": {"backend": "cloud", "cloud": {"provider": "openai", "streaming": True}}}, "Linux"
        )
        assert dictate.stream_worker(s, ["4242"]) == 1
        assert dictate._read_stream_result()["reason"] == "openai has no streaming variant"

    def test_the_worker_runs_the_session_and_writes_its_answer(self, state, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "dg-secret")
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x05\x06" * 500))
        fake = FakeDeepgram()
        s = _streaming_settings(fake.endpoint)
        # Stand in for the SIGTERM the stop toggle sends: the worker's own handler flips the same
        # flag, and what is under test here is everything AROUND the session — the key resolution,
        # the entry lookup, and the one document it writes.
        original = dictate.run_stream_session

        def session(*args, **kwargs):
            kwargs["stopping"] = _stop_after(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(dictate, "run_stream_session", session)
        assert dictate.stream_worker(s, ["4242"]) == 0
        fake.server.stop()

        result = dictate._read_stream_result()
        assert result["status"] == "ok"
        assert result["text"] == "Привет, это диктовка."

    def test_a_worker_told_no_recorder_pid_still_runs_rather_than_crashing(self, state, monkeypatch):
        """argv is a contract with ourselves, and a broken one must degrade like everything else."""
        monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "dg-secret")
        monkeypatch.setattr(dictate, "run_stream_session", lambda *a, **kw: {"status": "failed", "reason": "stub"})
        s = dictate.resolve_settings(
            {"stt": {"backend": "cloud", "cloud": {"provider": "deepgram", "streaming": True}}}, "Linux"
        )
        assert dictate.stream_worker(s, []) == 1
        assert dictate._read_stream_result()["reason"] == "stub"


# --- live preview surface (windowsill#115) --------------------------------------------------------


class TestPreviewConfig:
    """``dictate.preview`` is opt-in, same spelling as every other boolean config key."""

    def test_preview_is_off_by_default(self):
        assert dictate.resolve_settings({}, "Linux")["preview"] is False

    @pytest.mark.parametrize("value", [True, "true"])
    def test_it_accepts_json_true_and_the_shell_string(self, value):
        s = dictate.resolve_settings({"dictate": {"preview": value}}, "Linux")
        assert s["preview"] is True

    @pytest.mark.parametrize("value", [False, "false", "yes", 1, None])
    def test_nothing_else_turns_it_on(self, value):
        s = dictate.resolve_settings({"dictate": {"preview": value}}, "Linux")
        assert s["preview"] is False


class TestPreviewState:
    """The atomic write/clear helpers — the file the preview process reads."""

    def test_write_preview_writes_atomic_json(self, state):
        preview = state / "dictate-preview.json"
        dictate._write_preview({"interim": "Hel", "assembled": "Hello world"}, str(preview))
        assert json.loads(preview.read_text(encoding="utf-8")) == {"interim": "Hel", "assembled": "Hello world"}
        # no temp file left behind: the temp was renamed over, not left half-written
        assert not list(state.glob("voice-loop-preview-*"))

    def test_write_preview_is_best_effort_on_unwritable_dir(self, state, monkeypatch):
        """An unwritable state dir is a silent degrade, never a traceback into the worker."""
        monkeypatch.setattr(dictate, "_PREVIEW_PATH", str(state / "nope" / "dictate-preview.json"))
        dictate._write_preview({"interim": "x"}, str(state / "nope" / "dictate-preview.json"))
        # no raise, no state — silently off

    def test_clear_preview_removes_file(self, state):
        preview = state / "dictate-preview.json"
        preview.write_text('{"interim":"","assembled":"done"}', encoding="utf-8")
        dictate._clear_preview(str(preview))
        assert not preview.exists()

    def test_clear_preview_is_best_effort_when_file_absent(self, state):
        dictate._clear_preview(str(state / "never-existed.json"))
        # no raise


class TestOnInterimCallback:
    """The ``on_interim`` seam in ``run_stream_session`` — what the preview reads from."""

    def test_the_callback_sees_interim_then_final_then_cleared(self, state):
        """An interim arrives (dim guess), then a final settles it (solid), clearing the interim.
        The callback fires after each message so the preview updates in real time."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake = FakeDeepgram()
        s = _streaming_settings(fake.endpoint)
        entry = providers.STT_PROVIDERS["deepgram"]

        calls: list[tuple[str, str]] = []

        def on_interim(interim, assembled):
            calls.append((interim, assembled))

        result = dictate.run_stream_session(
            s, entry, "dg-secret",
            stopping=_stop_after(4),
            recorder_alive=lambda: True,
            wav_path=str(state / "dictate.wav"),
            on_interim=on_interim,
        )
        fake.server.stop()

        assert result["status"] == "ok"
        # At least three calls: the interim ("привет это"), the first final ("Привет, это"),
        # and the trailing final after CloseStream ("диктовка.")
        assert len(calls) >= 3, f"expected at least 3 preview updates, got {len(calls)}: {calls}"
        # The first call is the interim — assembled is empty because no final has landed yet
        assert calls[0] == ("привет это", "")
        # After the first final, the interim is cleared, and the assembled text appears
        assert ("", "Привет, это") in calls
        # After the trailing final, both are cleared — the interim is empty, assembled is complete
        assert calls[-1] == ("", "Привет, это диктовка.")

    def test_no_callback_when_not_set(self, state):
        """The default path — no on_interim — never fires and never raises."""
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake = FakeDeepgram()
        s = _streaming_settings(fake.endpoint)
        entry = providers.STT_PROVIDERS["deepgram"]

        result = dictate.run_stream_session(
            s, entry, "dg-secret",
            stopping=_stop_after(4),
            recorder_alive=lambda: True,
            wav_path=str(state / "dictate.wav"),
            # on_interim omitted — the default None
        )
        fake.server.stop()
        assert result["status"] == "ok"
        # the contract is: it worked, exactly as before — no crash, no side effect


class TestPreviewLifecycle:
    """Preview starts with the recording and clears when the text is delivered."""

    def test_stream_worker_writes_preview_when_enabled(self, state, monkeypatch):
        """When preview is on, the worker emits interims to the preview state file."""
        monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "dg-secret")
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake = FakeDeepgram()
        s = _streaming_settings(fake.endpoint)
        s["preview"] = True

        original = dictate.run_stream_session

        def session(*args, **kwargs):
            kwargs["stopping"] = _stop_after(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(dictate, "run_stream_session", session)
        assert dictate.stream_worker(s, ["4242"]) == 0
        fake.server.stop()

        # The worker wrote the preview file and then cleared it on exit
        assert not (state / "dictate-preview.json").exists()
        # But the worker DID produce a stream result as always
        assert dictate._read_stream_result()["status"] == "ok"

    def test_stream_worker_does_not_write_preview_when_disabled(self, state, monkeypatch):
        """The default — preview off — never touches the preview file."""
        monkeypatch.setenv("VOICE_LOOP_STT_API_KEY", "dg-secret")
        (state / "dictate.wav").write_bytes(_wav_bytes(b"\x01\x02" * 500))
        fake = FakeDeepgram()
        s = _streaming_settings(fake.endpoint)
        s["preview"] = False

        original = dictate.run_stream_session

        def session(*args, **kwargs):
            kwargs["stopping"] = _stop_after(3)
            # confirm no on_interim was wired
            assert kwargs.get("on_interim") is None
            return original(*args, **kwargs)

        monkeypatch.setattr(dictate, "run_stream_session", session)
        assert dictate.stream_worker(s, ["4242"]) == 0
        fake.server.stop()

        assert not (state / "dictate-preview.json").exists()

    def test_preview_is_not_started_without_streaming(self, state, monkeypatch):
        """Preview is only started inside the streaming_wanted guard — batch dictation has no
        interims to show, so there is nothing to render."""
        started: list[bool] = []
        monkeypatch.setattr(dictate, "_start_preview", lambda: started.append(True))
        monkeypatch.setattr(dictate.subprocess, "Popen", lambda argv, **kw: FakeProc())
        monkeypatch.setattr(dictate, "stop_speak_playback", lambda: None)
        monkeypatch.setattr(dictate, "note", lambda message, system: None)
        monkeypatch.setattr(dictate, "streaming_wanted", lambda s: False)  # streaming is OFF
        s = dictate.resolve_settings({"dictate": {"recorder": "arecord", "preview": True}}, "Linux")
        assert dictate.start_recording(s, "Linux", dictate.claim_pidfile()) == 0
        assert started == []  # never started — nothing to preview

    def test_start_preview_spawns_the_process(self, state, monkeypatch):
        """The preview renderer is spawned as a detached child."""
        spawned: list[list[str]] = []

        class _Proc:
            pid = 6000

        monkeypatch.setattr(
            dictate.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)) or _Proc()
        )
        monkeypatch.setattr(dictate, "_PREVIEW_SCRIPT", "/opt/voice-loop/scripts/preview.py")
        dictate._start_preview()

        assert len(spawned) == 1
        argv, kw = spawned[0]
        assert argv[0] == sys.executable
        assert argv[1] == "/opt/voice-loop/scripts/preview.py"
        assert argv[2] == dictate._PREVIEW_PATH
        assert kw["start_new_session"] is True

    def test_start_preview_is_best_effort(self, monkeypatch):
        """A preview that won't start is silence, not a failed recording."""
        def refuse(argv, **kw):
            raise OSError("cannot fork")

        monkeypatch.setattr(dictate.subprocess, "Popen", refuse)
        dictate._start_preview()  # does not raise

    def test_preview_is_cleared_on_min_clip_guard(self, state, monkeypatch):
        """A bounced hotkey clears the preview — there is nothing to transcribe."""
        cleared: list[str] = []
        monkeypatch.setattr(dictate, "_clear_preview", lambda path=None: cleared.append(path or ""))
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(dictate, "note", lambda message, system: None)
        s = dictate.resolve_settings({"dictate": {"preview": True}}, "Linux")
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0
        assert len(cleared) >= 1  # preview was cleared

    def test_preview_is_cleared_on_empty_transcript(self, state, monkeypatch):
        """A silent dictation clears the preview — nothing to put anywhere."""
        (state / "dictate.wav").write_bytes(b"\0" * (dictate.WAV_HEADER_BYTES + dictate.BYTES_PER_SECOND))
        cleared: list[str] = []
        monkeypatch.setattr(dictate, "_clear_preview", lambda path=None: cleared.append(path or ""))
        monkeypatch.setattr(dictate, "transcribe", lambda s: "")
        monkeypatch.setattr(dictate, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(dictate.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(dictate, "note", lambda message, system: None)
        s = dictate.resolve_settings({"dictate": {"clipboard": "xclip", "preview": True}}, "Linux")
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0
        assert len(cleared) >= 1

    def test_preview_is_cleared_after_successful_clipboard_write(self, state, monkeypatch, paste_run):
        """When the text lands on the clipboard (no auto-paste), the preview clears."""
        cleared: list[str] = []
        monkeypatch.setattr(dictate, "_clear_preview", lambda path=None: cleared.append(path or ""))
        s = dictate.resolve_settings({"dictate": {"clipboard": "xclip", "preview": True}}, "Linux")
        assert dictate.stop_and_transcribe(s, "Linux", "paste", 12345) == 0
        # both clipboard selections succeeded AND the preview cleared
        assert paste_run.clipboard == [b"hello agent", b"hello agent"]
        assert len(cleared) >= 1

    def test_preview_is_cleared_after_successful_paste(self, state, monkeypatch, paste_run):
        """Auto-paste success clears the preview — the text is where it goes."""
        cleared: list[str] = []
        monkeypatch.setattr(dictate, "_clear_preview", lambda path=None: cleared.append(path or ""))
        s = _guarded(preview=True)
        assert dictate.stop_and_transcribe(s, "Linux", "send", 12345) == 0
        assert cleared  # preview cleared
