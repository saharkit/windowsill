"""Voice-loop check manifest for /doctor.

Declares every check the diagnosis engine runs over voice-loop's config,
install ledger, and logs.  This module is the PLUGIN-SPECIFIC half of the
diagnosis contract — it imports nothing from the engine; the engine imports
nothing from here.  The two meet only through the plain-dict shape below.

Adding a check: add a dict to the list.  Each check has at minimum ``bin``,
``key``, ``title``, ``explanation``, and ``check``.

Bin order matters for the user, not for the engine — the engine's own
``Bin`` enum defines the presentation order.  The order within each bin here
is preserved inside the bin.

Config checks check the user's ``~/.config/voice-loop/config.json`` against
known "off" positions — settings whose chosen value explains a missing
feature.  Every one carries ``offer_flip: true`` so the skill can offer the
one-tap fix.

Ledger checks check the install ledger at
``~/.local/state/voice-loop/install.ledger`` — steps that must be done for
the contour to work.

Log checks grep the tails of ``speak.log`` and ``dictate.log`` for known
failure signatures.
"""

from __future__ import annotations

CHECK_MANIFEST: list[dict] = [
    # -----------------------------------------------------------------------
    # Bin 1 — CONSEQUENCE OF YOUR CHOICE (config-driven behaviour)
    # -----------------------------------------------------------------------
    {
        "bin": "consequence_of_choice",
        "key": "auto_paste_off",
        "title": "Auto-paste is disabled",
        "explanation": (
            "You chose manual paste (`dictate.auto_paste: false`) — that is "
            "why dictated text stays on the clipboard instead of inserting "
            "itself into the focused window.  This is the default tier "
            "(zero root, works everywhere)."
        ),
        "fix": (
            "Set `dictate.auto_paste` to `true` in "
            "`~/.config/voice-loop/config.json`.  On macOS this also needs "
            "an Accessibility consent for your terminal app.  On Wayland "
            "(GNOME) it needs `ydotool` (one root step).  The skill will "
            "explain what your setup needs before applying."
        ),
        "offer_flip": True,
        "flip_path": "dictate.auto_paste",
        "flip_value": True,
        "check": {
            "type": "config",
            "path": "dictate.auto_paste",
            "equals": False,
        },
    },
    {
        "bin": "consequence_of_choice",
        "key": "speak_disabled",
        "title": "Speak-back is disabled",
        "explanation": (
            "You set `speak.enabled: false` — that is why the assistant's "
            "lines are silent even when they start with the marker.  This "
            "is a deliberate choice, not a bug."
        ),
        "fix": (
            "Set `speak.enabled` to `true` in "
            "`~/.config/voice-loop/config.json`."
        ),
        "offer_flip": True,
        "flip_path": "speak.enabled",
        "flip_value": True,
        "check": {
            "type": "config",
            "path": "speak.enabled",
            "equals": False,
        },
    },
    {
        "bin": "consequence_of_choice",
        "key": "auto_paste_true_but_paste_target_same_window_wayland",
        "title": "Same-window paste guard on Wayland",
        "explanation": (
            "You set `dictate.paste_target: \"same-window\"` on a Wayland "
            "session — but Wayland has no portable focus query, so the "
            "guard degrades to `\"any\"`.  Your dictated text lands in "
            "whatever window is focused when you stop recording, exactly "
            "as if you had chosen `\"any\"`."
        ),
        "fix": (
            "This is not configurable on Wayland.  Either accept the `\"any\"` "
            "behaviour (the default), or set `auto_paste: false` to keep "
            "text on the clipboard for manual paste."
        ),
        "offer_flip": False,
        "check": {
            "type": "config",
            "path": "dictate.paste_target",
            "equals": "same-window",
        },
    },
    {
        "bin": "consequence_of_choice",
        "key": "stt_command_set",
        "title": "Custom STT command is set",
        "explanation": (
            "`stt.command` is set — speech recognition uses your own "
            "command instead of the built-in paths.  If the contour "
            "never transcribes anything, check that this command is "
            "installed and works when called by hand on a WAV file."
        ),
        "fix": (
            "Test your command manually: `<command> /path/to/test.wav` "
            "should print a transcript.  Clear `stt.command` in the config "
            "to switch back to the HTTP or cloud path."
        ),
        "offer_flip": False,
        "check": {
            "type": "config",
            "path": "stt.command",
            "not_equals": "",
        },
    },
    {
        "bin": "consequence_of_choice",
        "key": "tts_command_set",
        "title": "Custom TTS command is set",
        "explanation": (
            "`tts.command` is set — speech synthesis uses your own "
            "command instead of the built-in paths.  If the assistant "
            "is silent even though lines start with the marker, check "
            "that this command is installed and produces sound when "
            "fed text on stdin."
        ),
        "fix": (
            "Test your command manually: `echo 'test' | <command>`.  "
            "Clear `tts.command` in the config to switch back to the "
            "HTTP or cloud path."
        ),
        "offer_flip": False,
        "check": {
            "type": "config",
            "path": "tts.command",
            "not_equals": "",
        },
    },
    # -----------------------------------------------------------------------
    # Bin 2 — UNFINISHED INSTALL (step ledger)
    # -----------------------------------------------------------------------
    {
        "bin": "unfinished_install",
        "key": "install_incomplete_or_not_started",
        "title": "Voice-loop install is incomplete",
        "explanation": (
            "The install step ledger shows that one or more steps were "
            "never completed.  An interruption during `/voice-setup` "
            "leaves the contour in a state where some pieces are wired "
            "and others are not — the most common cause of 'it worked "
            "before the interruption and now it does not.'"
        ),
        "fix": (
            "Run `/voice-setup` — it will detect the interrupted state "
            "and offer to resume from the last completed step.  No "
            "work will be repeated."
        ),
        "offer_flip": False,
        "check": {
            "type": "ledger",
            "steps": [
                "step-0-probe",
                "step-1-language",
                "step-2-backends",
                "step-3-install-deps",
                "step-4-write-config",
                "step-5-paste-behaviour",
                "step-6-hotkey",
                "step-7-speak-convention",
                "step-8-verify",
            ],
            "require_all": False,
        },
    },
    {
        "bin": "unfinished_install",
        "key": "install_deps_incomplete",
        "title": "Server or packages are not installed",
        "explanation": (
            "Step 3 (install dependencies) is not marked complete.  "
            "Without the speech server or the tool packages, the "
            "contour cannot speak or transcribe."
        ),
        "fix": (
            "Run `/voice-setup` — it will resume from step 3 and "
            "install the missing pieces."
        ),
        "offer_flip": False,
        "check": {
            "type": "ledger",
            "steps": ["step-3-install-deps"],
        },
    },
    {
        "bin": "unfinished_install",
        "key": "config_not_written",
        "title": "Config file was never written",
        "explanation": (
            "Step 4 (write config) is not marked complete.  Without "
            "`~/.config/voice-loop/config.json`, the scripts have no "
            "backends, no language, and nothing to run."
        ),
        "fix": (
            "Run `/voice-setup` — it will resume from step 4 and "
            "write `config.json` with the choices you made."
        ),
        "offer_flip": False,
        "check": {
            "type": "ledger",
            "steps": ["step-4-write-config"],
        },
    },
    # -----------------------------------------------------------------------
    # Bin 3 — REAL ANOMALY (log patterns that signal a genuine defect)
    # -----------------------------------------------------------------------
    {
        "bin": "real_anomaly",
        "key": "server_unreachable",
        "title": "The speech server is unreachable",
        "explanation": (
            "The log shows synthesis or recognition failures because "
            "the server at the configured endpoint could not be "
            "reached.  This is not a config choice — either the "
            "service is stopped, the endpoint is wrong, or the "
            "network path is broken."
        ),
        "fix": (
            "Check that the server is running: "
            "`systemctl --user status voice-loop.service` (Linux) or "
            "`curl http://127.0.0.1:8355/health`.  If it is stopped, "
            "start it: `systemctl --user start voice-loop.service`."
        ),
        "offer_flip": False,
        "check": {
            "type": "log",
            "source": "speak.log",
            "pattern": "unreachable",
        },
    },
    {
        "bin": "real_anomaly",
        "key": "player_failed",
        "title": "The audio player is failing",
        "explanation": (
            "The log shows player failures — the synthesis succeeded "
            "but the audio could not be played.  The configured player "
            "(`speak.player`) may be missing, or the audio device may "
            "not be available."
        ),
        "fix": (
            "Check that the player in `speak.player` is installed: "
            "`command -v <player>`.  The default is `aplay` (Linux) "
            "or `afplay` (macOS).  Check the audio device with "
            "`aplay -l` or check that the macOS output is not muted."
        ),
        "offer_flip": False,
        "check": {
            "type": "log",
            "source": "speak.log",
            "pattern": "player failed",
        },
    },
    {
        "bin": "real_anomaly",
        "key": "recorder_failed",
        "title": "The audio recorder is failing",
        "explanation": (
            "The log shows recorder failures — dictation cannot "
            "capture audio.  The configured recorder (`dictate.recorder` "
            "or the auto-detected one) may be missing or the microphone "
            "may not be available."
        ),
        "fix": (
            "Check that a recorder is installed: `pw-record` (PipeWire), "
            "`arecord` (ALSA), `sox` (macOS).  Check that the microphone "
            "is not muted and is available to the user."
        ),
        "offer_flip": False,
        "check": {
            "type": "log",
            "source": "dictate.log",
            "pattern": "recorder failed",
        },
    },
    {
        "bin": "real_anomaly",
        "key": "stt_unreachable",
        "title": "Speech recognition is unreachable",
        "explanation": (
            "The log shows STT (speech-to-text) failures — dictation "
            "cannot convert audio to text.  The STT endpoint may be "
            "wrong, the service may be stopped, or the configured "
            "cloud key may be missing or invalid."
        ),
        "fix": (
            "Check the STT endpoint: `curl <stt.endpoint>/health`.  "
            "For cloud STT, check that the key file exists or the "
            "named environment variable is set."
        ),
        "offer_flip": False,
        "check": {
            "type": "log",
            "source": "dictate.log",
            "pattern": "stt unreachable",
        },
    },
    {
        "bin": "real_anomaly",
        "key": "streaming_stt_degraded",
        "title": "Streaming dictation fell back to the recorded clip",
        "explanation": (
            "You asked for streaming dictation (`stt.cloud.streaming: "
            "true`), but the live socket failed and the clip was "
            "transcribed the ordinary way instead.  Nothing was lost — "
            "the recording is always written to disk first — but the "
            "end-of-speech wait is the one streaming was meant to "
            "remove, and it is back."
        ),
        "fix": (
            "Read the reason on the `streaming stt failed (…)` line in "
            "`~/.local/state/voice-loop/dictate.log`.  An auth refusal "
            "means the key is wrong for this provider; 'could not "
            "reach' means the host or `stt.cloud.endpoint` is "
            "unreachable from here; 'has no streaming variant' means "
            "the configured provider does not stream and the setting "
            "is doing nothing."
        ),
        "offer_flip": False,
        "check": {
            "type": "log",
            "source": "dictate.log",
            "pattern": "streaming stt failed",
        },
    },
    {
        "bin": "real_anomaly",
        "key": "no_recorder_available",
        "title": "No audio recorder found",
        "explanation": (
            "The log shows that no recorder was found on this machine.  "
            "Without one, dictation cannot capture audio at all."
        ),
        "fix": (
            "Install a recorder: on Linux with PipeWire, `sudo apt install "
            "pipewire-utils` (or the equivalent for your distro); on "
            "ALSA, `alsa-utils`; on macOS, `brew install sox`."
        ),
        "offer_flip": False,
        "check": {
            "type": "log",
            "source": "dictate.log",
            "pattern": "no recorder available",
        },
    },
    {
        "bin": "real_anomaly",
        "key": "many_unclassified_log_lines",
        "title": "Log collector is out of date",
        "explanation": (
            "The log tail contains lines the collector's vocabulary "
            "table does not recognise.  This means the scripts on this "
            "machine are a different version from the collector — "
            "a genuine anomaly because future log patterns cannot be "
            "diagnosed."
        ),
        "fix": (
            "Update the plugin: run `/plugin install voice-loop@windowsill` "
            "again to pull the latest version.  If the issue persists, "
            "run `/report-bug` with the symptom and the unclassified-line "
            "count from this report."
        ),
        "offer_flip": False,
        "check": {
            "type": "log",
            "source": "dictate.log",
            "pattern": "unexpected error",
            "count": 1,
        },
    },
]
