"""Entry-surface contracts for native Windows and published pages."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "plugins" / "voice-loop" / "scripts"
_COVERAGERC = _ROOT / "plugins" / "voice-loop" / ".coveragerc"

# B2 (#156): the count of `# pragma: windows-only` markers in scripts/speak.py at this sha.
# A regression that ADDS a marker without bumping this literal silently grows the allow-list;
# a regression that REMOVES a marker fails the marker-presence assert first, but if the marker
# is also removed from the .coveragerc registration, this test catches it as the registered-marker
# assertion below.
_WINDOWS_ONLY_MARKERS_IN_SPEAK_PY = 11
# Sister allow-list for `scripts/contracts.py`: one `pragma: windows-only` marker on
# `_windows_process_is_live`, a `ctypes.WinDLL("kernel32")` handle probe a Linux runner cannot
# reach. The token is registered in `.coveragerc` and the marker count is pinned by the same
# kind of test. A regression that ADDS a marker without bumping this literal silently grows
# the allow-list; a regression that REMOVES it (and re-exposes the ctypes body) has no other
# witness on Linux.
_WINDOWS_ONLY_MARKERS_IN_CONTRACTS_PY = 1
# Sister allow-list for the macOS-only code path (`_ps_cmdline_of`, the macOS `ps` helper): the
# token is registered in `.coveragerc` and the marker count is pinned by the same kind of test.
# A regression that ADDS a marker without bumping this literal silently grows the macOS allow-list.
_MACOS_ONLY_MARKERS_IN_SPEAK_PY = 1


def test_cmd_launchers_are_declared_for_crlf_checkout():
    """Mutation gap: losing the CRLF attributes makes copied .cmd files unreliable on Windows."""
    attrs = (_ROOT / ".gitattributes").read_text(encoding="utf-8")
    for name in ("*.cmd",):
        assert f"{name} text eol=crlf" in attrs


def test_speak_cmd_is_honestly_windows_only():
    """Mutation gap: a POSIX preamble would be dead code under the required CRLF checkout."""
    script = (_SCRIPTS / "speak.cmd").read_text(encoding="utf-8")
    assert script.startswith("@echo off\n")
    assert "exec sh -c" not in script


def test_public_pages_declare_utf8():
    """Mutation gap: opening a mirrored page from disk without charset metadata can garble Unicode."""
    for path in (_ROOT / "docs" / "index.html", _ROOT / "docs" / "voice-loop" / "index.html", _ROOT / "docs" / "ru" / "index.html", _ROOT / "docs" / "ru" / "voice-loop" / "index.html"):
        first = path.read_text(encoding="utf-8")[:500].lower()
        assert '<meta charset="utf-8">' in first


def test_platform_prose_names_native_windows_consistently():
    """Mutation gap: a stale page can direct Windows users to an unsupported path."""
    paths = (_ROOT / "README.md", _ROOT / "plugins" / "voice-loop" / "README.md", _ROOT / "docs" / "index.html", _ROOT / "docs" / "voice-loop" / "index.html")
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "native windows" in text.lower()


def test_speak_py_windows_only_marker_count_is_exactly_the_allow_list():
    """Mutation gap: adding a `pragma: windows-only` marker widens the allow-list and a future
    refactor that removes the marker (and re-exposes the line) has no other witness. Pinning the
    count here means a silent grow or shrink of the allow-list fails this test before it can
    drift unnoticed behind a green 100%."""
    text = (_SCRIPTS / "speak.py").read_text(encoding="utf-8")
    actual = text.count("pragma: windows-only")
    assert actual == _WINDOWS_ONLY_MARKERS_IN_SPEAK_PY, (
        f"scripts/speak.py carries {actual} `pragma: windows-only` markers; "
        f"this test pins the count at {_WINDOWS_ONLY_MARKERS_IN_SPEAK_PY}. "
        f"Update the literal here AND in the PR body together — the marker must remain "
        f"registered as a coverage exclusion (see test_coveragerc_registers_windows_only_marker) "
        f"and the count must be reflected in the B2 PR description."
    )


def test_contracts_py_windows_only_marker_count_is_exactly_the_allow_list():
    """Mutation gap: same shape as the speak.py pin. contracts.py carries one
    `pragma: windows-only` marker on `_windows_process_is_live`, a `ctypes.WinDLL("kernel32")`
    handle probe that a Linux runner cannot reach by construction. A drift here either grows the
    allow-list (silently, behind a green 100%) or removes the marker (and re-exposes the ctypes
    body, which has no other witness on Linux). TESTING.md's exclusion table names the count;
    pinning the literal here keeps that claim true."""
    text = (_SCRIPTS / "contracts.py").read_text(encoding="utf-8")
    actual = text.count("pragma: windows-only")
    assert actual == _WINDOWS_ONLY_MARKERS_IN_CONTRACTS_PY, (
        f"scripts/contracts.py carries {actual} `pragma: windows-only` markers; "
        f"this test pins the count at {_WINDOWS_ONLY_MARKERS_IN_CONTRACTS_PY}. "
        f"Update the literal here AND TESTING.md's exclusion table together — the marker must "
        f"remain registered as a coverage exclusion "
        f"(see test_coveragerc_registers_windows_only_marker)."
    )


def test_coveragerc_registers_windows_only_marker():
    """Mutation gap: removing `pragma: windows-only` from .coveragerc's exclude_lines makes the
    marker just a comment — coverage still runs the line, the file drops to <100%, and the per-file
    CI gate added for B2 fails without telling the reader WHY. Pin the registration here so a
    marker added to speak.py cannot be left unregistered."""
    text = _COVERAGERC.read_text(encoding="utf-8")
    assert "pragma: windows-only" in text


def test_speak_py_macos_only_marker_count_is_exactly_the_allow_list():
    """Mutation gap: same shape as the windows-only pin. The macOS token has one marker in
    speak.py — `_ps_cmdline_of`, the macOS `ps -p` helper. A drift here either grows the
    allow-list (silently) or removes the marker (and re-exposes the function body)."""
    text = (_SCRIPTS / "speak.py").read_text(encoding="utf-8")
    actual = text.count("pragma: macos-only")
    assert actual == _MACOS_ONLY_MARKERS_IN_SPEAK_PY, (
        f"scripts/speak.py carries {actual} `pragma: macos-only` markers; "
        f"this test pins the count at {_MACOS_ONLY_MARKERS_IN_SPEAK_PY}. "
        f"Update the literal here AND in the PR body together — the marker must remain "
        f"registered as a coverage exclusion."
    )


def test_coveragerc_registers_macos_only_marker():
    """Mutation gap: same shape as the windows-only registration test. The macOS token must
    be present in `.coveragerc`'s exclude_lines, otherwise the marker is just a comment and
    coverage still tries to measure the body — which on Linux always fails."""
    text = _COVERAGERC.read_text(encoding="utf-8")
    assert "pragma: macos-only" in text
