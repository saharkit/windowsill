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

import builtins
import json
import os
import runpy
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
        # D3: the previous fixture invented a line speak.py never writes
        # ("player failed: No such file or directory: 'aplay'").  The
        # real failing-playback line is "played rc=1" — the player spawned
        # but exited non-zero.  The old assertion tested the OSError-only
        # path and missed the one the user actually hits.
        logs = {
            "speak.log": [
                "2026-08-05T12:00:01 played rc=1 bytes=232844 chunks=1 via=stream",
            ],
            "dictate.log": [],
        }
        findings = diagnose(MANIFEST, logs=logs)

        f = _find_by_key(findings, "player_nonzero_exit")
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
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        """The JSON output carries a sill_core_missing finding, not a traceback.

        Gap: the incident showed that a missing sill-core crashed the doctor
        with ModuleNotFoundError — the diagnostic tool could not report its
        own broken state.  This test pins that it now emits structured JSON
        with rc=1 instead, which the skill can present to the operator.
        """
        # Prevent side effects from reaching the host filesystem.
        real_read_ledger = doctor.read_ledger
        real_read_logs = doctor.read_logs
        real_load_manifest = doctor.load_manifest
        monkeypatch.setattr(doctor, "read_config", lambda path: {})
        monkeypatch.setattr(
            doctor, "read_ledger",
            lambda state_home: {"state": "none", "completed_steps": []},
        )
        monkeypatch.setattr(
            doctor, "read_logs", lambda state_home, tail_lines=60: {},
        )
        monkeypatch.setattr(doctor, "load_manifest", lambda root: [])
        # An empty config and ledger are also the WSL-boundary precondition, and on a Windows
        # host with a registered distro that finding appends a second row — environment noise,
        # not part of what this test decides.
        monkeypatch.setattr(
            doctor, "_wsl_boundary_finding",
            lambda **kwargs: None,
        )
        monkeypatch.setattr(doctor, "_default_state_home", lambda: str(tmp_path / "state"))
        monkeypatch.setattr(doctor, "_default_config_path", lambda: str(tmp_path / "config.json"))
        monkeypatch.setattr(
            doctor, "_sill_core_root",
            lambda: (_ for _ in ()).throw(
                FileNotFoundError("sill-core not found")
            ),
        )

        rc = doctor.main([])
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert rc == 1
        findings = output["findings"]
        finding = next(item for item in findings if item["key"] == "sill_core_missing")
        assert finding["bin"] == "real_anomaly"
        assert "The diagnosis engine" in finding["title"]
        assert "install sill-core" in finding["fix"].lower()

        # Exercise the reader verdicts and neutral argument branches in the same diagnosis setup.
        state_home = tmp_path / "state"
        ledger_path = state_home / "voice-loop" / "install.ledger"
        ledger_path.parent.mkdir(parents=True)
        monkeypatch.setattr(doctor, "read_ledger", real_read_ledger)
        monkeypatch.setattr(doctor, "read_logs", real_read_logs)
        monkeypatch.setattr(doctor, "load_manifest", real_load_manifest)
        assert doctor.read_ledger(str(state_home)).status == "missing"
        ledger_path.write_text("[]", encoding="utf-8")
        assert doctor.read_ledger(str(state_home)).status == "malformed"
        ledger_path.write_text(json.dumps({"steps": []}), encoding="utf-8")
        assert doctor.read_ledger(str(state_home)).status == "malformed"
        ledger_path.write_text(json.dumps({"steps": {"probe": []}}), encoding="utf-8")
        assert doctor.read_ledger(str(state_home)).status == "malformed"
        ledger_path.write_text(json.dumps({"state": "in_progress", "steps": {
            "done": {"status": "complete"}, "now": {"status": "in_progress"},
        }}), encoding="utf-8")
        parsed = doctor.read_ledger(str(state_home))
        assert parsed["completed_steps"] == ["done"]
        assert parsed["current_step"] == "now"
        assert doctor.read_logs(str(tmp_path / "no-logs")).status == "degraded"
        assert doctor.load_manifest(_plugin_root)

        assert doctor.main(["doctor", "--help"]) == 0
        assert doctor.main(["doctor", "--unknown"]) == 2

        # With a real engine and a Windows-boundary finding, the unfinished-install finding is
        # deliberately filtered and the structured output carries every field used by the skill.
        ledger_path.write_text(json.dumps({"state": "none", "steps": {}}), encoding="utf-8")
        monkeypatch.setattr(doctor, "_sill_core_root", lambda: _core_root)
        monkeypatch.setattr(doctor, "_wsl_boundary_finding", lambda **kwargs: {
            "bin": "consequence_of_choice", "key": "wsl", "title": "wsl",
            "explanation": "wsl", "fix": "wsl", "offer_flip": False,
            "flip_path": "", "flip_value": None, "evidence": {},
        })
        rc = doctor.main(["doctor", "--state-home", str(state_home), "--config-path", str(tmp_path / "config.json")])
        rendered = json.loads(capsys.readouterr().out)
        assert rc == 0
        keys = [finding["key"] for finding in rendered["findings"]]
        # What this asserts is the FILTER, not the exact roster: with a real engine and a
        # Windows-boundary finding, unfinished_install is dropped and wsl survives. A host may
        # legitimately contribute its own findings -- a Windows box resolves `python3` to the
        # Store alias and correctly reports python3_is_store_stub -- so comparing the whole list
        # couples this test to the machine it runs on, which is what made it red on the one
        # platform this work is about.
        assert "unfinished_install" not in keys
        assert "wsl" in keys
        assert rendered["ledger_status"] == "ok"

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
        # Same as the missing-engine test above: the WSL-boundary finding is environment noise
        # here, and on a Windows host with a registered distro it would add a second row.
        monkeypatch.setattr(
            doctor, "_wsl_boundary_finding",
            lambda **kwargs: None,
        )
        monkeypatch.setattr(doctor, "_sill_core_root", lambda: sill_root)

        rc = doctor.main([])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert rc == 1
        findings = output["findings"]
        finding = next(item for item in findings if item["key"] == "sill_core_import_failed")
        assert finding["bin"] == "real_anomaly"
        assert "The diagnosis engine" in finding["title"]
        assert "reinstall sill-core" in finding["fix"].lower()


