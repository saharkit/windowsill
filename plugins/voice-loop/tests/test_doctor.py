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

import doctor


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


# ---------------------------------------------------------------------------
# _sill_core_root() path resolution
# ---------------------------------------------------------------------------


class TestSillCoreRootResolution:
    """_sill_core_root() resolves the correct path in checkout and cache layouts."""

    def test_checkout_layout_sibling_resolution(self) -> None:
        """Checkout: voice-loop/ and sill-core/ side by side → finds sill-core/.

        Gap: the old code only worked in the checkout layout, and this test
        pins that it still does after the refactor that also handles the
        cache layout.
        """
        result = doctor._sill_core_root()
        # In the checkout, sill-core is at plugins/sill-core/ (no version dir).
        assert result.is_dir()
        assert result.name == "sill-core"
        # It must contain the sill_core Python package.
        assert (result / "sill_core" / "__init__.py").exists()

    def test_cache_layout_with_version_resolution(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Cache: voice-loop/0.7.0/ and sill-core/0.1.0/ → finds sill-core/0.1.0/.

        Gap: the old code returned voice-loop/sill-core (one level too deep);
        this pins that the refactored resolver walks up to the marketplace
        level and then into the version directory, which is the only code
        path that would have caught the incident without a symlink.
        """
        # Build a cache-like layout.
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)
        sill_version = tmp_path / "sill-core" / "0.1.0" / "sill_core"
        sill_version.mkdir(parents=True)
        (sill_version / "__init__.py").write_text("# sill_core")

        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        result = doctor._sill_core_root()
        assert result == tmp_path / "sill-core" / "0.1.0"

    def test_cache_picks_highest_version(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """When multiple versions exist, the highest one wins.

        Gap: without this, a stale copy could shadow a newer install —
        a silent regression the old code could not produce because it
        never resolved version directories at all.
        """
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)

        for ver in ("0.1.0", "0.2.0", "0.1.5"):
            pkg = tmp_path / "sill-core" / ver / "sill_core"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text(f"# v{ver}")

        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        result = doctor._sill_core_root()
        assert result == tmp_path / "sill-core" / "0.2.0"

    def test_raises_when_sill_core_absent(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """FileNotFoundError when sill-core is nowhere to be found.

        Gap: the error surface — a raised exception is the signal main()
        catches to emit the diagnosis; if this function returned a wrong
        path instead of raising, the diagnosis would never fire and the
        doctor would crash on the import as before.
        """
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)

        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        with pytest.raises(FileNotFoundError):
            doctor._sill_core_root()

    def test_cache_sill_core_at_marketplace_level_no_version_dir(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Cache layout WITHOUT version directories → the sill-core dir itself is used.

        Gap: a cache where sill-core was unpacked flat (no version subdir)
        should still resolve; the version-dir resolution is a refinement,
        not a requirement.
        """
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)
        sill_dir = tmp_path / "sill-core" / "sill_core"
        sill_dir.mkdir(parents=True)
        (sill_dir / "__init__.py").write_text("# sill_core")

        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        result = doctor._sill_core_root()
        assert result == tmp_path / "sill-core"


# ---------------------------------------------------------------------------
# _parse_version / _is_version_like
# ---------------------------------------------------------------------------


class TestVersionParsing:
    """_parse_version and _is_version_like drive the version-dir selection."""

    def test_parse_valid_semver(self) -> None:
        assert doctor._parse_version("0.1.0") == (0, 1, 0)
        assert doctor._parse_version("1.2.3") == (1, 2, 3)

    def test_parse_two_part_version(self) -> None:
        assert doctor._parse_version("0.1") == (0, 1)

    def test_parse_higher_wins(self) -> None:
        assert doctor._parse_version("0.2.0") > doctor._parse_version("0.1.0")
        assert doctor._parse_version("1.0.0") > doctor._parse_version("0.9.9")
        assert doctor._parse_version("0.1.10") > doctor._parse_version("0.1.9")

    def test_parse_garbage_returns_empty(self) -> None:
        assert doctor._parse_version("not-a-version") == ()
        assert doctor._parse_version("") == ()

    def test_is_version_like_valid(self) -> None:
        assert doctor._is_version_like("0.1.0") is True
        assert doctor._is_version_like("1.2") is True

    def test_is_version_like_rejects_single_number(self) -> None:
        """A lone number is not a version directory — needs at least two parts."""
        assert doctor._is_version_like("1") is False
        assert doctor._is_version_like("0") is False

    def test_is_version_like_rejects_qualifiers(self) -> None:
        assert doctor._is_version_like("0.1.0rc1") is False
        assert doctor._is_version_like("v0.1.0") is False


# ---------------------------------------------------------------------------
# Incident symlink cleanup
# ---------------------------------------------------------------------------


