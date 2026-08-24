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
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

import contracts


class DiagnosticData(dict):
    """Mapping data with a read verdict kept outside the payload."""

    def __init__(self, data: dict[str, Any], *, status: str, detail: str = "") -> None:
        super().__init__(data)
        self.status = status
        self.detail = detail


def _default_state_home() -> str:
    return os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))


def _default_config_path() -> str:
    return contracts.config_path()


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


def _redact_windows_path(path: str) -> str:
    """Hide the Windows account name while retaining the useful path shape."""
    parts = PureWindowsPath(path).parts
    profile = os.environ.get("USERPROFILE")
    if not profile:
        drive = os.environ.get("HOMEDRIVE", "")
        home = os.environ.get("HOMEPATH", "")
        profile = f"{drive}{home}" or None

    if profile:  # pragma: windows-only - Windows-only profile redaction; the coverage job runs on Ubuntu
        # Windows-only profile-path redaction; the coverage job runs on Ubuntu.
        profile_parts = PureWindowsPath(profile).parts
        profile_length = len(profile_parts)
        for start in range(len(parts) - profile_length + 1):
            candidate = parts[start:start + profile_length]
            if all(
                actual.casefold() == expected.casefold()
                for actual, expected in zip(candidate, profile_parts)
            ):
                redacted = list(parts)
                redacted[start + profile_length - 1] = "<user>"
                return str(PureWindowsPath(*redacted))

    # Keep the legacy shape as a fallback when the profile environment is not
    # available (and for callers that provide a path from a different profile).
    try:
        users_index = next(
            index for index, part in enumerate(parts[:-1])
            if part.lower() == "users"
        )
    except StopIteration:
        return path
    if users_index + 1 >= len(parts) - 1:
        return path
    redacted = list(parts)
    redacted[users_index + 1] = "<user>"
    return str(PureWindowsPath(*redacted))


def _decode_windows_output(output: Any) -> str:
    """Decode bytes emitted by Windows tools without propagating decode errors."""
    if isinstance(output, str):
        return output
    if not output:
        return ""
    if isinstance(output, bytes):
        # wsl.exe commonly emits UTF-16LE to a pipe.  Only try that codec when
        # a BOM or NUL bytes identify the stream as UTF-16; where.exe normally
        # emits UTF-8/ANSI text without either marker.
        # Windows-only wsl.exe UTF-16 output; the coverage job runs on Ubuntu.
        if output.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in output:  # pragma: windows-only - Windows-only wsl.exe UTF-16 decoding; the coverage job runs on Ubuntu
            try:  # Windows-only wsl.exe decoding; the coverage job runs on Ubuntu.
                return output.decode("utf-16le").lstrip("﻿")
            except UnicodeDecodeError:
                pass
        try:
            return output.decode("utf-8")
        except UnicodeDecodeError:
            return output.decode("utf-8", errors="replace")
    return str(output)


