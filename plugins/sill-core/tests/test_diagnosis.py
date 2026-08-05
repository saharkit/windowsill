"""Tests for sill_core.diagnosis — three-bin diagnosis engine.

No network, no models, no audio hardware, no voice-loop specifics.
The engine is tested against synthetic manifests and data — config dicts,
ledger dicts, and log tails built in the test, never read from a real user's
filesystem.

Acceptance criteria from the brief:
- each bin has a scripted scenario test (a chosen-off feature, a truncated
  install ledger, an injected log anomaly)
- /doctor names the right bin with the right explanation
- a grep-audit test proves the engine module imports nothing voice-specific
"""

from __future__ import annotations

import ast
import sys
from typing import Any

import pytest

from sill_core.diagnosis import (
    Bin,
    Finding,
    _MISSING,
    _resolve_path,
    _run_config_check,
    _run_ledger_check,
    _run_log_check,
    diagnose,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_by_key(findings: list[Finding], key: str) -> Finding | None:
    for f in findings:
        if f.key == key:
            return f
    return None


# ---------------------------------------------------------------------------
# Bin enum — addressable vocabulary
# ---------------------------------------------------------------------------


class TestBin:
    def test_three_bins_in_order(self) -> None:
        bins = list(Bin)
        assert len(bins) == 3
        assert bins[0] == Bin.CONSEQUENCE_OF_CHOICE
        assert bins[1] == Bin.UNFINISHED_INSTALL
        assert bins[2] == Bin.REAL_ANOMALY

    def test_bin_values_are_strings(self) -> None:
        assert Bin.CONSEQUENCE_OF_CHOICE.value == "consequence_of_choice"
        assert Bin.UNFINISHED_INSTALL.value == "unfinished_install"
        assert Bin.REAL_ANOMALY.value == "real_anomaly"


# ---------------------------------------------------------------------------
# _resolve_path — dot-separated dict navigation
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_top_level_key(self) -> None:
        assert _resolve_path({"a": 1}, "a") == 1

    def test_nested_key(self) -> None:
        data = {"dictate": {"auto_paste": False}}
        assert _resolve_path(data, "dictate.auto_paste") is False

    def test_missing_intermediate(self) -> None:
        assert _resolve_path({"a": 1}, "a.b.c") is _MISSING

    def test_missing_leaf(self) -> None:
        assert _resolve_path({"a": {}}, "a.b") is _MISSING

    def test_intermediate_is_not_dict(self) -> None:
        assert _resolve_path({"a": 1}, "a.b") is _MISSING


# ---------------------------------------------------------------------------
# _run_config_check
# ---------------------------------------------------------------------------


class TestConfigCheck:
    def test_triggers_on_equals_match(self) -> None:
        check = {"type": "config", "path": "dictate.auto_paste", "equals": False}
        cfg = {"dictate": {"auto_paste": False}}
        result = _run_config_check(check, cfg)
        assert result is not None
        assert result["path"] == "dictate.auto_paste"
        assert result["value"] is False

    def test_passes_when_value_differs(self) -> None:
        check = {"type": "config", "path": "dictate.auto_paste", "equals": False}
        cfg = {"dictate": {"auto_paste": True}}
        assert _run_config_check(check, cfg) is None

    def test_triggers_on_falsy_when_no_equals(self) -> None:
        check = {"type": "config", "path": "speak.enabled"}
        cfg = {"speak": {"enabled": False}}
        result = _run_config_check(check, cfg)
        assert result is not None

    def test_triggers_on_falsy_when_no_equals_or_not_equals(self) -> None:
        # Empty string is falsy → triggers when no condition is specified
        check = {"type": "config", "path": "stt.command"}
        cfg = {"stt": {"command": ""}}
        result = _run_config_check(check, cfg)
        assert result is not None  # "" is falsy → triggers

    def test_not_equals_trigger(self) -> None:
        # not_equals triggers when value differs
        check = {"type": "config", "path": "stt.command", "not_equals": ""}
        cfg_set = {"stt": {"command": "/usr/local/bin/mystt"}}
        result = _run_config_check(check, cfg_set)
        assert result is not None  # value != "" → triggers

    def test_not_equals_passes_when_empty(self) -> None:
        check = {"type": "config", "path": "stt.command", "not_equals": ""}
        cfg_empty = {"stt": {"command": ""}}
        assert _run_config_check(check, cfg_empty) is None  # value == "" → passes

    def test_passes_when_path_missing(self) -> None:
        check = {"type": "config", "path": "nonexistent.key", "equals": False}
        assert _run_config_check(check, {}) is None


# ---------------------------------------------------------------------------
# _run_ledger_check
# ---------------------------------------------------------------------------


class TestLedgerCheck:
    def test_triggers_when_step_incomplete(self) -> None:
        check = {
            "type": "ledger",
            "steps": ["step-3-install-deps"],
        }
        ledger = {
            "state": "in_progress",
            "completed_steps": ["step-0-probe", "step-1-language"],
        }
        result = _run_ledger_check(check, ledger)
        assert result is not None
        assert result["incomplete_steps"] == ["step-3-install-deps"]

    def test_passes_when_step_complete(self) -> None:
        check = {
            "type": "ledger",
            "steps": ["step-3-install-deps"],
        }
        ledger = {
            "state": "in_progress",
            "completed_steps": ["step-0-probe", "step-3-install-deps"],
        }
        assert _run_ledger_check(check, ledger) is None

    def test_triggers_when_none_complete_with_require_all_false(self) -> None:
        check = {
            "type": "ledger",
            "steps": ["step-3-install-deps", "step-4-write-config"],
            "require_all": False,
        }
        ledger = {"state": "none", "completed_steps": []}
        result = _run_ledger_check(check, ledger)
        assert result is not None
        assert len(result["incomplete_steps"]) == 2

    def test_passes_when_one_complete_with_require_all_false(self) -> None:
        check = {
            "type": "ledger",
            "steps": ["step-3-install-deps", "step-4-write-config"],
            "require_all": False,
        }
        ledger = {
            "state": "in_progress",
            "completed_steps": ["step-3-install-deps"],
        }
        assert _run_ledger_check(check, ledger) is None

    def test_includes_current_step_in_evidence(self) -> None:
        check = {
            "type": "ledger",
            "steps": ["step-3-install-deps"],
        }
        ledger = {
            "state": "in_progress",
            "completed_steps": [],
            "current_step": "step-2-backends",
        }
        result = _run_ledger_check(check, ledger)
        assert result is not None
        assert result["current_step"] == "step-2-backends"


# ---------------------------------------------------------------------------
# _run_log_check
# ---------------------------------------------------------------------------


class TestLogCheck:
    def test_triggers_when_pattern_found(self) -> None:
        check = {"type": "log", "source": "speak.log", "pattern": "unreachable"}
        logs = {"speak.log": ["2026-08-05 error: synthesis unreachable: refused", "ok"]}
        result = _run_log_check(check, logs)
        assert result is not None
        assert result["match_count"] == 1

    def test_passes_when_pattern_absent(self) -> None:
        check = {"type": "log", "source": "speak.log", "pattern": "unreachable"}
        logs = {"speak.log": ["all good", "no problems"]}
        assert _run_log_check(check, logs) is None

    def test_passes_when_source_missing(self) -> None:
        check = {"type": "log", "source": "nonexistent.log", "pattern": "error"}
        assert _run_log_check(check, {}) is None

    def test_count_minimum(self) -> None:
        check = {"type": "log", "source": "log", "pattern": "error", "count": 3}
        logs = {"log": ["error a", "error b"]}
        assert _run_log_check(check, logs) is None

        logs2 = {"log": ["error a", "error b", "error c"]}
        assert _run_log_check(check, logs2) is not None


# ---------------------------------------------------------------------------
# diagnose() — end-to-end engine
# ---------------------------------------------------------------------------


class TestDiagnose:
    def test_empty_manifest_returns_empty(self) -> None:
        assert diagnose([]) == []

    def test_unknown_bin_skipped(self) -> None:
        manifest = [{
            "bin": "nonexistent",
            "key": "x",
            "title": "x",
            "explanation": "x",
            "check": {"type": "config", "path": "x", "equals": True},
        }]
        assert diagnose(manifest, config={"x": True}) == []

    def test_unknown_check_type_skipped(self) -> None:
        manifest = [{
            "bin": "real_anomaly",
            "key": "x",
            "title": "x",
            "explanation": "x",
            "check": {"type": "nonexistent"},
        }]
        assert diagnose(manifest) == []


# ---------------------------------------------------------------------------
# Scenario 1 — consequence of your choice
# ---------------------------------------------------------------------------


class TestScenarioConsequenceOfChoice:
    """A config feature deliberately turned off → /doctor names the right bin."""

    def test_auto_paste_off_is_consequence_of_choice(self) -> None:
        manifest = [
            {
                "bin": "consequence_of_choice",
                "key": "auto_paste_off",
                "title": "Auto-paste is disabled",
                "explanation": (
                    "You chose manual paste — that is why text does not "
                    "insert itself after dictation."
                ),
                "fix": "Set `dictate.auto_paste` to `true`.",
                "offer_flip": True,
                "flip_path": "dictate.auto_paste",
                "flip_value": True,
                "check": {
                    "type": "config",
                    "path": "dictate.auto_paste",
                    "equals": False,
                },
            },
        ]
        config = {"dictate": {"auto_paste": False}}

        findings = diagnose(manifest, config=config)

        assert len(findings) == 1
        f = findings[0]
        assert f.bin == Bin.CONSEQUENCE_OF_CHOICE
        assert f.key == "auto_paste_off"
        assert f.title == "Auto-paste is disabled"
        assert "manual paste" in f.explanation
        assert f.offer_flip is True
        assert f.flip_path == "dictate.auto_paste"
        assert f.flip_value is True
        assert f.evidence["value"] is False

    def test_speak_disabled_is_consequence_of_choice(self) -> None:
        manifest = [
            {
                "bin": "consequence_of_choice",
                "key": "speak_off",
                "title": "Speak-back is disabled",
                "explanation": "You chose to disable speak-back.",
                "fix": "Set `speak.enabled` to `true`.",
                "offer_flip": True,
                "flip_path": "speak.enabled",
                "flip_value": True,
                "check": {
                    "type": "config",
                    "path": "speak.enabled",
                    "equals": False,
                },
            },
        ]

        # Auto-paste ON, speak OFF → only speak fires
        config = {"dictate": {"auto_paste": True}, "speak": {"enabled": False}}
        findings = diagnose(manifest, config=config)

        assert len(findings) == 1
        assert findings[0].key == "speak_off"

    def test_healthy_config_produces_no_findings(self) -> None:
        manifest = [
            {
                "bin": "consequence_of_choice",
                "key": "auto_paste_off",
                "title": "Auto-paste is disabled",
                "explanation": "...",
                "check": {
                    "type": "config",
                    "path": "dictate.auto_paste",
                    "equals": False,
                },
            },
            {
                "bin": "consequence_of_choice",
                "key": "speak_off",
                "title": "Speak-back is disabled",
                "explanation": "...",
                "check": {
                    "type": "config",
                    "path": "speak.enabled",
                    "equals": False,
                },
            },
        ]
        config = {"dictate": {"auto_paste": True}, "speak": {"enabled": True}}
        findings = diagnose(manifest, config=config)
        assert findings == []


# ---------------------------------------------------------------------------
# Scenario 2 — unfinished install
# ---------------------------------------------------------------------------


class TestScenarioUnfinishedInstall:
    """A truncated install ledger → /doctor names bin 2 with the right explanation."""

    def test_incomplete_install_is_unfinished_install(self) -> None:
        manifest = [
            {
                "bin": "unfinished_install",
                "key": "install_incomplete",
                "title": "Voice-loop install is incomplete",
                "explanation": (
                    "The install step ledger shows that one or more steps "
                    "were never completed."
                ),
                "fix": "Run `/voice-setup` to resume.",
                "check": {
                    "type": "ledger",
                    "steps": [
                        "step-0-probe",
                        "step-1-language",
                        "step-2-backends",
                        "step-3-install-deps",
                    ],
                    "require_all": True,
                },
            },
        ]
        # Only steps 0 and 1 done, ledger says "in_progress"
        ledger = {
            "state": "in_progress",
            "completed_steps": ["step-0-probe", "step-1-language"],
        }

        findings = diagnose(manifest, ledger=ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.bin == Bin.UNFINISHED_INSTALL
        assert f.key == "install_incomplete"
        assert "install step ledger" in f.explanation.lower()
        assert "/voice-setup" in f.fix
        assert "step-2-backends" in f.evidence["incomplete_steps"]
        assert "step-3-install-deps" in f.evidence["incomplete_steps"]

    def test_no_ledger_at_all_is_unfinished_install(self) -> None:
        manifest = [
            {
                "bin": "unfinished_install",
                "key": "install_incomplete",
                "title": "Install incomplete",
                "explanation": "...",
                "check": {
                    "type": "ledger",
                    "steps": ["step-3-install-deps"],
                },
            },
        ]
        findings = diagnose(manifest, ledger={"state": "none", "completed_steps": []})
        assert len(findings) == 1
        assert findings[0].bin == Bin.UNFINISHED_INSTALL

    def test_complete_install_produces_no_ledger_findings(self) -> None:
        manifest = [
            {
                "bin": "unfinished_install",
                "key": "install_incomplete",
                "title": "Install incomplete",
                "explanation": "...",
                "check": {
                    "type": "ledger",
                    "steps": ["step-3-install-deps"],
                },
            },
        ]
        ledger = {
            "state": "complete",
            "completed_steps": ["step-3-install-deps"],
        }
        findings = diagnose(manifest, ledger=ledger)
        assert findings == []


# ---------------------------------------------------------------------------
# Scenario 3 — real anomaly (injected log anomaly)
# ---------------------------------------------------------------------------


class TestScenarioRealAnomaly:
    """An injected log anomaly → /doctor names bin 3 with the right explanation."""

    def test_server_unreachable_is_real_anomaly(self) -> None:
        manifest = [
            {
                "bin": "real_anomaly",
                "key": "server_unreachable",
                "title": "Speech server unreachable",
                "explanation": (
                    "The log shows synthesis failures because the server "
                    "at the configured endpoint could not be reached."
                ),
                "fix": "Check that the server is running.",
                "check": {
                    "type": "log",
                    "source": "speak.log",
                    "pattern": "unreachable",
                },
            },
        ]
        logs = {
            "speak.log": [
                "2026-08-05T12:00:00 synthesis unreachable: Connection refused",
                "2026-08-05T12:00:01 played rc=0",
            ],
        }

        findings = diagnose(manifest, logs=logs)

        assert len(findings) == 1
        f = findings[0]
        assert f.bin == Bin.REAL_ANOMALY
        assert f.key == "server_unreachable"
        assert "synthesis failures" in f.explanation.lower()
        assert f.evidence["match_count"] == 1
        assert "unreachable" in f.evidence["matches"][0]

    def test_log_without_anomaly_produces_no_findings(self) -> None:
        manifest = [
            {
                "bin": "real_anomaly",
                "key": "server_unreachable",
                "title": "...",
                "explanation": "...",
                "check": {
                    "type": "log",
                    "source": "speak.log",
                    "pattern": "unreachable",
                },
            },
        ]
        logs = {"speak.log": ["2026-08-05 played rc=0", "timings extract_ms=50"]}
        findings = diagnose(manifest, logs=logs)
        assert findings == []


# ---------------------------------------------------------------------------
# Ordering — findings are sorted by bin, manifest order preserved within bin
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_findings_sorted_by_bin_order(self) -> None:
        manifest = [
            {
                "bin": "real_anomaly",
                "key": "anomaly_a",
                "title": "A",
                "explanation": "...",
                "check": {"type": "log", "source": "log", "pattern": "a"},
            },
            {
                "bin": "consequence_of_choice",
                "key": "choice_a",
                "title": "A",
                "explanation": "...",
                "check": {"type": "config", "path": "flag", "equals": True},
            },
            {
                "bin": "unfinished_install",
                "key": "install_a",
                "title": "A",
                "explanation": "...",
                "check": {"type": "ledger", "steps": ["s1"]},
            },
            {
                "bin": "consequence_of_choice",
                "key": "choice_b",
                "title": "B",
                "explanation": "...",
                "check": {"type": "config", "path": "flag2", "equals": True},
            },
        ]
        config = {"flag": True, "flag2": True}
        ledger: dict[str, Any] = {"state": "in_progress", "completed_steps": []}
        logs = {"log": ["a"]}

        findings = diagnose(manifest, config=config, ledger=ledger, logs=logs)

        assert len(findings) == 4
        bins = [f.bin for f in findings]
        assert bins[0] == Bin.CONSEQUENCE_OF_CHOICE
        assert bins[1] == Bin.CONSEQUENCE_OF_CHOICE
        assert bins[2] == Bin.UNFINISHED_INSTALL
        assert bins[3] == Bin.REAL_ANOMALY

        # Within each bin, manifest order is preserved
        choice_keys = [f.key for f in findings if f.bin == Bin.CONSEQUENCE_OF_CHOICE]
        assert choice_keys == ["choice_a", "choice_b"]


# ---------------------------------------------------------------------------
# Full three-bin scenario — all three bins fire at once
# ---------------------------------------------------------------------------


class TestFullThreeBinScenario:
    def test_all_three_bins_fire(self) -> None:
        """A machine with auto_paste off, a truncated install, and log errors."""
        manifest = [
            {
                "bin": "consequence_of_choice",
                "key": "auto_paste_off",
                "title": "Auto-paste off",
                "explanation": "You chose manual paste.",
                "fix": "Set auto_paste to true.",
                "offer_flip": True,
                "flip_path": "dictate.auto_paste",
                "flip_value": True,
                "check": {"type": "config", "path": "dictate.auto_paste", "equals": False},
            },
            {
                "bin": "unfinished_install",
                "key": "incomplete",
                "title": "Install incomplete",
                "explanation": "The ledger shows incomplete steps.",
                "fix": "Run /voice-setup.",
                "check": {"type": "ledger", "steps": ["step-3-install-deps"]},
            },
            {
                "bin": "real_anomaly",
                "key": "player_failed",
                "title": "Player failed",
                "explanation": "The audio player is failing.",
                "fix": "Check the player.",
                "check": {"type": "log", "source": "speak.log", "pattern": "player failed"},
            },
        ]
        config = {"dictate": {"auto_paste": False}}
        ledger = {"state": "in_progress", "completed_steps": []}
        logs = {"speak.log": ["2026-08-05 player failed: aplay not found"]}

        findings = diagnose(manifest, config=config, ledger=ledger, logs=logs)

        assert len(findings) == 3
        assert findings[0].bin == Bin.CONSEQUENCE_OF_CHOICE
        assert findings[1].bin == Bin.UNFINISHED_INSTALL
        assert findings[2].bin == Bin.REAL_ANOMALY


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------


class TestFinding:
    def test_defaults(self) -> None:
        f = Finding(
            bin=Bin.CONSEQUENCE_OF_CHOICE,
            key="test",
            title="Test",
            explanation="...",
        )
        assert f.fix == ""
        assert f.offer_flip is False
        assert f.flip_path == ""
        assert f.flip_value is None
        assert f.evidence == {}

    def test_with_flip(self) -> None:
        f = Finding(
            bin=Bin.CONSEQUENCE_OF_CHOICE,
            key="test",
            title="Test",
            explanation="...",
            fix="Set it.",
            offer_flip=True,
            flip_path="dictate.auto_paste",
            flip_value=True,
            evidence={"path": "dictate.auto_paste", "value": False},
        )
        assert f.offer_flip is True
        assert f.flip_value is True
        assert f.evidence["value"] is False


# ---------------------------------------------------------------------------
# Grep-audit: the engine module imports NOTHING voice-specific
# ---------------------------------------------------------------------------


class TestGrepAudit:
    """The engine module must not import or reference anything voice-loop-specific.

    This is the acceptance test from the brief: a grep-audit proves the
    engine module is ready for plugin #2 without a rewrite.
    """

    def test_no_voice_specific_imports(self) -> None:
        """Grep the engine source for voice-loop-specific strings."""
        import sill_core.diagnosis as mod

        source_path = mod.__file__
        assert source_path is not None

        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()

        # Also check the AST for import statements that mention voice-loop.
        tree = ast.parse(source)
        voice_refs: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if node.module and "voice" in node.module.lower():
                    voice_refs.append(f"import: {node.module}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lower = node.value.lower()
                if "voice-loop" in lower or "voice_loop" in lower:
                    voice_refs.append(f"string: {node.value!r}")

        assert voice_refs == [], (
            f"diagnosis.py contains voice-loop-specific references: {voice_refs}"
        )

    def test_no_voice_specific_in_source_text(self) -> None:
        """A plain text grep for the string 'voice' (case-insensitive) in the
        engine source — it should appear only in this test file, never in the
        engine itself."""
        import sill_core.diagnosis as mod

        source_path = mod.__file__
        assert source_path is not None

        with open(source_path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                lowered = line.lower()
                # The word "voice" should not appear anywhere in the engine source.
                assert "voice" not in lowered, (
                    f"diagnosis.py:{lineno} contains 'voice': {line.strip()!r}"
                )
