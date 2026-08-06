"""Diagnosis engine — three-bin verdict for windowsill plugins.

Plugin-agnostic: config-reader, ledger-reader, verdict shapes know nothing
about any specific plugin.  Plugins contribute a declarative check manifest
and the raw data to run it against (a config dict, a ledger dict, log tails).

Pattern bricks consulted:
- ``injected-clock`` — clock is an injected callable; the engine itself reads
  no timestamps, but consumers (the skill's script) pass UTC-aware timestamps
  that were produced by an injectable clock.
- ``addressable-id-ledger`` — the *Bin* enum is an addressable vocabulary (the
  skill cites ``Bin.CONSEQUENCE_OF_CHOICE`` by name, never a bare string).

Usage (from a plugin's script or skill)::

    from sill_core.diagnosis import Bin, Finding, diagnose

    findings = diagnose(
        manifest=MY_PLUGIN_CHECK_MANIFEST,
        config=read_my_config(),
        ledger=read_my_ledger(),
    )
    for f in findings:
        if f.bin == Bin.CONSEQUENCE_OF_CHOICE:
            ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Addressable vocabulary — the three bins
# ---------------------------------------------------------------------------


class Bin(Enum):
    """The three diagnosis bins, in the order they are ruled out.

    *CONSEQUENCE_OF_CHOICE* first — a config setting the user chose is the
    most common "nothing works" and the fastest to verify.  *UNFINISHED_INSTALL*
    next — the install ledger is the ground truth.  *REAL_ANOMALY* last — only
    after the first two bins are ruled out do we look for a genuine bug.
    """

    CONSEQUENCE_OF_CHOICE = "consequence_of_choice"
    UNFINISHED_INSTALL = "unfinished_install"
    REAL_ANOMALY = "real_anomaly"


# ---------------------------------------------------------------------------
# Finding — what one check produced
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One diagnosis finding, ready for the skill to present.

    Every finding carries enough context for the skill to explain it to the
    user without re-reading the manifest.  The *bin* is the classification;
    *offer_flip* and *flip_* fields name a config change the user can accept
    with a single confirmation — everything else is a proposal only.
    """

    bin: Bin
    """Which of the three bins this finding belongs to."""

    key: str
    """Stable identifier the manifest author chose (e.g. ``"auto_paste_off"``)."""

    title: str
    """One-line human-readable summary (e.g. ``"Auto-paste is disabled"``)."""

    explanation: str
    """One or two sentences explaining WHAT was found and WHY it explains the
    observed behaviour.  Shown to the user verbatim."""

    fix: str = ""
    """One or two sentences describing the fix.  Shown to the user; the skill
    must still ASK before applying."""

    offer_flip: bool = False
    """When True, the finding carries a concrete, low-risk config change the
    user can accept with a single confirmation (``flip_path`` → ``flip_value``).
    The skill shows the change and applies it only on an explicit yes."""

    flip_path: str = ""
    """Dot-separated path into the config dict (e.g. ``"dictate.auto_paste"``).
    Only meaningful when *offer_flip* is True."""

    flip_value: object = None
    """The value to set at *flip_path*.  Only meaningful when *offer_flip* is
    True."""

    evidence: dict[str, Any] = field(default_factory=dict)
    """Machine-readable backing for this finding — the config value that
    triggered it, the ledger step that is incomplete, the log tail that
    matched.  Used by the /report-bug handoff for real anomalies."""


# ---------------------------------------------------------------------------
# Manifest shape — what a plugin declares
#
# A "check" in the manifest is a dict with at minimum the keys ``bin``,
# ``key``, ``title``, ``explanation``, and ``check``.  The ``check`` sub-dict
# has a ``type`` field and type-specific fields below.
#
#   config check:
#     {"type": "config", "path": "dictate.auto_paste", "equals": False}
#     Triggers when the value at *path* (dot-separated into the config dict)
#     equals *equals*.  ``"not_equals": ""`` triggers when the value is a
#     non-empty string (e.g. a custom command is set).  When neither
#     *equals* nor *not_equals* is present, the check triggers on any
#     falsy value.
#
#   ledger check:
#     {"type": "ledger", "steps": ["step-3-install-deps", "step-4-write-config"]}
#     Triggers when any of the named steps are not ``"complete"``.  *require_all*
#     (default True) requires every step to be complete; set it to False to
#     trigger when none are.
#
#   log check:
#     {"type": "log", "source": "speak.log", "pattern": "unreachable"}
#     Triggers when *pattern* (a plain substring match) appears in the named
#     log tail.  *count* (optional) triggers only when the pattern appears at
#     least that many times.
#
# The optional ``fix``, ``offer_flip``, ``flip_path`` and ``flip_value``
# fields in the top-level check carry through to the Finding unchanged.
# ---------------------------------------------------------------------------


_MISSING: object = object()
"""Module-level sentinel for missing config paths.  Exported so tests can
use ``is _MISSING`` without re-creating the sentinel object."""


