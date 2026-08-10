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
import sys
from pathlib import Path
from typing import Any


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
                }
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
                }
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
        ],
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
