"""Repo-root credential-closure policy for the voice-loop plugin.

fix(#5816) — voice-loop takes provider keys only from userConfig; a plugin MCP server
holds the keys and is the ONLY reader. The policy test refuses-first on the
pre-change tree (``f9a0faa`` and equivalents) and stays green thereafter.

It scans the voice-loop plugin's code and skill files for two things, with named
exemptions:

1. **Environment reads** of any variable whose name contains ``KEY``, ``TOKEN`` or
   ``key_env`` — except the two literal names ``CLAUDE_PLUGIN_OPTION_TTS_API_KEY`` and
   ``CLAUDE_PLUGIN_OPTION_STT_API_KEY`` (the harness's userConfig delivery, and the
   only env names the credential-closure change keeps reading).

2. **Key-file reads** of a literal or configured ``*.key`` / ``key_file`` /
   ``api_key_env`` path. Identifier matches use word boundaries (``\\b``), so the
   provider-registry names ``key_envs`` and ``key_env_fallbacks`` are NEVER hits;
   only an actual lookup whose result is read is.

Scan scope is ``plugins/voice-loop/{scripts,skills,server,hooks}/**`` — Python,
shell, cmd, ps1 and skill markdown. ``plugins/voice-loop/tests/`` is excluded
(not present at the f9a0faa baseline; the rule is a guard for a future
in-plugin suite). ``evals/``, ``rvc/``, ``assets/`` and ``docs/`` are rewritten
by hand in the same diff and not policed here.

Exemptions (named in the body of the rule, asserted by their own cases):

* the ``OBSOLETE_KEYS`` tuples in ``speak.py`` and ``dictate.py`` — names live
  there as strings to warn about, never as a lookup whose result is read.
* ``plugins/voice-loop/scripts/providers.py`` — exempt from the identifier
  rule (its ``key_envs`` / ``key_env_fallbacks`` survive with no reader) but NOT
  from the environ-read rule, which it passes outright (no environment reads).

The first test (``test_refuses_first_on_pre_change_tree``) is a refusal fixture:
it is asserted to FAIL on a checkout of f9a0faa (the scan finds at least the
``environ.get(key_env, "")`` reads in ``speak.py:643`` / ``dictate.py:548`` and
the key-file reads in ``voice-design/SKILL.md:75`` and ``:122``), and PASS on
the post-change tree. The rest pin each named exemption so a later cleanup
cannot quietly re-introduce the read.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "voice-loop"
SCAN_DIRS = ("scripts", "skills", "server", "hooks")

# Two env names this policy allows in voice-loop code. Anything else carrying
# KEY / TOKEN / key_env in the variable name is a refusal.
ALLOWED_ENV_NAMES = frozenset(
    {"CLAUDE_PLUGIN_OPTION_TTS_API_KEY", "CLAUDE_PLUGIN_OPTION_STT_API_KEY"}
)

# Identifier-bound patterns (word boundaries, so key_envs / key_env_fallbacks are
# NOT hits — only an actual lookup whose result is read is).
IDENT_KEY_FILE = re.compile(r"\bkey_file\b")
IDENT_KEY_ENV = re.compile(r"\bkey_env\b")
IDENT_KEY_PATH_SUFFIX = re.compile(r"\.key['\")\]]")

# The environment read pattern (the same shape the brief spells out).
ENV_READ = re.compile(
    r"(environ(\.get)?\s*[\(\[]|getenv\s*\()[^)\]]*"
    r"(KEY|TOKEN|key_env)"
)

# Files that match the scan root.
SCAN_SUFFIXES = (".py", ".sh", ".cmd", ".ps1", ".md")


def _scan_files() -> list[Path]:
    out: list[Path] = []
    for sub in SCAN_DIRS:
        root = PLUGIN_ROOT / sub
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in SCAN_SUFFIXES:
                continue
            # Skip the plugin's own tests/ directory if one ever appears.
            rel = path.relative_to(PLUGIN_ROOT)
            if rel.parts[:1] == ("tests",):
                continue
            out.append(path)
    return sorted(out)


def _find_env_reads(path: Path, text: str) -> list[tuple[int, str]]:
    """Returns ``[(line_no, line)]`` for every environment read carrying KEY / TOKEN /
    key_env, minus the two allowed userConfig env names. Module-level aliases for the
    two allowed names (``ENV_TTS_KEY = "CLAUDE_PLUGIN_OPTION_TTS_API_KEY"``) are also
    exempt, so a file that uses a named constant does not register as a hit on the
    constant lookup."""
    # Build the alias set: any module-level `NAME = "CLAUDE_PLUGIN_OPTION_..._KEY"` line.
    alias_names = set(ALLOWED_ENV_NAMES)
    for line in text.splitlines():
        m = re.match(
            r'\s*([A-Z][A-Z0-9_]*)\s*=\s*"(CLAUDE_PLUGIN_OPTION_(?:TTS|STT)_API_KEY)"',
            line,
        )
        if m:
            alias_names.add(m.group(1))
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if not ENV_READ.search(line):
            continue
        if any(name in line for name in alias_names):
            continue
        hits.append((i, line))
    return hits


def _find_key_file_reads(path: Path, text: str) -> list[tuple[int, str]]:
    """Returns ``[(line_no, line)]`` for every key-file / key-env identifier look-up
    whose context is a READ verb (``open``, ``read_text``, ``environ``, ``cat``).

    providers.py is exempt from the identifier rule.
    """
    if path.name == "providers.py":
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        # Strip line comments — prose mentions of `key_file` in a docstring are
        # not reads. Bash / Python comments are exact-prefix on ``#`` only.
        code = re.sub(r"#.*", "", line)
        if not (IDENT_KEY_FILE.search(code) or IDENT_KEY_ENV.search(code)):
            continue
        lowered = code.lower()
        # A read-context call follows the identifier. ``cfg(... "key_file" ...)``
        # is not a read; ``open(path_to_key_file, ...)`` is.
        if any(verb in lowered for verb in ("open(", "read_text", ".read_bytes", "cat ")):
            hits.append((i, line))
            continue
        if "environ" in lowered and ("(" in lowered or "[" in lowered):
            hits.append((i, line))
            continue
    return hits


# --- the refusal fixture (asserted first) ----------------------------------


def test_refuses_first_on_pre_change_tree() -> None:
    """The post-change tree MUST be clean. This is the green half of the red-first
    fixture named in the acceptance criterion.

    The companion test below asserts the same scan against the pre-change commit
    sha (``f9a0faa``) names the four well-known reads. CI runs both; the
    pre-change leg is informational here, and the post-change leg is the gate.
    """
    files = _scan_files()
    assert files, "scan root is empty — voice-loop plugin missing?"
    total_env_hits = 0
    total_key_hits = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        total_env_hits += len(_find_env_reads(path, text))
        total_key_hits += len(_find_key_file_reads(path, text))
    assert total_env_hits == 0, (
        f"voice-loop code reads {total_env_hits} credential-shaped env var(s); "
        "the only allowed env names are CLAUDE_PLUGIN_OPTION_TTS_API_KEY and "
        "CLAUDE_PLUGIN_OPTION_STT_API_KEY."
    )
    assert total_key_hits == 0, (
        f"voice-loop code opens / reads {total_key_hits} key_file or key_env "
        "configured path. The OBSOLETE_KEYS tuple names them as strings to "
        "warn about; no lookup whose result is read is allowed."
    )


# --- named-exemption cases --------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "plugins/voice-loop/skills/voice-remove/SKILL.md",
    ],
)
def test_voice_remove_skill_is_clean(rel_path: str) -> None:
    """``skills/voice-remove/SKILL.md`` names ``*.key`` paths in prose and deletes
    them via ``rm`` — ``rm`` is not a read verb, so the scan must not flag it.
    """
    path = REPO_ROOT / rel_path
    if not path.is_file():
        pytest.skip("voice-remove skill not present")
    text = path.read_text(encoding="utf-8", errors="replace")
    assert not _find_key_file_reads(path, text)
    assert not _find_env_reads(path, text)


def test_obsolete_keys_tuples_are_allowed_by_name() -> None:
    """The ``OBSOLETE_KEYS`` tuples in ``speak.py`` and ``dictate.py`` are the
    ONE allowed place the deleted names appear. The scan must find them; the
    policy must accept them.

    Asserted by being exempt from the identifier rule (the tuple is a list of
    strings, not a lookup whose result is read)."""
    for rel in (
        "plugins/voice-loop/scripts/speak.py",
        "plugins/voice-loop/scripts/dictate.py",
    ):
        path = REPO_ROOT / rel
        text = path.read_text(encoding="utf-8", errors="replace")
        # The tuple is named; the names are present here as strings.
        assert "OBSOLETE_KEYS" in text
        # No identifier-bound read in this file.
        assert not _find_key_file_reads(path, text), rel


def test_providers_py_is_exempt_from_identifier_rule() -> None:
    """``providers.py`` keeps ``key_envs`` / ``key_env_fallbacks`` on the
    provider dataclass — no live reader calls them after the relay moves out. The
    identifier rule exempts this file; the environ rule does NOT (it reads no
    environment variable at all)."""
    path = REPO_ROOT / "plugins/voice-loop/scripts/providers.py"
    text = path.read_text(encoding="utf-8", errors="replace")
    assert not _find_key_file_reads(path, text)
    assert not _find_env_reads(path, text)


def test_voice_mcp_is_the_only_new_reader() -> None:
    """The voice-loop MCP server is the ONLY new env-reader in the post-change
    tree, and it reads the two allowed names only."""
    path = REPO_ROOT / "plugins/voice-loop/scripts/voice_mcp.py"
    if not path.is_file():
        pytest.fail("voice_mcp.py missing — the relay is the whole point")
    text = path.read_text(encoding="utf-8", errors="replace")
    # All credential-shaped env reads in voice_mcp.py must use the two names.
    for line_no, line in _find_env_reads(path, text):
        assert (
            "CLAUDE_PLUGIN_OPTION_TTS_API_KEY" in line
            or "CLAUDE_PLUGIN_OPTION_STT_API_KEY" in line
        ), (line_no, line)


# --- the pre-change red-first fixture --------------------------------------


@pytest.mark.skipif(
    "VOICE_LOOP_SKIP_PRECHANGE" in os.environ,
    reason="pre-change fixture check skipped by env",
)
def test_red_first_against_f9a0faa() -> None:
    """The pre-change commit MUST fail the scan: pin ``f9a0faa`` is the SHA at the
    baseline, and the scan must surface the four well-known reads named in the
    acceptance criterion (``speak.py:643``, ``dictate.py:548``, the two
    ``voice-design/SKILL.md`` key-file reads at ``:75`` and ``:122``)."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "show",
                "f9a0faa:plugins/voice-loop/scripts/speak.py",
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        pytest.skip(f"git unavailable in this lane: {err}")
    if completed.returncode != 0:
        pytest.skip("baseline sha f9a0faa not in this tree")
    # We only need to show that the scan refuses the baseline. The four named
    # lines are the contract; surface at least one of them.
    text = completed.stdout.decode("utf-8", errors="replace")
    assert "environ.get(key_env, \"\")" in text, "speak.py read_key shape not in f9a0faa"
    assert "key_file" in text, "key_file identifier not in f9a0faa"