class TestIncidentSymlinkCleanup:
    """_remove_incident_symlink() deletes the 2026-08-10 workaround symlink."""

    def test_removes_symlink_at_wrong_path(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The symlink at voice-loop/sill-core (the old wrong path) is deleted.

        Gap: without cleanup, a /plugin update removes the symlink silently
        and the old bug resurfaces; the cleanup is part of the fix so the
        old wrong path is never followed again, even by a stale caller.
        """
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)

        # Create the symlink at the old wrong location.
        wrong_path = tmp_path / "voice-loop" / "sill-core"
        wrong_path.symlink_to(tmp_path / "some-target")

        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        doctor._remove_incident_symlink()
        assert not wrong_path.exists()

    def test_noop_when_no_symlink(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Does not raise when there is nothing to remove.

        Gap: cleanup must be safe to call on a fresh install — a crash here
        would defeat the entire fix.
        """
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)

        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        # Must not raise.
        doctor._remove_incident_symlink()

    def test_noop_when_path_is_directory_not_symlink(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Only symlinks are deleted — a real directory at the wrong path is left alone.

        Gap: a user who manually created a real sill-core directory at the
        old wrong path should not lose data; the cleanup is scoped to the
        incident symlink only.
        """
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)

        real_dir = tmp_path / "voice-loop" / "sill-core"
        real_dir.mkdir()

        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        doctor._remove_incident_symlink()
        assert real_dir.is_dir()


# ---------------------------------------------------------------------------
# main() — missing engine diagnosis
# ---------------------------------------------------------------------------


class TestDoctorMissingEngineDiagnosis:
    """When sill-core is absent, main() emits a diagnosis instead of crashing."""

    def test_missing_engine_emits_diagnosis_json(
        self, monkeypatch, capsys
    ) -> None:
        """The JSON output carries a sill_core_missing finding, not a traceback.

        Gap: the incident showed that a missing sill-core crashed the doctor
        with ModuleNotFoundError — the diagnostic tool could not report its
        own broken state.  This test pins that it now emits structured JSON
        with rc=0 instead, which the skill can present to the operator.
        """
        # Prevent side effects from reaching the host filesystem.
        monkeypatch.setattr(doctor, "read_config", lambda path: {})
        monkeypatch.setattr(
            doctor, "read_ledger",
            lambda state_home: {"state": "none", "completed_steps": []},
        )
        monkeypatch.setattr(
            doctor, "read_logs", lambda state_home, tail_lines=60: {},
        )
        monkeypatch.setattr(doctor, "load_manifest", lambda root: [])
        monkeypatch.setattr(
            doctor, "_sill_core_root",
            lambda: (_ for _ in ()).throw(
                FileNotFoundError("sill-core not found")
            ),
        )

        rc = doctor.main([])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert rc == 0
        findings = output["findings"]
        assert len(findings) == 1
        assert findings[0]["key"] == "sill_core_missing"
        assert findings[0]["bin"] == "real_anomaly"
        assert "install sill-core" in findings[0]["fix"].lower()

    def test_import_failed_emits_diagnosis_json(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        """When sill-core is on disk but won't import, a distinct diagnosis fires.

        Gap: FileNotFoundError covers the "not there" case; ImportError covers
        the "found but broken" case — a damaged plugin is a different diagnosis
        than an absent one, and the operator action differs (reinstall vs install).
        """
        # Build a fake sill-core that exists but has no importable diagnosis module.
        sill_root = tmp_path / "sill-core"
        sill_root.mkdir()
        (sill_root / "sill_core").mkdir()
        (sill_root / "sill_core" / "__init__.py").write_text("")
        # No diagnosis.py — importing sill_core.diagnosis will raise ImportError.

        # Evict the real sill-core from sys.modules so the fake one is the only
        # candidate — otherwise the cached import from this test module's own
        # `from sill_core.diagnosis import ...` satisfies the import silently.
        for key in list(sys.modules):
            if key == "sill_core" or key.startswith("sill_core."):
                monkeypatch.delitem(sys.modules, key)

        monkeypatch.setattr(doctor, "read_config", lambda path: {})
        monkeypatch.setattr(
            doctor, "read_ledger",
            lambda state_home: {"state": "none", "completed_steps": []},
        )
        monkeypatch.setattr(
            doctor, "read_logs", lambda state_home, tail_lines=60: {},
        )
        monkeypatch.setattr(doctor, "load_manifest", lambda root: [])
        monkeypatch.setattr(doctor, "_sill_core_root", lambda: sill_root)

        rc = doctor.main([])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert rc == 0
        findings = output["findings"]
        assert len(findings) == 1
        assert findings[0]["key"] == "sill_core_import_failed"
        assert findings[0]["bin"] == "real_anomaly"
        assert "reinstall sill-core" in findings[0]["fix"].lower()