def _resolve_path(data: dict[str, Any], path: str) -> object:
    """Walk a dot-separated *path* into a nested dict, returning the value
    or ``_MISSING`` when any segment is missing."""
    current: object = data
    for segment in path.split("."):
        if not isinstance(current, dict):
            return _MISSING
        current = current.get(segment, _MISSING)
        if current is _MISSING:
            return _MISSING
    return current


def _run_config_check(check: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    """Evaluate one config-type check.  Returns evidence dict on trigger, None on pass."""
    path: str = check.get("path", "")
    value = _resolve_path(config, path)
    if value is _MISSING:
        return None  # path not present → check passes

    # Reject manifests that specify both equals and not_equals — the precedence
    # is non-obvious and either would suppress the other silently.
    if "equals" in check and "not_equals" in check:
        raise ValueError(
            f"check for path '{path}' specifies both 'equals' and 'not_equals'; "
            "specify only one condition"
        )

    if "equals" in check:
        triggered = value == check["equals"]
    elif "not_equals" in check:
        triggered = value != check["not_equals"]
    else:
        triggered = not value

    if not triggered:
        return None
    return {"path": path, "value": value}


def _run_ledger_check(check: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any] | None:
    """Evaluate one ledger-type check.  Returns evidence dict on trigger, None on pass."""
    steps: list[str] = check.get("steps", [])
    require_all: bool = check.get("require_all", True)
    completed: list[str] = ledger.get("completed_steps", [])
    current: str | None = ledger.get("current_step")

    incomplete = [s for s in steps if s not in completed]
    if require_all:
        triggered = len(incomplete) > 0
    else:
        triggered = len(incomplete) == len(steps)

    if not triggered:
        return None
    evidence: dict[str, Any] = {"incomplete_steps": incomplete}
    if current:
        evidence["current_step"] = current
    evidence["ledger_state"] = ledger.get("state", "unknown")
    return evidence


def _run_log_check(check: dict[str, Any], logs: dict[str, list[str]]) -> dict[str, Any] | None:
    """Evaluate one log-type check.  Returns evidence dict on trigger, None on pass."""
    source: str = check.get("source", "")
    pattern: str = check.get("pattern", "")
    min_count: int = check.get("count", 1)

    lines = logs.get(source, [])
    matches = [line for line in lines if pattern in line]
    if len(matches) < min_count:
        return None
    # When count is 0, the check triggers on zero matches (not intended) but we
    # still want to show all matches in evidence. For count > 0, show the last N.
    # Use min(len(matches), max(1, min_count)) to avoid the matches[-0:] edge case.
    shown = matches[-min_count:] if min_count > 0 else matches
    return {
        "source": source,
        "pattern": pattern,
        "match_count": len(matches),
        "matches": shown,
    }


# Runners dispatched by check type — a plugin author never touches this dict.
# The same key selects the source ``diagnose()`` hands the runner, so a runner
# registered here without a matching source fails loudly (KeyError) on its first
# check instead of silently producing no finding.
_CHECK_RUNNERS = {
    "config": _run_config_check,
    "ledger": _run_ledger_check,
    "log": _run_log_check,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def diagnose(
    manifest: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
    logs: dict[str, list[str]] | None = None,
) -> list[Finding]:
    """Run every check in *manifest* against the provided sources.

    Sources are passed explicitly — the engine never reads a file or makes a
    network call.  The caller (a script or a skill) is responsible for reading
    the config file, the install ledger, and the log tails before calling
    ``diagnose()``.

    Findings are returned in bin order: consequence-of-choice first, then
    unfinished install, then real anomaly.  Within each bin the manifest order
    is preserved.
    """
    # Keyed by CHECK TYPE, not by parameter name — the same key that selects
    # the runner selects the source it is handed.
    sources: dict[str, Any] = {
        "config": config or {},
        "ledger": ledger or {},
        "log": logs or {},
    }

    findings: list[Finding] = []

    for item in manifest:
        bin_name: str = item.get("bin", "")
        try:
            bin_ = Bin(bin_name)
        except ValueError:
            continue  # unknown bin → skip silently (the manifest is trusted)

        check_def: dict[str, Any] = item.get("check", {})
        check_type: str = check_def.get("type", "")
        runner = _CHECK_RUNNERS.get(check_type)
        if runner is None:
            continue  # unknown check type → skip

        evidence = runner(check_def, sources[check_type])

        if evidence is not None:
            findings.append(
                Finding(
                    bin=bin_,
                    key=item.get("key", ""),
                    title=item.get("title", ""),
                    explanation=item.get("explanation", ""),
                    fix=item.get("fix", ""),
                    offer_flip=item.get("offer_flip", False),
                    flip_path=item.get("flip_path", ""),
                    flip_value=item.get("flip_value"),
                    evidence=evidence,
                )
            )

    # Sort by bin order: CONSEQUENCE_OF_CHOICE first, then UNFINISHED_INSTALL,
    # then REAL_ANOMALY.  Within each bin preserve manifest order.
    _bin_order = {b: i for i, b in enumerate(Bin)}
    findings.sort(key=lambda f: _bin_order.get(f.bin))
    return findings
