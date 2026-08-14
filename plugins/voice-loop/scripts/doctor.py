#!/usr/bin/env python3
"""/doctor — three-bin diagnosis: collect the raw data and run the engine.

This is a thin CLI that reads voice-loop's config, install ledger, and log
tails, then hands them to the plugin-agnostic diagnosis engine in sill-core
together with the voice-loop check manifest.  The output is one JSON object to
stdout — the skill reads it and presents the findings.

Usage:
  python3 doctor.py [--state-home ~/.local/state] [--config-path ~/.config/voice-loop/config.json]

Stdlib only, Python 3.10+.  The engine lives in sill-core (a separate plugin);
this script is the voice-loop WIRING — it knows where the files are and how to
read them, but it knows nothing about what a diagnosis finding MEANS.  That is
all in the manifest at ``skills/doctor/check_manifest.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Callable


def _default_state_home() -> str:
    return os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))


def _default_config_path() -> str:
    return os.environ.get(
        "VOICE_LOOP_CONFIG",
        os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
            "voice-loop",
            "config.json",
        ),
    )


def _plugin_root() -> Path:
    """The voice-loop plugin root — this file's grandparent directory."""
    return Path(__file__).resolve().parent.parent


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string like ``'0.1.0'`` into a comparable tuple.

    Returns an empty tuple for unparseable strings so they sort lowest.
    """
    try:
        return tuple(int(part) for part in version_str.split("."))
    except (ValueError, TypeError):
        return ()


def _is_version_like(name: str) -> bool:
    """True when *name* looks like a version directory (e.g. ``'0.1.0'``, ``'1.2'``)."""
    parts = name.split(".")
    if len(parts) < 2:
        return False
    return all(part.isdigit() for part in parts)


def _resolve_version_dir(plugin_dir: Path) -> Path:
    """Resolve the code directory inside a plugin directory.

    In the cache layout, the version directory (e.g. ``'0.1.0'``) sits inside
    the plugin directory and is what should go on ``sys.path``.  When several
    versions are present, the highest one wins.

    In the checkout layout, the plugin directory itself is the code root — no
    version directory sits inside it.
    """
    try:
        entries = list(plugin_dir.iterdir())
    except OSError:
        return plugin_dir

    version_dirs = [
        entry for entry in entries
        if entry.is_dir() and _is_version_like(entry.name)
    ]

    if version_dirs:
        return max(version_dirs, key=lambda d: _parse_version(d.name))

    return plugin_dir


def _sill_core_root() -> Path:
    """The sill-core plugin root, alongside voice-loop.

    Account for two layouts:

    * **checkout** — ``plugins/voice-loop/`` and ``plugins/sill-core/``
      sit side by side; no version directory separates them.
    * **cache** — ``cache/<marketplace>/voice-loop/<ver>/`` and
      ``cache/<marketplace>/sill-core/<ver>/``; the version directory
      is one level down and must be resolved.

    When multiple versions exist, the highest one wins.  Raises
    :class:`FileNotFoundError` when sill-core is absent entirely.
    """
    our_parent = _plugin_root().parent  # marketplace / plugins/

    # Try sibling first (checkout — both plugins under one directory).
    candidates: list[Path] = [our_parent / "sill-core"]

    # Also try one level above (cache — voice-loop/<ver>/ puts us one deeper
    # than the marketplace level, while sill-core is at the marketplace level).
    candidates.append(our_parent.parent / "sill-core")

    for candidate in candidates:
        if candidate.is_dir():
            return _resolve_version_dir(candidate)

    raise FileNotFoundError(
        "sill-core plugin not found alongside voice-loop"
        f" (checked {', '.join(str(c) for c in candidates)})"
    )


def _remove_incident_symlink() -> None:
    """Delete the 2026-08-10 incident workaround symlink if it still exists.

    Before the fix, :func:`_sill_core_root` pointed at
    ``voice-loop/sill-core`` — one level too deep.  An incident symlink was
    placed there to keep the doctor running.  It is now dead code and must
    not survive a ``/plugin update``.
    """
    wrong_path = _plugin_root().parent / "sill-core"
    try:
        if wrong_path.is_symlink():
            wrong_path.unlink()
    except OSError:
        pass  # best-effort — the fix itself does not depend on this succeeding


def _is_store_stub(path: str | None) -> bool:
    """True if *path* points at the Microsoft Store Python stub.

    A real Python interpreter lives somewhere the user installed it
    (typically ``Program Files`` or a user-chosen directory).  The Store
    stub lives under
    ``C:\\Users\\<user>\\AppData\\Local\\Microsoft\\WindowsApps\\`` — a
    real ``.exe`` that opens the Store instead of running Python.

    **Why a presence test is not enough.**  Every launcher in voice-loop
    calls ``python3``, but the python.org Windows installer ships
    ``python.exe`` and NOT ``python3.exe``.  When the only ``python3`` on
    PATH is the Store stub, ``shutil.which("python3")`` returns a path
    WITHOUT raising — the user sees no error at resolution time, but
    every invocation silently opens the Store.  This is the trap that
    cost ticket #174 its discovery: a check that only asked "is
    ``python3`` on PATH?" answered YES on the redirect.  The path check
    distinguishes the real interpreter from the stub.

    **Why we anchor on the parent directory NAME, not a substring.**
    The real shape is a file living INSIDE the ``WindowsApps``
    directory; ``PureWindowsPath(path).parent.name`` is that one
    component.  A substring scan of the whole string would flag any
    path that happens to contain ``windowsapps`` anywhere along it —
    e.g. ``C:\\myapp\\subdir\\windowsapps-tools\\python.exe`` — and
    tell a user with a real interpreter to install one they already
    have.  ``PureWindowsPath`` (rather than ``Path``) is used so the
    split on backslashes works under the Linux test runner too.
    """
    if path is None:
        return False
    # Resolve any symlink aliases so a redirected stub is still
    # recognised.  ``os.path.realpath`` does not require the file to
    # exist; the Store stub itself is a launcher EXE, not a symlink, but
    # resolving aliases catches user-installed wrappers too.
    try:
        resolved = os.path.realpath(path)
    except OSError:
        resolved = path
    return PureWindowsPath(resolved).parent.name.lower() == "windowsapps"


def _check_python_interpreter(
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any] | None:
    """Return a Windows prerequisite finding for the Python interpreter.

    Two distinct failure modes, two distinct findings:

    1. ``python3`` IS the Microsoft Store stub — fires when a
       ``python3.exe`` is on PATH but resolves to the Store redirect.
       A mere presence test answers YES on this trap; the launchers
       all call ``python3`` and will silently fail.
    2. No real Python interpreter at all — fires when NEITHER
       ``python3`` NOR ``python`` resolves to a real interpreter
       (either absent or another Store stub).

    Returns ``None`` on Linux/macOS (the check is inert) and when both
    ``python3`` and ``python`` resolve to a real interpreter.

    The probe seams are keyword-only with production defaults
    (``sys.platform`` and ``shutil.which``), so the test can inject a
    fixed platform and a fake ``which`` without monkeypatching the
    module — the same shape the neighbouring ``_sill_core_root`` seam
    uses for path resolution.
    """
    if platform != "win32":
        return None

    python3_path = which("python3")
    python_path = which("python")

    # Decision 1: python3 is the Store stub.  Fires first because the
    # launchers all call ``python3``; a real ``python`` elsewhere does
    # not save the launchers.
    if python3_path is not None and _is_store_stub(python3_path):
        # NOTE: the raw resolved path is NOT included in evidence.  The
        # Store stub lives under
        # ``C:\Users\<account>\AppData\Local\Microsoft\WindowsApps\``,
        # so embedding the full path would carry the OS account name
        # into the bundle, and /doctor's stdout is consumed by the
        # /report-bug handoff which files a public GitHub issue.
        # The diagnosis ("python3 IS the Store stub") is the whole
        # signal the user needs; one boolean captures it without
        # identity.
        return {
            "bin": "real_anomaly",
            "key": "python3_is_store_stub",
            "title": (
                "`python3` is the Microsoft Store stub, not a real interpreter"
            ),
            "explanation": (
                "Every launcher in voice-loop calls `python3`, but the "
                "python.org Windows installer ships `python.exe` and NOT "
                "`python3.exe`.  When `python3` is on PATH but its file "
                "lives under `WindowsApps`, it is the Microsoft Store "
                "stub — a real executable that opens the Store instead "
                "of running Python.  A presence test for `python3` would "
                "ANSWER YES on this trap."
            ),
            "fix": (
                "Install Python from python.org "
                "(https://www.python.org/downloads/windows/) and tick "
                "'Add Python to PATH' during install.  If you also need "
                "`python3.exe` to work, install the Python Launcher for "
                "Windows (also at python.org) — it ships a real "
                "`python.exe` and registers it on PATH."
            ),
            "offer_flip": False,
            "flip_path": "",
            "flip_value": None,
            "evidence": {"python3_is_stub": True},
        }

    # Decision 2: no real interpreter.  "Real" means: present AND not
    # the Store stub.  If either ``python3`` or ``python`` is real, the
    # launchers have a path to a working interpreter.
    has_real = (
        (python3_path is not None and not _is_store_stub(python3_path))
        or (python_path is not None and not _is_store_stub(python_path))
    )
    if not has_real:
        # NOTE: the raw resolved paths are NOT included in evidence.
        # Either lookup (or both) resolves under
        # ``C:\Users\<account>\AppData\Local\Microsoft\WindowsApps\``,
        # so embedding the full paths would carry the OS account name
        # into the bundle, and /doctor's stdout is consumed by the
        # /report-bug handoff which files a public GitHub issue.
        # The diagnosis ("no real interpreter on PATH") is what the
        # user needs; two booleans — one per lookup — capture which
        # case they hit (absent entirely, only-stub, both-stub) without
        # identity.
        return {
            "bin": "real_anomaly",
            "key": "python_interpreter_missing",
            "title": "No Python interpreter is installed",
            "explanation": (
                "voice-loop launches scripts with `python3`; on Windows "
                "the python.org installer ships `python` (not `python3`).  "
                "A user with no Python install at all — or only the "
                "Microsoft Store stub — sees cryptic errors when scripts "
                "try to execute."
            ),
            "fix": (
                "Install Python from python.org "
                "(https://www.python.org/downloads/windows/) and tick "
                "'Add Python to PATH' during install.  The Microsoft "
                "Store Python launcher will not work because the stub "
                "does not vend `python3`."
            ),
            "offer_flip": False,
            "flip_path": "",
            "flip_value": None,
            "evidence": {
                "python3_is_stub": python3_path is not None and _is_store_stub(python3_path),
                "python_is_stub": python_path is not None and _is_store_stub(python_path),
            },
        }

    return None


def read_config(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def read_ledger(state_home: str) -> dict[str, Any]:
    path = os.path.join(state_home, "voice-loop", "install.ledger")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"state": "none", "completed_steps": []}

    ledger: dict[str, Any] = {}
    ledger["state"] = raw.get("state", "none")
    steps = raw.get("steps", {})
    ledger["completed_steps"] = [
        sid for sid, entry in steps.items() if entry.get("status") == "complete"
    ]
    in_flight = [
        sid for sid, entry in steps.items() if entry.get("status") == "in_progress"
    ]
    if in_flight:
        ledger["current_step"] = in_flight[0]
    return ledger


def read_logs(state_home: str, tail_lines: int = 60) -> dict[str, list[str]]:
    logs: dict[str, list[str]] = {}
    state_dir = os.path.join(state_home, "voice-loop")
    for log_name in ("speak.log", "dictate.log"):
        path = os.path.join(state_dir, log_name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            logs[log_name] = lines[-tail_lines:]
        except OSError:
            logs[log_name] = []
    return logs


def load_manifest(plugin_root: Path) -> list[dict[str, Any]]:
    import importlib.util

    manifest_path = plugin_root / "skills" / "doctor" / "check_manifest.py"
    spec = importlib.util.spec_from_file_location("check_manifest", str(manifest_path))
    if spec is None:
        raise FileNotFoundError(f"manifest not found at {manifest_path}")
    mod = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"manifest loader is None for {manifest_path}")
    spec.loader.exec_module(mod)
    return mod.CHECK_MANIFEST


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv

    state_home = _default_state_home()
    config_path = _default_config_path()

    # Rudimentary arg parsing — just enough for the skill to override defaults.
    args = list(argv[1:])
    while args:
        if args[0] == "--state-home" and len(args) > 1:
            state_home = args[1]
            args = args[2:]
        elif args[0] == "--config-path" and len(args) > 1:
            config_path = args[1]
            args = args[2:]
        elif args[0] in ("-h", "--help"):
            print(f"usage: {argv[0]} [--state-home DIR] [--config-path FILE]", file=sys.stderr)
            return 0
        else:
            print(f"unknown argument: {args[0]}", file=sys.stderr)
            return 2

    # Platform prerequisite checks (Windows-only, inert on Linux/macOS).
    # These run BEFORE the data sources are read because they are about
    # the runtime environment, not the user's config/ledger/logs.  They
    # are merged into every output path so the user always sees them
    # when present.
    platform_findings: list[dict[str, Any]] = []
    py_finding = _check_python_interpreter()
    if py_finding is not None:
        platform_findings.append(py_finding)

    # Read the raw data.
    config = read_config(config_path)
    ledger = read_ledger(state_home)
    logs = read_logs(state_home)

    # Load the manifest and the engine.
    root = _plugin_root()
    manifest = load_manifest(root)

    try:
        core_root = str(_sill_core_root())
    except FileNotFoundError:
        # A diagnostic tool must never be unable to report itself.
        # When sill-core is absent, emit the diagnosis rather than crashing.
        output: dict[str, Any] = {
            "config_present": bool(config),
            "config_path": config_path,
            "ledger_state": ledger.get("state", "none"),
            "findings": [
                {
                    "bin": "real_anomaly",
                    "key": "sill_core_missing",
                    "title": "The diagnosis engine (sill-core) is not installed",
                    "explanation": (
                        "sill-core, the shared plugin that provides the diagnosis "
                        "engine, was not found alongside voice-loop.  Without it, "
                        "/doctor cannot run checks — the diagnosis tool itself "
                        "cannot diagnose anything."
                    ),
                    "fix": (
                        "Install sill-core alongside voice-loop: run "
                        "`/plugin install sill-core@windowsill` in Claude Code, "
                        "then run `/voice-loop:doctor` again."
                    ),
                    "offer_flip": False,
                    "flip_path": "",
                    "flip_value": None,
                    "evidence": {},
                },
                *platform_findings,
            ],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0

    _remove_incident_symlink()

    if core_root not in sys.path:
        sys.path.insert(0, core_root)

    try:
        from sill_core.diagnosis import Bin, diagnose  # noqa: E402
    except ImportError:
        output: dict[str, Any] = {
            "config_present": bool(config),
            "config_path": config_path,
            "ledger_state": ledger.get("state", "none"),
            "findings": [
                {
                    "bin": "real_anomaly",
                    "key": "sill_core_import_failed",
                    "title": "The diagnosis engine (sill-core) failed to import",
                    "explanation": (
                        "sill-core was found on disk, but its diagnosis module "
                        "could not be imported — the plugin may be damaged or "
                        "from an incompatible version."
                    ),
                    "fix": (
                        "Reinstall sill-core: run "
                        "`/plugin install sill-core@windowsill` in Claude Code, "
                        "then run `/voice-loop:doctor` again."
                    ),
                    "offer_flip": False,
                    "flip_path": "",
                    "flip_value": None,
                    "evidence": {},
                },
                *platform_findings,
            ],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0

    findings = diagnose(manifest=manifest, config=config, ledger=ledger, logs=logs)

    # Emit structured JSON.
    output: dict[str, Any] = {
        "config_present": bool(config),
        "config_path": config_path,
        "ledger_state": ledger.get("state", "none"),
        "findings": [
            {
                "bin": f.bin.value,
                "key": f.key,
                "title": f.title,
                "explanation": f.explanation,
                "fix": f.fix,
                "offer_flip": f.offer_flip,
                "flip_path": f.flip_path,
                "flip_value": f.flip_value,
                "evidence": f.evidence,
            }
            for f in findings
        ] + platform_findings,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
