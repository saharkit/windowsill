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


def test_missing_registry_fails_closed(tmp_path, monkeypatch, capsys):
    """Gap: a missing registry must not silently guess a cache directory.

    L1: the decision is made in main() — a non-zero exit and a stderr reason.
    The existing test stopped at _current_script() and never verified main()
    does anything with that None (D2).
    """
    _install(tmp_path, "0.8.0")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert _launcher.main() == 1
    captured = capsys.readouterr()
    assert "cannot identify exactly one current voice-loop install" in captured.err


def test_ambiguous_registry_fails_closed(tmp_path, monkeypatch, capsys):
    """Gap: two installed candidates must not send a hotkey to an arbitrary version.

    L1: the decision is made in main() — a non-zero exit and a stderr reason.
    The existing test stopped at _current_script() and never verified main()
    does anything with that None (D2).
    """
    first = _install(tmp_path, "0.8.0")
    second = _install(tmp_path, "0.9.0")
    _write_registry(
        tmp_path,
        [{"installPath": str(first)}, {"installPath": str(second)}],
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert _launcher.main() == 1
    captured = capsys.readouterr()
    assert "cannot identify exactly one current voice-loop install" in captured.err


def test_main_execs_current_script_with_hotkey_arguments(tmp_path, monkeypatch, capsys):
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

    assert _launcher.main() == 1  # D2: OSError in execv must return non-zero
    captured = capsys.readouterr()
    assert "exec failed" in captured.err
    assert calls == [(str(current / "scripts" / "dictate-toggle.sh"), [str(current / "scripts" / "dictate-toggle.sh"), "send"])]


def test_main_on_windows_spawns_subprocess_and_returns_child_exit(tmp_path, monkeypatch):
    """Gap: native Windows (`os.name == "nt"`) has no `os.execv`. The same launcher must reach the
    child, propagate its exit code, and never re-enter the POSIX branch — without this branch, a
    Windows hotkey fires the launcher and fails with `OSError: [Errno 22]` from `os.execv` every
    time, the dictation equivalent of #151 (a path that broke while manual invocation kept
    working, except here manual invocation never worked at all).

    `_current_script` is mocked to a sentinel Path so the test never constructs ``Path()`` while
    ``os.name`` is patched to ``"nt"`` — the stdlib `Path()` factory follows `os.name` and would
    return WindowsPath, which `__init__` raises on Linux."""
    fake_script = Path("/fake/install/scripts/dictate-toggle.sh")
    monkeypatch.setattr(_launcher, "_current_script", lambda: fake_script)
    monkeypatch.setattr(_launcher.os, "name", "nt")
    monkeypatch.setattr(_launcher.sys, "argv", ["launcher", "send"])
    execv_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_launcher.os, "execv", lambda path, argv: execv_calls.append((path, argv)))

    spawn_calls: list[list[str]] = []

    class Done:
        returncode = 7

    def fake_run(argv, **kw):
        spawn_calls.append(argv)
        assert kw["check"] is False
        return Done()

    monkeypatch.setattr(_launcher.subprocess, "run", fake_run)

    assert _launcher.main() == 7  # the child's exit code, surfaced to the caller
    assert execv_calls == [], "Windows must not invoke os.execv (it raises OSError there)"
    assert spawn_calls == [
        [str(fake_script), "send"]
    ], "argv reaches the child verbatim, including the user's hotkey arguments"


def test_main_on_windows_returns_nonzero_when_subprocess_spawn_fails(tmp_path, monkeypatch, capsys):
    """A Windows-side spawn failure (file not found, permission denied) must surface as a
    non-zero exit and an informative stderr message — the silent-launcher failure shape would
    hide the broken hotkey behind a hotkey that appears to do nothing."""
    fake_script = Path("/fake/install/scripts/dictate-toggle.sh")
    monkeypatch.setattr(_launcher, "_current_script", lambda: fake_script)
    monkeypatch.setattr(_launcher.os, "name", "nt")
    monkeypatch.setattr(_launcher.sys, "argv", ["launcher", "send"])

    def fake_run(argv, **kw):
        raise OSError("the script does not exist")

    monkeypatch.setattr(_launcher.subprocess, "run", fake_run)

    assert _launcher.main() == 1
    captured = capsys.readouterr()
    assert "spawn failed" in captured.err