# ---------------------------------------------------------------------------
# Windows prerequisites — /doctor names the three Windows traps by name
# ---------------------------------------------------------------------------


class TestWindowsPrerequisites:
    """The doctor names all three Windows prerequisites specifically.

    Ticket #174 was lost because a presence test for ``python3`` on PATH
    answered YES on the Microsoft Store stub — a real ``.exe`` that
    silently redirects to the Store instead of running Python.  A doctor
    that only reported absent interpreters would pass on the exact trap
    that hid the bug.  These tests pin the DECISION each check makes
    (real interpreter vs. Store stub vs. absent), not the wording of
    its message.

    The probe seams follow the same shape the tree already uses for
    ``_sill_core_root`` — keyword-only parameters with production
    defaults, so the test injects a fixed platform and a fake ``which``
    without monkeypatching the module.
    """

    def test_windows_python_check_decision(self, monkeypatch) -> None:
        """Each decision produces a distinct, expected key.

        The check has two failure modes and two pass-throughs:

        - Real ``python3`` (not under ``WindowsApps``) → no finding.
        - ``python3`` resolves to the Store stub → fires
          ``python3_is_store_stub``.
        - No real interpreter on PATH (neither ``python3`` nor
          ``python`` is real) → fires ``python_interpreter_missing``.
        - Non-Windows platform → no finding (inert).

        A check that only reported absences would answer the same
        whether the user has the Store stub or a real interpreter:
        both report "python3 is somehow on PATH".  The path check
        distinguishes them — that is the load-bearing assertion.
        """

        def _which_with(python3: str | None, py: str | None):
            """Build a fake ``which`` that answers the two lookups."""
            def fake(name: str) -> str | None:
                if name == "python3":
                    return python3
                if name == "python":
                    return py
                return None
            return fake

        # Decision 1: real python3 — no finding. ``where`` is injected because leaving it at
        # its default runs ``where.exe python3`` against the HOST's PATH: on a Windows runner
        # that answers the WindowsApps Store stub too, and the stub-anywhere rule (correctly)
        # fires a finding this decision was not exercising.
        result = doctor._check_python_interpreter(
            platform="win32",
            which=_which_with(
                python3="C:\\Python311\\python3.exe",
                py=None,
            ),
            where=lambda: ["C:\\Python311\\python3.exe"],
        )
        assert result is None, (
            f"real python3 must not fire a finding: got {result}"
        )

        # Decision 2: python3 is the Store stub — fires
        # ``python3_is_store_stub``.  This is the load-bearing check
        # from ticket #174: a presence test answers YES on this trap.
        stub = (
            "C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\"
            "python3.exe"
        )
        result = doctor._check_python_interpreter(
            platform="win32",
            which=_which_with(python3=stub, py=None),
        )
        assert result is not None, "Store stub must fire a finding"
        assert result["key"] == "python3_is_store_stub", (
            f"wrong key for Store stub: {result.get('key')!r}"
        )
        assert result["bin"] == "real_anomaly"

        # Decision 3: no python3 AND no python on PATH — fires
        # ``python_interpreter_missing``.
        result = doctor._check_python_interpreter(
            platform="win32",
            which=_which_with(python3=None, py=None),
        )
        assert result is not None, (
            "absent interpreter must fire a finding"
        )
        assert result["key"] == "python_interpreter_missing", (
            f"wrong key for absent interpreter: {result.get('key')!r}"
        )
        assert result["bin"] == "real_anomaly"

        # Decision 4: non-Windows is inert.  The launchers call
        # ``python3``; on Linux/macOS the Store stub does not exist,
        # so the check returns None and /doctor output is unchanged.
        for platform_name in ("linux", "darwin"):
            result = doctor._check_python_interpreter(
                platform=platform_name,
                which=_which_with(python3=None, py=None),
            )
            assert result is None, (
                f"platform {platform_name!r} must be inert: got {result}"
            )

        # Sanity: the two failure-mode keys are distinct, not a single
        # "FAIL" — the brief requires each prerequisite be named.
        decision_stub = doctor._check_python_interpreter(
            platform="win32",
            which=_which_with(python3=stub, py=None),
        )
        decision_missing = doctor._check_python_interpreter(
            platform="win32",
            which=_which_with(python3=None, py=None),
        )
        assert decision_stub is not None
        assert decision_missing is not None
        assert decision_stub["key"] != decision_missing["key"]

        # Exercise the platform-neutral seams with injected Windows-tool results; these are
        # reachable on the Ubuntu measuring runner without pretending to run Windows itself.
        assert doctor._is_store_stub(None) is False
        assert doctor._decode_windows_output("already text") == "already text"
        assert doctor._decode_windows_output(b"") == ""
        assert doctor._decode_windows_output(b"plain") == "plain"
        assert doctor._decode_windows_output(b"\xff") == "�"
        assert doctor._decode_windows_output(42) == "42"
        assert doctor._where_python3(platform="linux") == []
        class FailedWhere:
            returncode = 1
            stdout = "unused"
        assert doctor._where_python3(platform="win32", run=lambda *args, **kwargs: FailedWhere()) == []
        class SuccessfulWhere:
            returncode = 0
            stdout = "C:\\Python311\\python3.exe\n"
        assert doctor._where_python3(platform="win32", run=lambda *args, **kwargs: SuccessfulWhere()) == [
            "C:\\Python311\\python3.exe"
        ]
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)
        assert doctor._redact_windows_path("C:\\temp\\python.exe") == "C:\\temp\\python.exe"
        assert doctor._redact_windows_path("C:\\Users\\alice\\python.exe") == "C:\\Users\\<user>\\python.exe"
        assert doctor._wsl_boundary_finding(platform="linux") is None
        assert doctor._wsl_boundary_finding(
            platform="win32", distros_probe=lambda: ([], 0)
        ) is None
        finding = doctor._wsl_boundary_finding(
            platform="win32", distros_probe=lambda: (["Ubuntu"], 1)
        )
        assert finding is not None
        assert finding["evidence"]["wsl_distro_count"] == 1

    def test_python3_alias_missing_fires_and_reaches_output(
        self, monkeypatch, capsys
    ) -> None:
        """Real python.org install, no ``python3`` anywhere → the finding fires.

        Gap (#178, prerequisite 2): python.org ships ``python.exe`` and no
        ``python3.exe``, while every launcher calls ``python3`` and guards
        with ``command -v python3`` → exit 0.  The user sees a plugin that
        does NOTHING, with no error — and before this check, /doctor saw a
        healthy install and stayed silent too, because ``python`` resolves
        to a real interpreter and no Store stub is on PATH.  This pins both
        the decision and its merge into the final JSON output.
        """
        real_check = doctor._check_python_interpreter

        fake_account = "FakeAccountForAliasTest"
        fake_real_python = (
            f"C:\\Users\\{fake_account}\\AppData\\Local\\Programs\\Python\\"
            "Python312\\python.exe"
        )

        def patched_check():
            return real_check(
                platform="win32",
                which=lambda name: fake_real_python if name == "python" else None,
                where=lambda: [],
            )

        monkeypatch.setattr(doctor, "_check_python_interpreter", patched_check)
        monkeypatch.setattr(doctor, "read_config", lambda path: {})
        monkeypatch.setattr(
            doctor,
            "read_ledger",
            lambda state_home: {"state": "none", "completed_steps": []},
        )
        monkeypatch.setattr(
            doctor, "read_logs", lambda state_home, tail_lines=60: {}
        )
        monkeypatch.setattr(doctor, "load_manifest", lambda root: [])
        monkeypatch.setattr(
            doctor,
            "_sill_core_root",
            lambda: (_ for _ in ()).throw(
                FileNotFoundError("sill-core not found")
            ),
        )

        rc = doctor.main([])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert rc == 1
        keys = [f["key"] for f in output["findings"]]
        assert "python3_alias_missing" in keys, (
            f"python3 alias finding missing from output: {keys}"
        )

        finding = next(
            f for f in output["findings"] if f["key"] == "python3_alias_missing"
        )
        assert finding["bin"] == "real_anomaly"
        # The finding must say what to DO: name the alias fix concretely.
        assert "python3.exe" in finding["fix"]
        assert "where.exe python3" in finding["fix"]
        # And the redacted real-interpreter path in the explanation must
        # not carry the account name (same identity rule as the stub
        # finding's evidence — /doctor stdout feeds /report-bug).
        blob = json.dumps(finding)
        assert fake_account not in blob, (
            f"account name leaked into finding: {blob}"
        )

        # The control: a real ``python3`` present must NOT fire it.
        clean = real_check(
            platform="win32",
            which=lambda name: (
                "C:\\Python311\\python3.exe" if name == "python3" else None
            ),
            where=lambda: ["C:\\Python311\\python3.exe"],
        )
        assert clean is None, f"real python3 must not fire: {clean}"

    def test_windows_findings_merged_into_main_output(
        self, monkeypatch, capsys
    ) -> None:
        """When the platform check fires, its finding flows through main().

        The brief requires ``/doctor`` to name all three prerequisites —
        not for the unit function to behave correctly in isolation, but
        for the final JSON to carry the finding the user reads.  This
        pins the integration: a diagnostic tool that drops its own
        broken state would defeat the engine.

        The platform check's evidence is reviewed here: it MUST NOT carry
        identity.  A real ``C:\\Users\\<account>\\...`` path embeds the
        OS account name, and /doctor's stdout is consumed by the
        /report-bug handoff which files a public GitHub issue — the
        redactor's home-collapse is forward-slash-only and does not save
        a Windows backslash path.  A fake account name shaped like a real
        login is planted in the input path; the assertion below would
        fail if the finding's evidence ever regressed to the raw path.
        """
        # Save the real function so the patch below can call it with
        # controlled args — the seam is keyword-only ``platform`` and
        # ``which``, so wrapping preserves production logic.
        real_check = doctor._check_python_interpreter

        # A login-shaped account name (≥3 chars, lowercase) that will
        # surface in the path the fake `which` returns below.  Distinct
        # enough that a substring search of the JSON output cannot
        # accidentally hit it from anywhere else.
        fake_account = "FakeAccountForDoctorTest"
        fake_stub_path = (
            f"C:\\Users\\{fake_account}\\AppData\\Local\\Microsoft\\"
            "WindowsApps\\python3.exe"
        )

        def patched_check():
            return real_check(
                platform="win32",
                which=lambda name: fake_stub_path if name == "python3" else None,
            )

        monkeypatch.setattr(doctor, "_check_python_interpreter", patched_check)
        # Standard monkeypatches for the sill_core_missing branch.
        monkeypatch.setattr(doctor, "read_config", lambda path: {})
        monkeypatch.setattr(
            doctor,
            "read_ledger",
            lambda state_home: {"state": "none", "completed_steps": []},
        )
        monkeypatch.setattr(
            doctor, "read_logs", lambda state_home, tail_lines=60: {}
        )
        monkeypatch.setattr(doctor, "load_manifest", lambda root: [])
        monkeypatch.setattr(
            doctor,
            "_sill_core_root",
            lambda: (_ for _ in ()).throw(
                FileNotFoundError("sill-core not found")
            ),
        )

        rc = doctor.main([])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert rc == 1
        keys = [f["key"] for f in output["findings"]]
        assert "python3_is_store_stub" in keys, (
            f"missing platform finding in output: {keys}"
        )
        assert "sill_core_missing" in keys, (
            f"missing sister finding in output: {keys}"
        )

        # The platform finding's evidence must not carry the account
        # name planted in the input path.  A regression that
        # re-introduced the raw resolved path into evidence would
        # surface the fake account here.
        platform_finding = next(
            f for f in output["findings"] if f["key"] == "python3_is_store_stub"
        )
        evidence_blob = json.dumps(platform_finding["evidence"])
        assert fake_account not in evidence_blob, (
            f"account name leaked into platform evidence: {evidence_blob}"
        )


