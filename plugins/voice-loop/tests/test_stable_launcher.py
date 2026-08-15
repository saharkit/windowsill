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
    (scripts / "dictate-toggle.cmd").write_text("@echo off\r\n", encoding="utf-8")
    return root


def _install_cmd(tmp_path: Path, version: str) -> Path:
    """An install carrying BOTH launcher leaves — the real checkout ships the ``.cmd`` beside the
    ``.sh``, so the leaf the resolver picks is a decision it makes, not an accident of which file
    happens to exist."""
    return _install(tmp_path, version)


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


def test_main_on_windows_spawns_a_real_child_and_propagates_its_exit_code(tmp_path, monkeypatch):
    """Native Windows (`os.name == "nt"`) has no `os.execv`, so main() must reach the child through
    subprocess.run and surface the child's own exit code. A replaced spawn function returns whatever
    the test told it to, so it can never prove the exit code actually came from the child; a real
    executable that records its own argv and exits 7 proves both halves at once — argv reaches it
    verbatim and its exit code is propagated to the caller.

    `os.name` is patched to "nt" so main() takes the subprocess branch; the child is a POSIX
    executable with a shebang, which exec runs the same way CreateProcess runs a `.cmd` shim on
    Windows — the platform difference this patch stands in for."""
    record = tmp_path / "argv.json"
    child = tmp_path / "dictate-toggle.cmd"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(record)!r}, 'w'))\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    monkeypatch.setattr(_launcher, "_current_script", lambda: child)
    monkeypatch.setattr(_launcher.os, "name", "nt")
    monkeypatch.setattr(_launcher.sys, "argv", ["launcher", "send"])
    execv_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(_launcher.os, "execv", lambda path, argv: execv_calls.append((path, argv)))

    assert _launcher.main() == 7  # the child's exit code, surfaced to the caller
    assert execv_calls == [], "Windows must not invoke os.execv (it raises OSError there)"
    assert json.loads(record.read_text(encoding="utf-8")) == ["send"]  # argv reached the child verbatim


def test_current_script_resolves_the_cmd_leaf_on_windows(tmp_path, monkeypatch):
    """The registry candidate under Windows must be the ``.cmd`` shim, not the ``.sh``:
    ``subprocess.run`` reaches the child through CreateProcess, which does not consult shell file
    associations — a ``.sh`` candidate is WinError 193 before argv matters, so a hardcoded POSIX
    leaf makes every durable Windows hotkey invocation fail. The platform is a parameter (not a
    patched ``os.name``) because ``Path()`` itself follows ``os.name`` and cannot be constructed
    as a WindowsPath on this Linux runner."""
    root = _install_cmd(tmp_path, "0.8.0")
    _write_registry(tmp_path, [{"installPath": str(root), "scope": "user"}])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert _launcher._current_script("nt") == root / "scripts" / "dictate-toggle.cmd"
    assert _launcher._current_script("posix") == root / "scripts" / "dictate-toggle.sh"


def test_main_on_windows_returns_nonzero_when_subprocess_spawn_fails(tmp_path, monkeypatch, capsys):
    """A Windows-side spawn failure (file not found, permission denied) must surface as a
    non-zero exit and an informative stderr message — the silent-launcher failure shape would
    hide the broken hotkey behind a hotkey that appears to do nothing."""
    fake_script = Path("/fake/install/scripts/dictate-toggle.cmd")
    monkeypatch.setattr(_launcher, "_current_script", lambda: fake_script)
    monkeypatch.setattr(_launcher.os, "name", "nt")
    monkeypatch.setattr(_launcher.sys, "argv", ["launcher", "send"])

    def fake_run(argv, **kw):
        raise OSError("the script does not exist")

    monkeypatch.setattr(_launcher.subprocess, "run", fake_run)

    assert _launcher.main() == 1
    captured = capsys.readouterr()
    assert "spawn failed" in captured.err
