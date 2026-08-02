"""The dictation toggle's pure functions: config precedence, the recorder/clipboard/paste tables,
the min-clip guard, multipart building, response parsing.

dictate.py is glue around a recorder subprocess, an HTTP STT call and desktop paste tools, so —
like speak.py — its runtime contract is proven by real invocation, not by mocks (see TESTING.md).
What IS unit-tested here is the part with no I/O in it at all: the decision tables the shell
version kept in case-statements, now importable functions. Stdlib + pytest only; nothing here
touches the network, a microphone, or the state dir.
"""

from __future__ import annotations

import importlib.util
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
    assert s["language"] == "ru"
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
    assert dictate.resolve_settings({"language": "en"}, "Linux")["language"] == "en"
    both = {"language": "en", "stt": {"language": "de"}}
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