def _where_python3(
    *,
    platform: str = sys.platform,
    run: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Return every ``python3`` resolution reported by Windows ``where.exe``."""
    if platform != "win32":
        return []
    try:
        result = run(
            ["where.exe", "python3"],
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    output = _decode_windows_output(result.stdout)
    return [line.strip() for line in output.splitlines() if line.strip()]


# Docker Desktop for Windows registers two WSL distros on every machine
# that runs it.  Nothing is ever installed in them by hand, so they are not
# evidence that a voice-loop contour could live in WSL.
_WSL_DOCKER_DISTROS = frozenset({
    "docker-desktop",
    "docker-desktop-data",
})

# The registry key wsl.exe itself enumerates from: one GUID subkey per
# registered distro, each carrying a ``DistributionName`` value.  Per-user
# registrations sit under HKCU; all-users registrations (newer WSL) under
# HKLM.  Reading it needs no privileges.
_WSL_LXSS_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Lxss"


def _registered_wsl_distros() -> tuple[list[str], int]:  # pragma: windows-only - Windows-only winreg WSL scan; the coverage job runs on Ubuntu
    """Enumerate registered WSL distro names straight from the registry.

    Returns ``(names, docker_excluded)`` where *names* excludes the Docker
    Desktop distros.  This is the source of truth ``wsl.exe -l`` reports
    from, read directly — which matters for two reasons:

    * on some builds, ``wsl.exe -l -q`` prints the localized "no installed
      distributions" banner to STDOUT with exit code 0, and one banner line
      would otherwise count as one distro; the registry has no banner and
      no localization to be wrong about;
    * a STOPPED distro is registered exactly like a running one, so a
      contour in a distro that is not running right now still counts —
      the registration, not the running state, is the signal.

    An unreadable or absent key means "no distros we can vouch for", which
    suppresses the finding rather than risking a false positive.
    """
    # Windows-only registry scan; the coverage job runs on Ubuntu.
    try:
        import winreg
    except ImportError:
        return [], 0

    roots = (
        (winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ),
    )
    names: list[str] = []
    docker_excluded = 0
    for root, access in roots:
        try:
            lxss = winreg.OpenKey(root, _WSL_LXSS_SUBKEY, 0, access)
        except OSError:
            continue
        with lxss:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(lxss, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(lxss, subkey_name, 0, access) as sub:
                        name, _ = winreg.QueryValueEx(sub, "DistributionName")
                except OSError:
                    continue
                if not isinstance(name, str) or not name:
                    continue
                if name.casefold() in _WSL_DOCKER_DISTROS:
                    docker_excluded += 1
                elif name not in names:
                    names.append(name)
    return names, docker_excluded


def _wsl_boundary_finding(
    *,
    platform: str = sys.platform,
    distros_probe: Callable[[], tuple[list[str], int]] = _registered_wsl_distros,
) -> dict[str, Any] | None:
    """Explain an empty Windows-side contour when WSL has a distro.

    The probe knows only that a usable (non-Docker) WSL distro is
    REGISTERED — not that a voice-loop contour lives in it — so the finding
    is worded as a conditional, never as an assertion that the contour is
    there.  A distro counts whether or not it is currently running.
    """
    if platform != "win32":
        return None
    distros, docker_excluded = distros_probe()
    if not distros:
        return None
    return {
        "bin": "consequence_of_choice",
        "key": "wsl_contour_not_visible_from_windows",
        "title": (
            "A WSL distro is registered, but no voice-loop contour is "
            "visible on Windows"
        ),
        "explanation": (
            "This Windows-side `/doctor` found no config or install ledger, "
            "but WSL has at least one registered distro (a stopped distro "
            "counts too).  This probe cannot see inside the distro, so it "
            "cannot tell whether voice-loop is installed there — if it is, "
            "its config and ledger live in the distro filesystem and are "
            "invisible from Windows; if it is not, there is nothing there."
        ),
        "fix": (
            "If you installed voice-loop inside a WSL distro, run `/doctor` "
            "from inside that distro.  If you never did, there is nothing in "
            "WSL — continue with setup on the Windows side."
        ),
        "offer_flip": False,
        "flip_path": "",
        "flip_value": None,
        "evidence": {
            "wsl_distro_present": True,
            "wsl_distro_count": len(distros),
            "wsl_docker_distros_excluded": docker_excluded,
        },
    }


def _check_python_interpreter(
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    where: Callable[[], list[str]] | None = None,
) -> dict[str, Any] | None:
    """Return a Windows prerequisite finding for the Python interpreter.

    Three Windows prerequisite outcomes produce findings or pass:

    1. A ``python3.exe`` under ``WindowsApps`` is on PATH — fires even when
       a real interpreter currently wins, because PATH ordering can change.
    2. ``python3`` resolves first to the Microsoft Store stub — the launchers
       all call ``python3`` and will silently fail.
    3. No real Python interpreter at all — fires when NEITHER ``python3`` NOR
       ``python`` resolves to a real interpreter (either absent or another
       Store stub).
    4. A real interpreter is installed but only as ``python`` — python.org
       ships no ``python3.exe``, so nothing answers to ``python3`` and every
       launcher's ``command -v python3`` guard exits silently.

    Returns ``None`` on Linux/macOS (the check is inert) and when no Store
    stub is present, a real ``python3`` is resolvable, and the interpreter
    answers to the name the launchers call.

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

    # ``which`` reports only the winner.  ``where.exe`` exposes every
    # candidate, which matters because a Store stub later on PATH can win
    # after a PATH reorder or a fresh shell.  Keep the injected seam useful
    # to tests while using the real Windows probe in production.
    # ``which`` is the executable probe.  A ``where.exe`` candidate is useful only when that
    # probe already found a python3 executable: a failed/absent probe must not be reclassified as
    # a Store stub merely because ``where.exe`` printed an alias that cannot execute.
    python3_paths = list(where() if where is not None else _where_python3(platform=platform))
    if python3_path is not None and python3_path not in python3_paths:
        python3_paths.insert(0, python3_path)
    elif python3_path is None:
        python3_paths = []
    stub_paths = [path for path in python3_paths if _is_store_stub(path)]

    # Decision 1: any Store stub on PATH.  Fires even when a real interpreter
    # currently wins, because that ordering is not a safe configuration.
    if stub_paths:
        first_path = _redact_windows_path(python3_path or python3_paths[0])
        first_is_stub = _is_store_stub(python3_path)
        if first_is_stub:
            title = (
                f"`python3` resolves first to `{first_path}`, the Microsoft "
                "Store stub, not a real interpreter"
            )
            explanation = (
                f"`python3` resolves first to `{first_path}`.  Every launcher "
                "in voice-loop calls `python3`, but the python.org Windows "
                "installer ships `python.exe` and NOT `python3.exe`.  This "
                "`WindowsApps` executable is the Microsoft Store stub — a "
                "real executable that opens the Store instead of running "
                "Python."
            )
        else:
            title = (
                f"`python3` resolves first to `{first_path}`, but a "
                "Microsoft Store stub is also on PATH"
            )
            explanation = (
                f"`python3` currently resolves first to `{first_path}`, but "
                "another `python3.exe` lives under `WindowsApps`.  PATH "
                "ordering can change, so the Store stub may be selected by a "
                "new shell and silently open the Store instead of running "
                "voice-loop."
            )
        return {
            "bin": "real_anomaly",
            "key": "python3_is_store_stub",
            "title": title,
            "explanation": explanation,
            "fix": (
                "Remove or disable the Microsoft Store `python3.exe` App "
                "Execution Alias in Windows Settings, or remove its "
                "`WindowsApps` entry from PATH.  Ensure the real interpreter "
                "you want is first, then open a new shell and run "
                "`where.exe python3` to verify the ordering."
            ),
            "offer_flip": False,
            "flip_path": "",
            "flip_value": None,
            "evidence": {
                "python3_is_stub": first_is_stub,
                "python3_first_path": first_path,
                "python3_stub_paths": [
                    _redact_windows_path(path) for path in stub_paths
                ],
            },
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

    # Decision 3: a real interpreter exists but only as ``python``.
    # python.org ships no ``python3.exe``, so nothing answers to the one
    # name every launcher calls.  The launchers guard with
    # ``command -v python3`` and exit 0 on absence — speak-back and
    # dictation do NOTHING, with no error anywhere.  This is the most
    # invisible of the three prerequisites: the interpreter is installed
    # and healthy, so only the missing NAME is wrong.
    if python3_path is None or _is_store_stub(python3_path):
        first_python = (
            _redact_windows_path(python_path) if python_path is not None else None
        )
        return {
            "bin": "real_anomaly",
            "key": "python3_alias_missing",
            "title": (
                "Python is installed, but nothing on PATH answers to "
                "`python3`"
            ),
            "explanation": (
                "A real Python interpreter is installed (`python` resolves"
                f"{f' to `{first_python}`' if first_python else ''}), but "
                "the python.org Windows installer ships `python.exe` and "
                "NOT `python3.exe` — and every voice-loop launcher calls "
                "`python3`.  Nothing errors: each launcher checks "
                "`command -v python3`, finds nothing, and exits silently, "
                "so the plugin appears to do nothing at all (no spoken "
                "replies, no dictation)."
            ),
            "fix": (
                "Create a `python3` alias for the real interpreter: open "
                "the Python install directory (typically "
                "`C:\\Program Files\\Python312\\` or "
                "`C:\\Users\\<user>\\AppData\\Local\\Programs\\Python\\"
                "Python312\\`), copy `python.exe` to `python3.exe` in the "
                "same directory, then open a new shell and run "
                "`where.exe python3` — it must list that copy and no "
                "`WindowsApps` entry.  Alternatively `mklink "
                "python3.exe python.exe` from an administrator prompt in "
                "the same directory achieves the same without a second "
                "copy."
            ),
            "offer_flip": False,
            "flip_path": "",
            "flip_value": None,
            "evidence": {
                "python_resolves_real": python_path is not None
                and not _is_store_stub(python_path),
                "python3_absent": python3_path is None,
            },
        }

    return None


def read_config(path: str) -> DiagnosticData:
    try:
        loaded = contracts.read_config(path)
    except FileNotFoundError:
        return DiagnosticData({}, status="missing", detail=f"{path} does not exist")
    except OSError as err:
        return DiagnosticData({}, status="unreadable", detail=f"{type(err).__name__}: {err}")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as err:
        return DiagnosticData({}, status="malformed", detail=f"{type(err).__name__}: {err}")
    if not isinstance(loaded, dict):
        return DiagnosticData({}, status="malformed", detail=f"expected JSON object, got {type(loaded).__name__}")
    return DiagnosticData(loaded, status="ok")


def read_ledger(state_home: str) -> DiagnosticData:
    path = os.path.join(state_home, "voice-loop", "install.ledger")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return DiagnosticData({"state": "none", "completed_steps": []}, status="missing")
    except OSError as err:
        return DiagnosticData({"state": "none", "completed_steps": []}, status="unreadable", detail=f"{type(err).__name__}: {err}")
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return DiagnosticData({"state": "none", "completed_steps": []}, status="malformed", detail=f"{type(err).__name__}: {err}")
    if not isinstance(raw, dict) or not isinstance(raw.get("steps", {}), dict):
        return DiagnosticData({"state": "none", "completed_steps": []}, status="malformed", detail="ledger must be a JSON object with object steps")

    ledger: dict[str, Any] = {"state": raw.get("state", "none")}
    steps = raw.get("steps", {})
    if any(not isinstance(entry, dict) for entry in steps.values()):
        return DiagnosticData({"state": "none", "completed_steps": []}, status="malformed", detail="ledger step entries must be objects")
    ledger["completed_steps"] = [
        sid for sid, entry in steps.items() if entry.get("status") == "complete"
    ]
    in_flight = [
        sid for sid, entry in steps.items() if entry.get("status") == "in_progress"
    ]
    if in_flight:
        ledger["current_step"] = in_flight[0]
    return DiagnosticData(ledger, status="ok")


def read_logs(state_home: str, tail_lines: int = 60) -> DiagnosticData:
    logs: dict[str, list[str]] = {}
    statuses: dict[str, str] = {}
    details: dict[str, str] = {}
    state_dir = os.path.join(state_home, "voice-loop")
    for log_name in ("speak.log", "dictate.log"):
        path = os.path.join(state_dir, log_name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            logs[log_name] = lines[-tail_lines:]
            statuses[log_name] = "ok"
        except FileNotFoundError:
            logs[log_name] = []
            statuses[log_name] = "missing"
            continue
        except OSError as err:
            logs[log_name] = []
            statuses[log_name] = "unreadable"
            details[log_name] = f"{type(err).__name__}: {err}"
            continue

        try:
            with open(path, encoding="utf-8") as fh:
                fh.read()
        except UnicodeDecodeError as err:
            statuses[log_name] = "malformed"
            details[log_name] = f"{type(err).__name__}: {err}"
        except FileNotFoundError:
            statuses[log_name] = "missing"
            details[log_name] = f"{path} disappeared after reading"
        except OSError as err:
            statuses[log_name] = "unreadable"
            details[log_name] = f"{type(err).__name__}: {err}"
    result = DiagnosticData(logs, status="ok" if all(s == "ok" for s in statuses.values()) else "degraded")
    result.source_status = statuses
    result.source_details = details
    return result


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

    # A Windows-side empty contour may simply be the wrong side of the WSL2
    # boundary.  Do not probe WSL on POSIX, and do not override a local config
    # or ledger with a boundary warning.  When the boundary is present, the
    # empty ledger is expected to belong to WSL, so suppress its misleading
    # "run /voice-setup" findings below.
    wsl_finding: dict[str, Any] | None = None
    if not config and ledger.get("state", "none") == "none":
        wsl_finding = _wsl_boundary_finding()
        if wsl_finding is not None:
            platform_findings.append(wsl_finding)

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
            "config_status": getattr(config, "status", "unknown"),
            "config_read_detail": getattr(config, "detail", ""),
            "ledger_state": ledger.get("state", "none"),
            "ledger_status": getattr(ledger, "status", "unknown"),
            "ledger_read_detail": getattr(ledger, "detail", ""),
            "log_status": getattr(logs, "source_status", {}),
            "log_read_details": getattr(logs, "source_details", {}),
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
        return 1

    _remove_incident_symlink()

    if core_root not in sys.path:
        sys.path.insert(0, core_root)

    try:
        from sill_core.diagnosis import Bin, diagnose  # noqa: E402
    except ImportError:
        output: dict[str, Any] = {
            "config_present": bool(config),
            "config_path": config_path,
            "config_status": getattr(config, "status", "unknown"),
            "config_read_detail": getattr(config, "detail", ""),
            "ledger_state": ledger.get("state", "none"),
            "ledger_status": getattr(ledger, "status", "unknown"),
            "ledger_read_detail": getattr(ledger, "detail", ""),
            "log_status": getattr(logs, "source_status", {}),
            "log_read_details": getattr(logs, "source_details", {}),
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
        return 1

    findings = diagnose(manifest=manifest, config=config, ledger=ledger, logs=logs)
    if wsl_finding is not None:
        findings = [
            finding for finding in findings
            if finding.bin.value != "unfinished_install"
        ]

    # Emit structured JSON.
    output: dict[str, Any] = {
        "config_present": bool(config),
        "config_path": config_path,
        "config_status": getattr(config, "status", "ok"),
        "config_read_detail": getattr(config, "detail", ""),
        "ledger_state": ledger.get("state", "none"),
        "ledger_status": getattr(ledger, "status", "ok"),
        "ledger_read_detail": getattr(ledger, "detail", ""),
        "log_status": getattr(logs, "source_status", {}),
        "log_read_details": getattr(logs, "source_details", {}),
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