# ---------------------------------------------------------------------------
# Round B1 — drive the remaining uncovered lines and branches up to 100%
# ---------------------------------------------------------------------------
#
# Round #156 B1 ("scripts/doctor.py to 100% — 90% today, 25 statements and 9
# partial branches left") measured the module at 90% at commit 92abf86 and
# rechecked the inputs as unchanged at a6d2292. The table of gaps is anchored
# at specific line numbers in ``scripts/doctor.py``; each method below names
# which row it closes. Where a row names one line, the test exercises the
# wider region around it: a test that crosses a branch also executes the
# statement on the side the coverage tool counts.


_SCRIPTS_DIR = _plugin_root / "scripts"
_DOCTOR_PATH = _SCRIPTS_DIR / "doctor.py"


class TestResolveVersionDirIterdirRaises:
    """Row 1 — ``_resolve_version_dir`` returns ``plugin_dir`` when ``iterdir`` raises."""

    def test_iterdir_raises_oserror_returns_plugin_dir(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The best-effort guard catches a non-iteration-safe plugin_dir and returns it.

        Gap: the catch exists so a permission error inside a cache layout cannot
        crash the doctor; without it a read-only cache directory would propagate
        PermissionError and the diagnosis tool itself would fail to start.
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        def raising_iterdir(self, *args, **kwargs):  # noqa: ARG001
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "iterdir", raising_iterdir)

        assert doctor._resolve_version_dir(plugin_dir) == plugin_dir


