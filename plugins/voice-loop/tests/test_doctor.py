"""Tests for the /doctor skill — voice-loop-specific scenario tests.

Exercises the voice-loop check manifest against configs, ledgers, and log
tails shaped like real voice-loop data.  The engine itself is tested in
sill-core; these tests verify that the MANIFEST's checks fire for the right
voice-loop failure modes.

Acceptance criteria from the brief:
- each bin has a scripted scenario test (a chosen-off feature, a truncated
  install ledger, an injected log anomaly)
- /doctor names the right bin with the right explanation
- a grep-audit test proves the engine module imports nothing voice-specific
  (in sill-core's test; echoed here for the manifest import)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# The engine is in sill-core; add it to the path.
_plugin_root = Path(__file__).resolve().parent.parent
_core_root = _plugin_root.parent / "sill-core"
if str(_core_root) not in sys.path:
    sys.path.insert(0, str(_core_root))

from sill_core.diagnosis import Bin, Finding, diagnose


# ---------------------------------------------------------------------------
# Load the manifest
# ---------------------------------------------------------------------------


def _load_manifest() -> list[dict[str, Any]]:
    import importlib.util

    manifest_path = (
        _plugin_root / "skills" / "doctor" / "check_manifest.py"
    )
    spec = importlib.util.spec_from_file_location(
        "check_manifest", str(manifest_path)
    )
    assert spec is not None, f"manifest not found at {manifest_path}"
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.CHECK_MANIFEST


MANIFEST = _load_manifest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_by_key(findings: list[Finding], key: str) -> Finding | None:
    for f in findings:
        if f.key == key:
            return f
    return None


# ---------------------------------------------------------------------------
# Manifest integrity checks
# ---------------------------------------------------------------------------


class TestManifestIntegrity:
    """The manifest itself is well-formed — every check has required fields."""

    def test_every_check_has_bin(self) -> None:
        for item in MANIFEST:
            assert "bin" in item, f"missing bin in {item.get('key', '?')}"
            assert item["bin"] in [b.value for b in Bin], (
                f"unknown bin {item['bin']!r} in {item.get('key', '?')}"
            )

    def test_every_check_has_key_and_title(self) -> None:
        for item in MANIFEST:
            assert item.get("key"), f"missing key in {item}"
            assert item.get("title"), f"missing title in {item.get('key', '?')}"

    def test_every_check_has_explanation(self) -> None:
        for item in MANIFEST:
            assert item.get("explanation"), (
                f"missing explanation in {item.get('key', '?')}"
            )

    def test_every_check_has_check_type(self) -> None:
        valid_types = {"config", "ledger", "log"}
        for item in MANIFEST:
            check_def = item.get("check", {})
            assert check_def.get("type") in valid_types, (
                f"bad check type in {item.get('key', '?')}: {check_def.get('type')}"
            )

    def test_no_duplicate_keys(self) -> None:
        keys = [item["key"] for item in MANIFEST]
        seen: set[str] = set()
        for key in keys:
            assert key not in seen, f"duplicate key {key!r} in manifest"
            seen.add(key)

    def test_at_least_one_check_per_bin(self) -> None:
        bins = {item["bin"] for item in MANIFEST}
        expected = {b.value for b in Bin}
        assert bins == expected, f"manifest covers bins {bins}, expected {expected}"


# ---------------------------------------------------------------------------
# Scenario 1 — consequence of your choice (chosen-off feature)
# ---------------------------------------------------------------------------


class TestScenarioConsequenceOfChoice:
    """A config file with a feature deliberately off → /doctor finds it."""

    def test_auto_paste_off_is_consequence_of_choice(self) -> None:
        config = {
            "dictate": {"auto_paste": False},
            "speak": {"enabled": True},
        }
        findings = diagnose(MANIFEST, config=config)

        f = _find_by_key(findings, "auto_paste_off")
        assert f is not None
        assert f.bin == Bin.CONSEQUENCE_OF_CHOICE
        assert "manual paste" in f.explanation.lower()
        assert f.offer_flip is True
        assert f.flip_path == "dictate.auto_paste"
        assert f.flip_value is True

    def test_speak_disabled_is_consequence_of_choice(self) -> None:
        config = {
            "dictate": {"auto_paste": True},
            "speak": {"enabled": False},
        }
        findings = diagnose(MANIFEST, config=config)

        f = _find_by_key(findings, "speak_disabled")
        assert f is not None
        assert f.bin == Bin.CONSEQUENCE_OF_CHOICE
        assert "speak.enabled" in f.explanation.lower()
        assert f.offer_flip is True

    def test_custom_stt_command_is_consequence_of_choice(self) -> None:
        config = {
            "stt": {"command": "/usr/local/bin/mystt"},
        }
        findings = diagnose(MANIFEST, config=config)

        f = _find_by_key(findings, "stt_command_set")
        assert f is not None
        assert f.bin == Bin.CONSEQUENCE_OF_CHOICE
        assert "stt.command" in f.explanation.lower()

    def test_config_with_everything_on_produces_no_choice_findings(self) -> None:
        config = {
            "dictate": {"auto_paste": True},
            "speak": {"enabled": True},
        }
        findings = diagnose(MANIFEST, config=config)

        choice_findings = [
            f for f in findings if f.bin == Bin.CONSEQUENCE_OF_CHOICE
        ]
        assert choice_findings == []


# ---------------------------------------------------------------------------
# Scenario 2 — unfinished install (truncated install ledger)
# ---------------------------------------------------------------------------


class TestScenarioUnfinishedInstall:
    """A truncated install ledger → /doctor finds it."""

    def test_no_ledger_at_all(self) -> None:
        ledger = {"state": "none", "completed_steps": []}
        findings = diagnose(MANIFEST, ledger=ledger)

        f = _find_by_key(findings, "install_incomplete_or_not_started")
        assert f is not None
        assert f.bin == Bin.UNFINISHED_INSTALL
        assert "install step ledger" in f.explanation.lower()

    def test_partial_install(self) -> None:
        ledger = {
            "state": "in_progress",
            "completed_steps": [
                "step-0-probe",
                "step-1-language",
                "step-2-backends",
            ],
            "current_step": "step-3-install-deps",
        }
        findings = diagnose(MANIFEST, ledger=ledger)

        # The specific deps check fires (step 3 is not done)
        f_deps = _find_by_key(findings, "install_deps_incomplete")
        assert f_deps is not None
        assert f_deps.bin == Bin.UNFINISHED_INSTALL

        # The config check also fires (step 4 is not done)
        f_config = _find_by_key(findings, "config_not_written")
        assert f_config is not None

        # The "all steps" check does NOT fire — some steps ARE done
        f_all = _find_by_key(findings, "install_incomplete_or_not_started")
        assert f_all is None

    def test_complete_install_produces_no_ledger_findings(self) -> None:
        all_steps = [
            "step-0-probe",
            "step-1-language",
            "step-2-backends",
            "step-3-install-deps",
            "step-4-write-config",
            "step-5-paste-behaviour",
            "step-6-hotkey",
            "step-7-speak-convention",
            "step-8-verify",
        ]
        ledger = {
            "state": "complete",
            "completed_steps": all_steps,
        }
        findings = diagnose(MANIFEST, ledger=ledger)

        ledger_findings = [
            f for f in findings if f.bin == Bin.UNFINISHED_INSTALL
        ]
        assert ledger_findings == []


# ---------------------------------------------------------------------------
# Scenario 3 — real anomaly (injected log anomaly)
# ---------------------------------------------------------------------------


class TestScenarioRealAnomaly:
    """An injected log anomaly → /doctor finds it."""

    def test_server_unreachable_in_log(self) -> None:
        logs = {
            "speak.log": [
                "2026-08-05T12:00:00 synthesis unreachable: Connection refused to 127.0.0.1:8355",
            ],
            "dictate.log": [],
        }
        findings = diagnose(MANIFEST, logs=logs)

        f = _find_by_key(findings, "server_unreachable")
        assert f is not None, f"findings: {[x.key for x in findings]}"
        assert f.bin == Bin.REAL_ANOMALY
        assert f.evidence["match_count"] >= 1

    def test_player_failed_in_log(self) -> None:
        logs = {
            "speak.log": [
                "2026-08-05T12:00:01 player failed: No such file or directory: 'aplay'",
            ],
            "dictate.log": [],
        }
        findings = diagnose(MANIFEST, logs=logs)

        f = _find_by_key(findings, "player_failed")
        assert f is not None, f"findings: {[x.key for x in findings]}"
        assert f.bin == Bin.REAL_ANOMALY

    def test_recorder_failed_in_log(self) -> None:
        logs = {
            "speak.log": [],
            "dictate.log": [
                "2026-08-05T12:00:02 recorder failed: pw-record: command not found",
            ],
        }
        findings = diagnose(MANIFEST, logs=logs)

        f = _find_by_key(findings, "recorder_failed")
        assert f is not None, f"findings: {[x.key for x in findings]}"
        assert f.bin == Bin.REAL_ANOMALY

    def test_stt_unreachable_in_log(self) -> None:
        logs = {
            "speak.log": [],
            "dictate.log": [
                "2026-08-05T12:00:03 stt unreachable: HTTP 503",
            ],
        }
        findings = diagnose(MANIFEST, logs=logs)

        f = _find_by_key(findings, "stt_unreachable")
        assert f is not None, f"findings: {[x.key for x in findings]}"
        assert f.bin == Bin.REAL_ANOMALY

    def test_no_recorder_in_log(self) -> None:
        logs = {
            "speak.log": [],
            "dictate.log": [
                "2026-08-05T12:00:04 no recorder available",
            ],
        }
        findings = diagnose(MANIFEST, logs=logs)

        f = _find_by_key(findings, "no_recorder_available")
        assert f is not None, f"findings: {[x.key for x in findings]}"
        assert f.bin == Bin.REAL_ANOMALY

    def test_clean_logs_produce_no_anomaly_findings(self) -> None:
        logs = {
            "speak.log": [
                "2026-08-05T12:00:00 played rc=0",
                "2026-08-05T12:00:01 timings extract_ms=45",
            ],
            "dictate.log": [
                "2026-08-05T12:00:02 recording via pw-record",
                "2026-08-05T12:00:03 transcript: <redacted 30 chars>",
            ],
        }
        findings = diagnose(MANIFEST, logs=logs)

        anomaly_findings = [
            f for f in findings if f.bin == Bin.REAL_ANOMALY
        ]
        # The "many_unclassified" check looks for "unexpected error" which
        # is not in these clean logs
        assert anomaly_findings == []


# ---------------------------------------------------------------------------
# Full three-bin scenario — all three at once
# ---------------------------------------------------------------------------


class TestFullThreeBinScenario:
    """A machine with auto_paste off, a truncated install, and log errors."""

    def test_all_three_bins_fire(self) -> None:
        config = {
            "dictate": {"auto_paste": False},
            "speak": {"enabled": True},
        }
        ledger = {
            "state": "in_progress",
            "completed_steps": ["step-0-probe", "step-1-language"],
        }
        logs = {
            "speak.log": [
                "2026-08-05T12:00:00 synthesis unreachable: Connection refused",
            ],
            "dictate.log": [],
        }

        findings = diagnose(MANIFEST, config=config, ledger=ledger, logs=logs)

        bins = {f.bin for f in findings}
        assert Bin.CONSEQUENCE_OF_CHOICE in bins
        assert Bin.UNFINISHED_INSTALL in bins
        assert Bin.REAL_ANOMALY in bins

        # Findings are sorted: choice first, then install, then anomaly
        bin_order = [f.bin for f in findings]
        # All choice before install before anomaly
        for i in range(len(bin_order) - 1):
            this = bin_order[i]
            nxt = bin_order[i + 1]
            bin_rank = {b: i for i, b in enumerate(Bin)}
            assert bin_rank[this] <= bin_rank[nxt], (
                f"findings out of order: {bin_order}"
            )


# ---------------------------------------------------------------------------
# Grep-audit — the manifest imports nothing voice-loop-specific from the engine
# ---------------------------------------------------------------------------


class TestGrepAudit:
    """The engine module imports nothing voice-specific (sill-core test has
    the primary grep audit; this one verifies the manifest import path is clean)."""

    def test_manifest_does_not_import_engine_directly(self) -> None:
        """The manifest is data-only — it never imports the engine."""
        manifest_path = (
            _plugin_root / "skills" / "doctor" / "check_manifest.py"
        )
        source = manifest_path.read_text(encoding="utf-8")

        # The manifest must not import from sill_core.diagnosis
        assert "from sill_core" not in source, (
            "manifest imports sill-core — it should be data-only"
        )
        assert "import sill_core" not in source, (
            "manifest imports sill-core — it should be data-only"
        )

    def test_manifest_is_pure_data(self) -> None:
        """The manifest file is a list literal plus imports — no logic."""
        import ast

        manifest_path = (
            _plugin_root / "skills" / "doctor" / "check_manifest.py"
        )
        source = manifest_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # It may import `annotations` from __future__, and that's it.
        imports = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        for imp in imports:
            module = (
                imp.module
                if isinstance(imp, ast.ImportFrom)
                else imp.names[0].name
            )
            # Only __future__ imports are allowed
            assert module == "__future__", (
                f"manifest imports {module!r} — should be data-only"
            )
