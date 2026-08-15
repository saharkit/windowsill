"""The selftest.sh helpers that have to survive a fresh WSL2 distro: the python3-backed config
parse that replaces the old ``jq`` dependency (#179, defect 1).

The full loopback (TTS -> STT -> compare) is proven by the real-invocation ``loopback`` job in
``.github/workflows/selftest.yml`` against a live server — that test alone does NOT exercise the
config-driven path, because the loopback lanes always pass ``--endpoint`` (the WSL2 pass for #41
could only be made green the same way, which is how #179 came back as a follow-up). What this file
pins is the parse step the loopback lanes kept bypassing: with no ``jq`` on PATH and a config file,
the script must reach the TTS call against the configured endpoint, never the default loopback.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[2]
_SELFTEST = _PLUGINS / "voice-loop" / "scripts" / "selftest.sh"


# The tools selftest.sh reaches for, in PATH lookup order. Every one of them lives on a directory
# that also holds jq on this system, so a private bin — built fresh per test — is what actually
# proves "no jq available": the test PATH excludes every directory that holds jq, and the script
# runs against symlinks whose dir does not.
_TOOLS_FOR_SHELL = (
    "bash",
    "python3",
    "curl",
    "mktemp",
    "cat",
    "head",
    "wc",
    "printf",
    "touch",
    "rm",
    "dirname",
    "basename",
    "sh",
    "command",  # bash builtin lookup uses a different path, but keep for safety
)


def _real_bash() -> str:
    """Find an actual POSIX bash, not Windows' WSL dispatch stub."""
    candidates: list[str] = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    if os.name == "nt":
        for root in (
            os.environ.get("ProgramFiles", r"C:\\Program Files"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if root:
                candidates.extend([
                    str(Path(root) / "Git" / "bin" / "bash.exe"),
                    str(Path(root) / "Git" / "usr" / "bin" / "bash.exe"),
                ])
    for candidate in dict.fromkeys(candidates):
        try:
            probe = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True, timeout=3
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "GNU bash" in (probe.stdout + probe.stderr):
            return candidate
    pytest.skip("no POSIX shell found")


def _build_jqless_bin(tmp_path: Path, bash: str) -> str:
    """A private /tmp bin directory whose links point at the real tools but whose DIRECTORY
    contains no ``jq`` binary. The test PATH is set to just this directory so the script's own
    interpreter/parser/toolchain is reachable while ``command -v jq`` — the very check the old
    selftest.sh used — answers no.

    The links are COPIES on Windows and symlinks elsewhere: creating a symlink there needs a
    privilege the runner does not grant, and a failed link reads as "tool absent" to the script's
    ``command -v`` guard — an exit 1 that says nothing about the parse step under test.
    """
    bin_dir = tmp_path / "jqless-bin"
    bin_dir.mkdir()
    for name in _TOOLS_FOR_SHELL:
        resolved = bash if name == "bash" else shutil.which(name)
        if not resolved:
            continue
        target = bin_dir / ("bash.exe" if name == "bash" and os.name == "nt" else name)
        try:
            if os.name == "nt":
                shutil.copy2(resolved, target)
            else:
                target.symlink_to(resolved)
        except (FileExistsError, OSError):
            continue
    if not (bin_dir / "bash").exists() or not (bin_dir / "python3").exists():
        pytest.skip("bash/python3 not findable on this host — cannot simulate a fresh WSL2 distro")
    # curl is the other tool the script hard-requires before it can report its no-config exit;
    # a host without it cannot reach the code under test, whichever platform that is.
    if not (bin_dir / "curl").exists():
        pytest.skip("curl not findable on this host — selftest.sh refuses to run before the parse step")
    return str(bin_dir)


@pytest.mark.skipif(not _SELFTEST.is_file(), reason="selftest.sh not present in this checkout")
def test_selftest_parses_config_via_python3_not_jq(tmp_path):
    """L2: with no jq available, the cfg function must still read the configured endpoint and
    the script must reach the TTS step against it.

    Catches: a regression that re-adds the old `jq -r` call, or that mangles the python heredoc
    enough that the parse step exits 2 before TTS. The "1/3 synthesizing via <URL>" line is the
    canonical signal that ``cfg .tts.endpoint`` returned the config-supplied URL.
    """
    bash = _real_bash()
    jqless_bin = _build_jqless_bin(tmp_path, bash)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "tts": {"endpoint": "http://192.0.2.42:8355"},
                "stt": {"endpoint": "http://192.0.2.42:8355"},
            }
        ),
        encoding="utf-8",
    )
    # Reserved, unrouteable address: the curl call inside selftest.sh will hang on it (the
    # script's curl uses a 180 s --max-time). We deliberately do NOT want to wait for it: the
    # parse step is the thing under test, and it has already finished by the time "1/3
    # synthesizing via <URL>" is printed. start_new_session=True puts the script in its own
    # process group so killpg stops the curl that comes next.
    proc = subprocess.Popen(
        [bash, str(_SELFTEST)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env={
            "PATH": jqless_bin,
            "HOME": str(tmp_path),
            "VOICE_LOOP_CONFIG": str(config),
        },
    )
    captured: list[str] = []
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            captured.append(line)
            if "synthesizing via " in line:
                break  # the parse step finished; assert and reap
            if proc.poll() is not None:
                # script exited (parse-step failure or other error) — capture tail and stop
                tail = proc.stdout.read()
                if tail:
                    captured.append(tail)
                break
    finally:
        # stop the curl the script is about to hang on. Windows has no process groups, so there
        # killpg does not exist and the direct kill is the only way to reach the child.
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, 9)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    combined = "".join(captured)
    # The parse step is the thing we are pinning: it has to read the config and reach the TTS
    # call. Before the python3 path, the script printed "config ignored" and tried the default
    # loopback — neither would land us on http://192.0.2.42:8355.
    assert "192.0.2.42:8355" in combined, (
        f"selftest.sh did not reach the configured endpoint — parse step failed.\n"
        f"output: {combined[:600]!r}"
    )
    assert "No speech endpoint configured" not in combined, (
        f"selftest.sh treated the config as absent — the python3 parse path regressed.\n"
        f"output: {combined[:600]!r}"
    )


@pytest.mark.skipif(not _SELFTEST.is_file(), reason="selftest.sh not present in this checkout")
def test_selftest_no_config_no_jq_exits_2_with_a_how_to(tmp_path):
    """L3: the no-config, no-jq fallback is still the same diagnostic — the parse path is
    not silently swallowed when both inputs are missing.

    Two-way falsification: against the OLD jq path this test would print "config ignored" to
    stderr AND exit 2 — the obsolete warning is exactly what #179 retired.
    """
    bash = _real_bash()
    jqless_bin = _build_jqless_bin(tmp_path, bash)
    result = subprocess.run(
        [bash, str(_SELFTEST)],
        capture_output=True,
        text=True,
        timeout=5,
        env={
            "PATH": jqless_bin,
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "empty-cfg-home"),
        },
    )
    assert result.returncode == 2, (
        f"expected exit 2 when no config and no backend; got {result.returncode}\n"
        f"stdout: {result.stdout[:400]!r}\nstderr: {result.stderr[:400]!r}"
    )
    assert "No speech endpoint configured" in result.stdout
    # And the obsolete "jq not found ... is ignored" warning is gone — the python3 path reads the
    # file when present, and on a fresh distro where it is absent, that absence is the documented
    # answer (#179 owns the retirement of this diagnostic, because fresh distros never had jq).
    assert "jq not found" not in result.stderr
    assert "is ignored" not in result.stderr
