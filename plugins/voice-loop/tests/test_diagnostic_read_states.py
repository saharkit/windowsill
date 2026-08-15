"""Diagnostic reader states: missing, unreadable and malformed never collapse.

Mutation gaps closed:
- deleting the reader's status classification would make missing/unreadable/malformed
  indistinguishable and allow a false healthy/none result;
- accepting a non-object JSON document as a valid diagnostic source would report a
  state the tool never verified.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import doctor
import install_ledger
import report_bug
import contour_poll


@pytest.mark.parametrize("payload", ["{broken", "[]"])
def test_doctor_config_distinguishes_malformed_sources(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")
    malformed = doctor.read_config(str(path))
    missing = doctor.read_config(str(tmp_path / "missing.json"))
    unreadable_path = tmp_path / "config-dir"
    unreadable_path.mkdir()
    unreadable = doctor.read_config(str(unreadable_path))
    assert malformed.status == "malformed"
    assert missing.status == "missing"
    assert unreadable.status == "unreadable"
    assert len({malformed.status, missing.status, unreadable.status}) == 3


def test_doctor_ledger_distinguishes_all_read_states(tmp_path):
    state_home = tmp_path / "state"
    ledger_dir = state_home / "voice-loop"
    ledger_dir.mkdir(parents=True)
    missing = doctor.read_ledger(str(state_home))
    (ledger_dir / "install.ledger").write_text("{broken", encoding="utf-8")
    malformed = doctor.read_ledger(str(state_home))
    (ledger_dir / "install.ledger").unlink()
    (ledger_dir / "install.ledger").mkdir()
    unreadable = doctor.read_ledger(str(state_home))
    assert {missing.status, malformed.status, unreadable.status} == {"missing", "malformed", "unreadable"}


def test_install_ledger_check_reports_malformed_instead_of_none(tmp_path):
    path = str(tmp_path / "install.ledger")
    Path(path).write_text("{broken", encoding="utf-8")
    result = install_ledger.check_state(path)
    assert result["state"] == "malformed"
    assert "read_detail" in result


def test_contour_status_distinguishes_all_read_states(tmp_path):
    path = tmp_path / "status.json"
    missing = contour_poll.read_status(str(path), warn=lambda _: None)
    path.write_text("{broken", encoding="utf-8")
    malformed = contour_poll.read_status(str(path), warn=lambda _: None)
    path.unlink()
    path.mkdir()
    unreadable = contour_poll.read_status(str(path), warn=lambda _: None)
    assert {missing.status, malformed.status, unreadable.status} == {"missing", "malformed", "unreadable"}


def test_report_bundle_config_distinguishes_missing_unreadable_malformed(tmp_path, monkeypatch):
    missing = report_bug.read_json(str(tmp_path / "missing.json"))[1]
    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("{broken", encoding="utf-8")
    malformed = report_bug.read_json(str(malformed_path))[1]
    unreadable_path = tmp_path / "unreadable"
    unreadable_path.mkdir()
    unreadable = report_bug.read_json(str(unreadable_path))[1]
    assert missing != malformed != unreadable
    assert missing == "absent"
    assert malformed.startswith("unreadable")
    assert unreadable.startswith("unreadable")
