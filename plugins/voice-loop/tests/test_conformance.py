"""The conformance checklist's contract: version pinned, every row a verdict cell, and every
section the task requires.

These tests validate the STRUCTURE of CONFORMANCE.md — they do not execute the checklist
(that is what the /conformance skill does, interactively, with a human at the keyboard).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_CONFORMANCE = _PLUGIN / "CONFORMANCE.md"
_PLUGIN_JSON = _PLUGIN / ".claude-plugin" / "plugin.json"
_SKILL = _PLUGIN / "skills" / "conformance" / "SKILL.md"


# --- helpers -----------------------------------------------------------------------


def _conformance_text() -> str:
    return _CONFORMANCE.read_text(encoding="utf-8")


def _plugin_version() -> str:
    return json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))["version"]


def _table_rows(text: str) -> list[dict[str, str]]:
    """Extract every data row from every markdown table in `text`.

    Returns a list of dicts keyed by the column headers of each table.
    Tracks table boundaries: the first row of a new table is the header,
    the next row must be a separator, and everything after is data until
    a non-table line ends the table.
    """
    rows: list[dict[str, str]] = []
    headers: list[str] = []
    in_table = False
    past_separator = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            if all(re.fullmatch(r"-{3,}", c) for c in cells):
                past_separator = True
                continue
            if not in_table:
                # First row of a new table: treat as header.
                headers = [c for c in cells]
                in_table = True
                past_separator = False
                continue
            if not past_separator or not headers:
                continue
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
        else:
            in_table = False
            past_separator = False
    return rows


# --- version pinning ---------------------------------------------------------------


def test_checklist_version_matches_plugin_version():
    """The version in CONFORMANCE.md must equal the version in plugin.json.

    A mismatch means the checklist was not updated when the plugin was released —
    a stale checklist is a checklist that tests the wrong behaviour.
    """
    text = _conformance_text()
    plugin_ver = _plugin_version()

    # The version appears in the title: "# voice-loop conformance — v0.4.0"
    title_match = re.search(r"# voice-loop conformance — v(\d+\.\d+\.\d+)", text)
    assert title_match, "CONFORMANCE.md title must include a version (e.g. 'v0.4.0')"
    title_ver = title_match.group(1)
    assert title_ver == plugin_ver, (
        f"CONFORMANCE.md title version ({title_ver}) does not match "
        f"plugin.json version ({plugin_ver})"
    )

    # The footer also carries the version
    footer_match = re.search(r"\*\*Checklist version:\*\* (\d+\.\d+\.\d+)", text)
    assert footer_match, "CONFORMANCE.md footer must carry the checklist version"
    footer_ver = footer_match.group(1)
    assert footer_ver == plugin_ver, (
        f"CONFORMANCE.md footer version ({footer_ver}) does not match "
        f"plugin.json version ({plugin_ver})"
    )

    # The environment table also pins the version
    env_match = re.search(r"\| plugin version \| (\d+\.\d+\.\d+) \|", text)
    assert env_match, "CONFORMANCE.md environment table must pin the plugin version"
    env_ver = env_match.group(1)
    assert env_ver == plugin_ver, (
        f"CONFORMANCE.md environment-table version ({env_ver}) does not match "
        f"plugin.json version ({plugin_ver})"
    )


# --- checklist rows have verdict and evidence cells ----------------------------------


_REQUIRED_SECTIONS = {
    "1.": "Install",
    "2.": "Dictation",
    "3.": "Speak-back",
    "4.": "Degrade paths",
    "5.": "Uninstall",
}


def test_every_scenario_row_has_verdict_and_evidence_columns():
    """Every data row in a checklist table must have 'verdict' and 'evidence' columns.

    A row without a verdict cell is a row that can never be adjudicated.
    """
    text = _conformance_text()
    rows = _table_rows(text)
    checklist_rows = [r for r in rows if re.match(r"^\d", r.get("#", ""))]
    assert len(checklist_rows) > 0, "no checklist rows found in CONFORMANCE.md"

    missing_verdict = []
    missing_evidence = []
    for row in checklist_rows:
        if "verdict" not in row:
            missing_verdict.append(row.get("#", "?"))
        if "evidence" not in row:
            missing_evidence.append(row.get("#", "?"))

    assert not missing_verdict, (
        f"rows missing 'verdict' column: {missing_verdict}"
    )
    assert not missing_evidence, (
        f"rows missing 'evidence' column: {missing_evidence}"
    )


def test_every_required_section_has_at_least_one_row():
    """Each section named in the task brief must have at least one checklist row."""
    text = _conformance_text()
    rows = _table_rows(text)

    section_numbers = {row.get("#", "")[:2] for row in rows if re.match(r"^\d", row.get("#", ""))}
    for prefix, name in _REQUIRED_SECTIONS.items():
        assert prefix in section_numbers, (
            f"section '{name}' ({prefix}) has no checklist rows"
        )


def test_environment_table_has_all_required_fields():
    """The environment table must have the fields the skill needs to fill at runtime."""
    text = _conformance_text()
    rows = _table_rows(text)

    # The environment table is the first table in the document — its rows have no
    # leading number in the first column.
    env_rows = [r for r in rows if not re.match(r"^\d", r.get("#", ""))]
    fields = {r.get("field", "") for r in env_rows}
    required = {"date", "tester", "plugin version", "backends chosen (stt / tts)", "language"}
    missing = required - fields
    assert not missing, f"environment table missing fields: {missing}"


def test_no_duplicate_row_numbers():
    """Every checklist row must have a unique number."""
    text = _conformance_text()
    rows = _table_rows(text)
    numbers = [r["#"] for r in rows if re.match(r"^\d", r.get("#", ""))]
    seen: set[str] = set()
    dupes = []
    for n in numbers:
        if n in seen:
            dupes.append(n)
        seen.add(n)
    assert not dupes, f"duplicate row numbers: {dupes}"


# --- the skill file exists and has valid frontmatter ----------------------------------


def test_conformance_skill_exists_and_has_valid_frontmatter():
    """The /conformance skill must be present and its frontmatter must parse."""
    assert _SKILL.is_file(), f"skill file not found at {_SKILL}"

    text = _SKILL.read_text(encoding="utf-8")
    # YAML frontmatter between --- delimiters
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter (---)"
    parts = text.split("---", 2)
    assert len(parts) >= 3, "SKILL.md must have opening and closing --- for frontmatter"

    frontmatter = parts[1].strip()
    for key in ("name:", "description:", "allowed-tools:"):
        assert key in frontmatter, f"SKILL.md frontmatter missing '{key}'"

    # name must be 'conformance'
    name_match = re.search(r"^name:\s*(.+)", frontmatter, re.MULTILINE)
    assert name_match, "SKILL.md frontmatter missing 'name'"
    assert name_match.group(1).strip() == "conformance", (
        f"skill name must be 'conformance', got '{name_match.group(1).strip()}'"
    )


# --- the report file naming convention ------------------------------------------------


def test_report_filename_convention_is_documented():
    """The skill must document the report filename pattern so the artifact is predictable."""
    skill_text = _SKILL.read_text(encoding="utf-8")
    assert "conformance-v" in skill_text, (
        "SKILL.md must document the report filename pattern (conformance-v<version>-<YYYYMMDD>.md)"
    )


# --- the transport reuses report-bug infrastructure ------------------------------------


def test_skill_references_report_bug_transports():
    """The conformance skill must reference the report-bug transports it reuses."""
    skill_text = _SKILL.read_text(encoding="utf-8")
    assert "report-bug" in skill_text, (
        "SKILL.md must reference the /report-bug transports it reuses"
    )
    assert "conformance" in skill_text, (
        "SKILL.md must reference the 'conformance' label for issue triage"
    )
