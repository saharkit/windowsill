"""Tests for the desktop launcher that survives versioned plugin updates.

The launcher is intentionally tested at its registry-resolution seam: a unit test
can prove the update junction without starting a recorder or a desktop daemon.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path


_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_LOADER = importlib.machinery.SourceFileLoader(
    "voice_loop_dictate_launcher", str(_SCRIPTS / "voice-loop-dictate")
)
_SPEC = importlib.util.spec_from_loader(_LOADER.name, _LOADER)
assert _SPEC and _SPEC.loader
_launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_launcher)


def _write_registry(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    registry = tmp_path / "plugins" / "installed_plugins.json"
    registry.parent.mkdir()
    registry.write_text(
        json.dumps({"version": 2, "plugins": {"voice-loop@windowsill": entries}}),
        encoding="utf-8",
    )
    return registry


def _install(tmp_path: Path, version: str) -> Path:
    root = tmp_path / "cache" / "voice-loop" / version
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "dictate-toggle.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def test_registry_resolution_follows_update_without_version_sorting(tmp_path, monkeypatch):
    """Gap: a launcher that globbed cache directories would keep invoking version A."""
    old = _install(tmp_path, "0.6.0")
    current = _install(tmp_path, "0.8.0")
    _write_registry(tmp_path, [{"installPath": str(current), "scope": "user"}])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert _launcher._current_script() == current / "scripts" / "dictate-toggle.sh"
    assert old.exists(), "the test models an update where the old cache may still linger"


def test_missing_registry_fails_closed(tmp_path, monkeypatch):
    """Gap: a missing registry must not silently guess a cache directory."""
    _install(tmp_path, "0.8.0")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert _launcher._current_script() is None


def test_ambiguous_registry_fails_closed(tmp_path, monkeypatch):
    """Gap: two installed candidates must not send a hotkey to an arbitrary version."""
    first = _install(tmp_path, "0.8.0")
    second = _install(tmp_path, "0.9.0")
    _write_registry(
        tmp_path,
        [{"installPath": str(first)}, {"installPath": str(second)}],
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert _launcher._current_script() is None


def test_main_execs_current_script_with_hotkey_arguments(tmp_path, monkeypatch):
    """Gap: resolving the right file is insufficient if setup arguments are dropped."""
    current = _install(tmp_path, "0.8.0")
    _write_registry(tmp_path, [{"installPath": str(current)}])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(_launcher.sys, "argv", ["launcher", "send"])
    calls: list[tuple[str, list[str]]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        calls.append((path, argv))
        raise OSError("stop test exec")

    monkeypatch.setattr(_launcher.os, "execv", fake_execv)

    assert _launcher.main() == 0
    assert calls == [(str(current / "scripts" / "dictate-toggle.sh"), [str(current / "scripts" / "dictate-toggle.sh"), "send"])]