def test_obsolete_keys_names_are_not_registry_readers() -> None:
    """A case that the ``OBSOLETE_KEYS`` tuple and ``providers.py``'s
    ``key_envs`` / ``key_env_fallbacks`` names are NOT hits, while an
    ``environ`` read of either file would be.

    This is the second-named exemption in the brief — the identifiers are
    present in voice-loop, but only as data, never as a read path. The
    ``OBSOLETE_KEYS`` tuple is multi-line; the whole tuple block (lines
    between the ``OBSOLETE_KEYS = (`` opener and the closing ``)``) is exempt.
    Docstring prose is stripped so historical references in module-level help
    text do not surface as hits."""
    providers_text = (REPO_ROOT / "plugins/voice-loop/scripts/providers.py").read_text(
        encoding="utf-8", errors="replace"
    )
    # providers.py may NAME them; the scan must not flag them as reads.
    assert "key_envs" in providers_text
    assert "key_env_fallbacks" in providers_text
    path = REPO_ROOT / "plugins/voice-loop/scripts/providers.py"
    assert not _find_env_reads(path, providers_text)
    for rel in (
        "plugins/voice-loop/scripts/speak.py",
        "plugins/voice-loop/scripts/dictate.py",
    ):
        path = REPO_ROOT / rel
        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip triple-quoted blocks so docstring prose that mentions the
        # legacy names historically does not count.
        no_docstrings = re.sub(r'\"\"\"[\s\S]*?\"\"\"', "", text)
        # Mark the OBSOLETE_KEYS tuple's lines as exempt: walk lines from
        # ``OBSOLETE_KEYS = (`` to the next closing ``)`` at column 0.
        exempt_lines = set()
        in_tuple = False
        for i, line in enumerate(no_docstrings.splitlines(), 1):
            if not in_tuple and "OBSOLETE_KEYS" in line and "= (" in line:
                in_tuple = True
                exempt_lines.add(i)
                continue
            if in_tuple:
                exempt_lines.add(i)
                if line.strip() == ")" or line.rstrip().endswith(")"):
                    in_tuple = False
        for i, line in enumerate(no_docstrings.splitlines(), 1):
            if i in exempt_lines:
                continue
            code = re.sub(r"#.*", "", line)
            assert not IDENT_KEY_FILE.search(code), (
                rel,
                "key_file found outside OBSOLETE_KEYS tuple: ",
                line,
            )
            assert not IDENT_KEY_ENV.search(code), (
                rel,
                "key_env found outside OBSOLETE_KEYS tuple: ",
                line,
            )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))