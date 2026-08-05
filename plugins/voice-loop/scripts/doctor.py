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


def _sill_core_root() -> Path:
    """The sill-core plugin root, alongside voice-loop."""
    return _plugin_root().parent / "sill-core"


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

    core_root = str(_sill_core_root())
    if core_root not in sys.path:
        sys.path.insert(0, core_root)

    from sill_core.diagnosis import Bin, diagnose  # noqa: E402

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