class TestRemoveIncidentSymlinkIsSymlinkRaises:
    """Row 2 — ``_remove_incident_symlink`` swallows an ``is_symlink`` raise."""

    def test_is_symlink_raises_oserror_returns_silently(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A flaky FS that cannot stat the wrong path is still a clean no-op.

        Gap: the cleanup is best-effort — a regression to ``raise`` would
        prevent ``/doctor`` from completing on hosts where the wrong path
        is unreadable; this pins the silent return.
        """
        voice_root = tmp_path / "voice-loop" / "0.7.0"
        voice_root.mkdir(parents=True)

        def raising_is_symlink(self, *args, **kwargs):  # noqa: ARG001
            raise OSError("stat failed")

        monkeypatch.setattr(Path, "is_symlink", raising_is_symlink)
        monkeypatch.setattr(doctor, "_plugin_root", lambda: voice_root)

        # Must not raise.
        doctor._remove_incident_symlink()


class TestIsStoreStubRealpathRaises:
    """Row 3 — ``_is_store_stub`` still recognises a Store stub when ``realpath`` raises."""

    def test_realpath_raises_oserror_still_recognises_stub(
        self, monkeypatch
    ) -> None:
        """A realpath failure must not promote the Store stub into "real".

        Gap: a host where ``os.path.realpath`` is locked down (or returns an
        error on a redirected path) would otherwise mis-classify the stub as
        a real interpreter; this pins that the un-resolved path still goes
        through the parent-name check.
        """
        monkeypatch.setattr(
            "doctor.os.path.realpath",
            lambda path: (_ for _ in ()).throw(OSError("realpath failed")),
        )
        stub = (
            "C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\"
            "python3.exe"
        )
        assert doctor._is_store_stub(stub) is True


class TestRedactWindowsPathLegacyFallbackLastButOne:
    """Row 5 — the legacy ``Users`` fallback returns the path unchanged.

    When the account segment is already the last-but-one part, the fallback
    guard (``users_index + 1 >= len(parts) - 1``) returns the path verbatim
    rather than re-emitting a stray ``<user>`` placeholder that the caller
    did not authorise.
    """

    def test_account_already_last_but_one_is_returned_unchanged(
        self, monkeypatch
    ) -> None:
        """``C:\\Users\\alice`` (the account IS the last-but-one part) is returned as-is.

        Gap: the guard prevents the fallback from overwriting an already-
        terminal segment with ``<user>``; a regression that lifted the guard
        would corrupt every short Users path the operator hands to /report-bug.
        """
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)

        # ``C:\\Users\\alice`` parses to ``("C:\\", "Users", "alice")`` —
        # users_index is 1, and 1 + 1 == len(parts) - 1 == 2, so the guard
        # fires and the path is returned unchanged.
        assert doctor._redact_windows_path(r"C:\Users\alice") == r"C:\Users\alice"


class TestPython3FindingStubOnPathButNotFirst:
    """Row 6 — a Store stub on PATH that is NOT the first ``python3`` still fires.

    When ``shutil.which`` already returns a real ``python3`` but a stub also
    sits somewhere on PATH, the finding fires the "also on PATH" branch
    (lines 464-468) so the user knows a PATH reorder could resurface the trap.
    """

    def test_stub_on_path_but_not_first_fires_also_on_path_branch(self) -> None:
        """First is real, second is the Store stub → the "also on PATH" wording fires.

        Gap: the second decision fires only when ``stub_paths`` is non-empty
        while ``first_is_stub`` is False; the mirror (first_is_stub is True)
        is already covered beside it, so this one is the asymmetry that
        confirms ``first_is_stub`` was checked.
        """

        def fake_which(name: str) -> str | None:
            if name == "python3":
                return "C:\\Python311\\python3.exe"
            if name == "python":
                return "C:\\Python311\\python.exe"
            return None

        result = doctor._check_python_interpreter(
            platform="win32",
            which=fake_which,
            where=lambda: [
                "C:\\Python311\\python3.exe",
                "C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\"
                "python3.exe",
            ],
        )
        assert result is not None
        assert result["key"] == "python3_is_store_stub"
        assert "also on PATH" in result["title"]
        assert result["evidence"]["python3_is_stub"] is False
        # The first-path redaction survives the second-decision branch too.
        assert result["evidence"]["python3_first_path"] == "C:\\Python311\\python3.exe"


class TestReadConfigValidObjectHappyPath:
    """Row 7 — ``read_config`` returns ``DiagnosticData(status='ok')`` for a JSON object."""

    def test_valid_object_round_trips_with_ok_status(
        self, tmp_path: Path
    ) -> None:
        """A well-formed JSON object file is read back as a successful DiagnosticData.

        Gap: every existing test for ``read_config`` monkeypatched it out, so
        the happy path (and every branch below it) was silently uncovered —
        a JSON parser regression could not surface in /doctor's verdict.
        """
        config_path = tmp_path / "config.json"
        payload = {"speak": {"enabled": True}, "dictate": {"auto_paste": True}}
        config_path.write_text(json.dumps(payload), encoding="utf-8")

        result = doctor.read_config(str(config_path))
        assert result.status == "ok"
        assert dict(result) == payload

    def test_malformed_json_returns_malformed_status(self, tmp_path: Path) -> None:
        """Invalid JSON returns ``status='malformed'`` with the exception name in the detail.

        Gap: the malformed-JSON branch (lines 621-622 in read_ledger, and the
        mirror at 605-606 in read_config) was uncovered — a JSON parser that
        raised the wrong exception type would silently return ``status='ok'``.
        """
        config_path = tmp_path / "config.json"
        config_path.write_text("{this is not valid json", encoding="utf-8")

        result = doctor.read_config(str(config_path))
        assert result.status == "malformed"
        assert "JSONDecodeError" in result.detail

    def test_non_object_json_returns_malformed(self, tmp_path: Path) -> None:
        """A JSON list at the top level returns ``malformed`` — ``read_config`` promises an object.

        Gap: the ``isinstance(loaded, dict)`` guard was uncovered; a regression
        to ``loaded = json.loads(...)`` without that guard would let /doctor
        index into a list and raise inside the diagnose engine.
        """
        config_path = tmp_path / "config.json"
        config_path.write_text("[1, 2, 3]", encoding="utf-8")

        result = doctor.read_config(str(config_path))
        assert result.status == "malformed"
        assert "expected JSON object" in result.detail

    def test_unreadable_file_returns_unreadable(self, tmp_path: Path) -> None:
        """An ``OSError`` other than ``FileNotFoundError`` returns ``status='unreadable'``.

        Gap: the ``OSError`` arm of ``read_config`` was uncovered — a host
        where the config file became unreadable for any reason other than
        "missing" would otherwise return the wrong verdict.
        """
        # The file is a directory, so open raises IsADirectoryError (an OSError).
        config_path = tmp_path / "config.json"
        config_path.mkdir()

        result = doctor.read_config(str(config_path))
        assert result.status == "unreadable"
        # A directory open raises IsADirectoryError on POSIX and PermissionError on Windows; the class name is not assertable, the path is.
        assert str(config_path) in result.detail

    def test_missing_file_returns_missing(self, tmp_path: Path) -> None:
        """An absent config file returns ``status='missing'`` with the path in the detail.

        Gap: the ``FileNotFoundError`` arm of ``read_config`` was the only
        exception branch the round did not already exercise via a sibling
        test — ``read_ledger``'s missing arm is exercised by the existing
        ``test_missing_engine_emits_diagnosis_json`` fixture, so this pins
        the read_config mirror.
        """
        missing_path = tmp_path / "absent.json"

        result = doctor.read_config(str(missing_path))
        assert result.status == "missing"
        assert str(missing_path) in result.detail
        assert "does not exist" in result.detail


class TestReadLedgerExceptBranches:
    """Round B1 extensions — cover the two ``read_ledger`` exception arms."""

    def test_unreadable_file_returns_unreadable(self, tmp_path: Path) -> None:
        """``read_ledger`` mirrors ``read_config``: a directory-as-file raises ``unreadable``.

        Gap: lines 619-620 in ``scripts/doctor.py`` were uncovered alongside
        the read_config arms — the OSError branches share the same reasoning
        as row 7 and are required for the module to hit 100%.
        """
        state_home = tmp_path / "state"
        (state_home / "voice-loop").mkdir(parents=True)
        # Replace install.ledger with a directory so open raises IsADirectoryError.
        (state_home / "voice-loop" / "install.ledger").mkdir()

        result = doctor.read_ledger(str(state_home))
        assert result.status == "unreadable"
        # A directory open raises IsADirectoryError on POSIX and PermissionError on Windows; the class name is not assertable, the path is.
        assert str(state_home / "voice-loop" / "install.ledger") in result.detail

    def test_malformed_json_returns_malformed(self, tmp_path: Path) -> None:
        """Invalid JSON in ``install.ledger`` returns ``status='malformed'``.

        Gap: lines 621-622 were uncovered — a corrupted ledger would
        otherwise crash with ``json.JSONDecodeError`` instead of being
        surfaced as a structured verdict for /doctor.
        """
        state_home = tmp_path / "state"
        (state_home / "voice-loop").mkdir(parents=True)
        (state_home / "voice-loop" / "install.ledger").write_text(
            "{not valid", encoding="utf-8"
        )

        result = doctor.read_ledger(str(state_home))
        assert result.status == "malformed"
        assert "JSONDecodeError" in result.detail


class TestReadLogsHappyPath:
    """Round B1 extensions — cover the ``read_logs`` happy path and first-open OSError arm."""

    def test_readable_logs_return_ok_status_with_tail(
        self, tmp_path: Path
    ) -> None:
        """A populated ``speak.log`` is read successfully and the tail is preserved.

        Gap: lines 650-652 were uncovered — every existing read_logs test
        fed in a missing path, so the happy path itself (file present,
        content read, lines truncated to ``tail_lines``) was untested.
        """
        state_home = tmp_path / "state"
        log_dir = state_home / "voice-loop"
        log_dir.mkdir(parents=True)
        speak_log = log_dir / "speak.log"
        dictate_log = log_dir / "dictate.log"
        # More than tail_lines lines so the truncation actually has work to do.
        for log in (speak_log, dictate_log):
            # Two lines per log is enough; the speak truncation gets its
            # work from a separate, longer fixture below.
            log.write_text("line a\nline b\n", encoding="utf-8")
        speak_log.write_text(
            "\n".join(f"line {i}" for i in range(80)) + "\n",
            encoding="utf-8",
        )

        result = doctor.read_logs(str(state_home), tail_lines=10)
        # Both logs present and valid → overall status is ok.
        assert result.status == "ok"
        assert result.source_status["speak.log"] == "ok"
        assert result.source_status["dictate.log"] == "ok"
        assert len(result["speak.log"]) == 10
        assert result["speak.log"][-1] == "line 79"

    def test_first_open_raises_oserror_marks_source_unreadable(
        self, tmp_path: Path
    ) -> None:
        """Row 8 — a directory at the log path raises ``IsADirectoryError``, marked ``unreadable``.

        Gap: only the ``FileNotFoundError`` arm of the first open was
        exercised; any other ``OSError`` (the same family ``IsADirectoryError``
        belongs to) is what this branch handles.
        """
        state_home = tmp_path / "state"
        log_dir = state_home / "voice-loop"
        log_dir.mkdir(parents=True)
        # Replace speak.log with a directory so the first open raises
        # IsADirectoryError (a subclass of OSError, distinct from FileNotFoundError).
        (log_dir / "speak.log").mkdir()

        result = doctor.read_logs(str(state_home))
        assert result.source_status["speak.log"] == "unreadable"
        # A directory open raises IsADirectoryError on POSIX and PermissionError on Windows; the class name is not assertable, the path is.
        assert str(state_home / "voice-loop" / "speak.log") in result.source_details["speak.log"]
        assert result.source_status["dictate.log"] == "missing"
        assert result.status == "degraded"


class TestReadLogsSecondOpenRaises:
    """Row 9 — the second open in ``read_logs`` raises a structured exception."""

    def test_second_open_raises_filenotfounderror_marks_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The file vanishes between reads → ``status='missing'``, detail names the cause.

        Gap: lines 669-671 in ``scripts/doctor.py`` were uncovered — the
        race between the tail read and the re-read for a UTF-8 decode error
        must surface as a structured verdict, not a crash.
        """
        state_home = tmp_path / "state"
        log_dir = state_home / "voice-loop"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "speak.log"
        log_path.write_text("first line\nsecond line\n", encoding="utf-8")

        real_open = builtins.open
        call_count = {"n": 0}

        def counting_open(path, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2 and str(path).endswith("speak.log"):
                raise FileNotFoundError("vanished")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)

        result = doctor.read_logs(str(state_home))
        assert result.source_status["speak.log"] == "missing"
        # The detail names the path and the cause: ``f"{path} disappeared after reading"``.
        assert "disappeared after reading" in result.source_details["speak.log"]
        assert "speak.log" in result.source_details["speak.log"]

    def test_second_open_raises_oserror_marks_unreadable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The second open raises a non-FileNotFoundError ``OSError`` → ``unreadable``.

        Gap: lines 672-674 in ``scripts/doctor.py`` were uncovered — the
        read-recheck handshake has to fall through every OSError branch.
        """
        state_home = tmp_path / "state"
        log_dir = state_home / "voice-loop"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "speak.log"
        log_path.write_text("first line\nsecond line\n", encoding="utf-8")

        real_open = builtins.open
        call_count = {"n": 0}

        def counting_open(path, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2 and str(path).endswith("speak.log"):
                raise OSError("second read blocked")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)

        result = doctor.read_logs(str(state_home))
        assert result.source_status["speak.log"] == "unreadable"
        assert "OSError" in result.source_details["speak.log"]

    def test_second_open_raises_unicodedecodeerror_marks_malformed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The second open sees a malformed UTF-8 byte → ``malformed`` with the decoder name.

        Gap: lines 666-668 in ``scripts/doctor.py`` were uncovered — a log
        that was readable on the first pass (because the first pass uses
        ``errors='replace'``) but not on the second (which uses strict UTF-8)
        must surface as a structured verdict rather than a crash.
        """
        state_home = tmp_path / "state"
        log_dir = state_home / "voice-loop"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "speak.log"
        # Bytes that ``errors='replace'`` makes it through, but strict UTF-8 rejects.
        log_path.write_bytes(b"first line\n\xff\xfe bad utf-8\n")

        result = doctor.read_logs(str(state_home))
        assert result.source_status["speak.log"] == "malformed"
        assert "UnicodeDecodeError" in result.source_details["speak.log"]


class TestLoadManifestDefensiveRaises:
    """Row 10 — the two ``load_manifest`` defensive raises carry the manifest path."""

    def test_spec_is_none_raises_filenotfounderror(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``importlib.util.spec_from_file_location`` returning ``None`` → ``FileNotFoundError``.

        Gap: line 687 in ``scripts/doctor.py`` was uncovered — a malformed
        manifest file (one that the import machinery refuses to spec) must
        surface as a missing-manifest error, not a silent empty checklist.
        """
        import importlib.util

        monkeypatch.setattr(
            importlib.util,
            "spec_from_file_location",
            lambda name, location, *args, **kwargs: None,
        )

        # ``load_manifest`` resolves ``plugin_root / "skills" / "doctor" / "check_manifest.py"``;
        # the error message names THAT path, not whatever the caller passes.
        manifest_path = tmp_path / "skills" / "doctor" / "check_manifest.py"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("# anything", encoding="utf-8")

        with pytest.raises(FileNotFoundError) as raised:
            doctor.load_manifest(tmp_path)
        assert "manifest not found" in str(raised.value)
        assert str(manifest_path) in str(raised.value)

    def test_spec_loader_is_none_raises_runtimeerror(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A spec without a loader → ``RuntimeError`` naming the manifest path.

        Gap: line 690 in ``scripts/doctor.py`` was uncovered — an environment
        that produces a spec without a usable loader must surface as a
        distinct diagnostic, not a generic ``NoneType`` crash inside exec_module.
        """
        import importlib.util

        class FakeSpec:
            # ``importlib.util.module_from_spec`` reads several spec
            # attributes; stand in with sentinels so the call does not
            # raise AttributeError BEFORE we reach the line we are testing.
            name = "fake_spec"
            loader = None
            submodule_search_locations = None
            parent = None
            has_location = False
            origin = None

        monkeypatch.setattr(
            importlib.util,
            "spec_from_file_location",
            lambda name, location, *args, **kwargs: FakeSpec(),
        )

        manifest_path = tmp_path / "skills" / "doctor" / "check_manifest.py"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("# anything", encoding="utf-8")

        with pytest.raises(RuntimeError) as raised:
            doctor.load_manifest(tmp_path)
        assert "loader is None" in str(raised.value)
        assert str(manifest_path) in str(raised.value)


class TestMainArgvFallsBackToSysArgv:
    """Row 11 — ``main(argv=None)`` reads ``sys.argv`` when no argv is supplied."""

    def test_argv_none_uses_sys_argv_for_help_message(
        self, monkeypatch, capsys
    ) -> None:
        """Calling ``main()`` with no argv honours ``sys.argv`` — including argv[0] in the help line.

        Gap: line 697 in ``scripts/doctor.py`` was reachable only via the
        ``runpy`` test in row 4; this pins it independently so a regression
        to ``argv = []`` is caught even if the runpy shape changes.
        """
        monkeypatch.setattr(sys, "argv", ["doctor.py", "--help"])

        rc = doctor.main()
        assert rc == 0
        assert "doctor.py" in capsys.readouterr().err


class TestMainWslBoundaryBlockSkipped:
    """Row 12 — when config is PRESENT, the WSL block and finding filter are SKIPPED.

    A single end-to-end ``main()`` run with a non-empty config drives the
    false arm at branch 739→745 (``not config`` is False, falls through) and
    keeps ``wsl_finding`` None at 835, so the filter at 835→842 is also a
    no-op pass.
    """

    def test_present_config_skips_wsl_block_and_filter(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        """With a config present, no WSL boundary finding is appended and the filter is a no-op.

        Gap: branches 739→745 and 835→842 in ``scripts/doctor.py`` were
        uncovered — both are branch-only arcs with no missed statement
        behind them, and both fall through in this single run.
        """
        config_path = tmp_path / "config.json"
        # A non-empty object: ``DiagnosticData`` extends ``dict``, so an
        # empty body would make ``not config`` True and re-enter the
        # WSL block we are trying to bypass.
        config_path.write_text(
            json.dumps({"speak": {"enabled": True}}),
            encoding="utf-8",
        )
        state_home = tmp_path / "state"

        # Force the file readers and loaders to their real implementations so
        # the WSL-block predicate (``not config``) sees a non-empty config.
        monkeypatch.setattr(doctor, "_default_state_home", lambda: str(state_home))
        monkeypatch.setattr(doctor, "_default_config_path", lambda: str(config_path))
        monkeypatch.setattr(doctor, "_sill_core_root", lambda: _core_root)
        # An absent WSL finding is exactly what this test asserts: the
        # branch 835→842 arc is the "no filter" pass when ``wsl_finding`` stays None.
        # Track whether the WSL boundary finding was ever probed.
        probe_called = {"n": 0}

        def counting_probe(**kwargs):
            probe_called["n"] += 1
            return None

        monkeypatch.setattr(doctor, "_wsl_boundary_finding", counting_probe)

        rc = doctor.main(["doctor.py"])
        assert rc == 0
        # The WSL probe is inside the ``if not config`` block; with a
        # non-empty config the probe is never invoked, which is the
        # branch 739→745 false arm.
        assert probe_called["n"] == 0

        rendered = json.loads(capsys.readouterr().out)
        # Branch 835→842 false arm: ``wsl_finding`` stayed None, so the
        # filter list-comprehension is a no-op pass and the findings the
        # engine produced are preserved as-is.
        keys = [finding["key"] for finding in rendered["findings"]]
        assert "wsl" not in keys


# ---------------------------------------------------------------------------
# Pinned pragma list — the source-text test that holds the allow-list to its
# enumerated size.
# ---------------------------------------------------------------------------


class TestPinnedPragmaList:
    """The no-cover pragma list in ``scripts/doctor.py`` is exactly three entries.

    Each pragma corresponds to a Windows-only body branch that the Linux
    coverage job cannot exercise without faking the Windows API — a fake
    would itself need a correctness argument, so the discipline is to enumerate
    the allow-list in the PR body and pin the count in this test so an
    unbounded pragma cannot slip through.
    """

    def test_no_cover_pragmas_are_exactly_three_entries(self) -> None:
        """The sorted 1-indexed line numbers carrying ``# pragma: no cover`` equal the pinned list.

        Today that list is ``[198, 239, 289]``. The list grows ONLY when a
        new Windows-only body is added AND named in the PR body's
        allow-list in the same commit.
        """
        source = _DOCTOR_PATH.read_text(encoding="utf-8")
        pragma_lines = sorted(
            index
            for index, line in enumerate(source.splitlines(), start=1)
            if "pragma: no cover" in line
        )
        assert pragma_lines == [198, 239, 289]

    def test_pinned_lines_carry_the_windows_only_reason(self) -> None:
        """Each pinned pragma sits on a Windows-only body branch.

        The reason appears verbatim on the same line (the brief specifies a
        one-line reason per entry), and the substring "Windows-only" is the
        vocabulary that gates it.
        """
        source_lines = _DOCTOR_PATH.read_text(encoding="utf-8").splitlines()
        for line_number in (198, 239, 289):
            assert "Windows-only" in source_lines[line_number - 1], (
                f"line {line_number} pragma lost its Windows-only reason: "
                f"{source_lines[line_number - 1]!r}"
            )


# ---------------------------------------------------------------------------
# Row 4 — the __main__ guard is covered with runpy + monkeypatched sys.argv
# ---------------------------------------------------------------------------


class TestScriptEntrypointRequiresACommand:
    """``doctor.py`` executed as ``__main__`` exits with the parsed code."""

    def test_main_module_execution_exits(self, monkeypatch) -> None:
        """Running the file as ``__main__`` propagates ``SystemExit(main())``.

        Gap: lines 871-872 in ``scripts/doctor.py`` were uncovered — the
        ``__main__`` guard is a one-liner whose body IS the executable
        entry, and it is the shape five sibling scripts use.
        """
        # Mirror ``test_report_bug.py``'s pattern: monkeypatch ``sys.argv`` so the
        # unknown-argument branch fires deterministically.  An argv that
        # contains an unknown flag returns 2 and raises SystemExit(2).
        monkeypatch.setattr(sys, "argv", ["doctor.py", "--unknown"])
        with pytest.raises(SystemExit) as raised:
            runpy.run_path(str(_DOCTOR_PATH), run_name="__main__")
        assert raised.value.code == 2
