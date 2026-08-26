"""Entry-surface contracts for native Windows and published pages."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "plugins" / "voice-loop" / "scripts"
_COVERAGERC = _ROOT / "plugins" / "voice-loop" / ".coveragerc"


def _registered_exclude_lines():
    """The entries currently listed under ``[report] exclude_lines`` in the plugin's
    `.coveragerc`. coverage.py parses this list as a YAML array under `[report]`, so the
    substring-search the original tests used (and which still would catch the token anywhere
    in the file, including in comments) does not match the registration the way coverage.py
    sees it. This helper returns only what coverage.py will treat as an exclusion — so the
    un-registration tests below can assert ABSENCE without being tripped by explanatory prose
    that names the dropped tokens."""
    text = _COVERAGERC.read_text(encoding="utf-8")
    in_report = False
    in_exclude_lines = False
    registered = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_report = line == "[report]"
            in_exclude_lines = False
            continue
        if in_report and line.startswith("exclude_lines"):
            in_exclude_lines = True
            continue
        if in_exclude_lines:
            if not line or line.startswith("["):
                in_exclude_lines = False
                continue
            registered.append(line)
    return registered

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
    """Mutation gap: adding a `pragma: windows-only` marker widens the source-side documentation
    of what is platform-specific and a future refactor that removes the marker (and re-exposes
    the line) has no other witness. After #276 these markers are NO LONGER coverage exclusions —
    they are documentation of what each platform leg is responsible for exercising — but they
    must still be present in the source so the platform-specificity of the line is obvious to a
    reader. Pinning the count here means a silent grow or shrink of that documentation fails
    this test before it can drift unnoticed behind a green union 100%."""
    text = (_SCRIPTS / "speak.py").read_text(encoding="utf-8")
    actual = text.count("pragma: windows-only")
    assert actual == _WINDOWS_ONLY_MARKERS_IN_SPEAK_PY, (
        f"scripts/speak.py carries {actual} `pragma: windows-only` markers; "
        f"this test pins the count at {_WINDOWS_ONLY_MARKERS_IN_SPEAK_PY}. "
        f"Update the literal here AND in the PR body together — the markers are documentation "
        f"of what the Windows coverage leg is responsible for exercising, and they must NOT be "
        f"registered as coverage exclusions (see test_coveragerc_does_not_register_platform_markers)."
    )


def test_contracts_py_windows_only_marker_count_is_exactly_the_allow_list():
    """Mutation gap: same shape as the speak.py pin. contracts.py carries one
    `pragma: windows-only` marker on `_windows_process_is_live`, a `ctypes.WinDLL("kernel32")`
    handle probe that a Linux runner cannot reach by construction. After #276 the marker is
    documentation of platform-specificity, not a coverage exclusion. A drift here either grows
    the allow-list (silently, behind a green union 100%) or removes the marker (and re-exposes
    the ctypes body, which has no other witness on Linux). TESTING.md's enumeration names the
    count; pinning the literal here keeps that claim true."""
    text = (_SCRIPTS / "contracts.py").read_text(encoding="utf-8")
    actual = text.count("pragma: windows-only")
    assert actual == _WINDOWS_ONLY_MARKERS_IN_CONTRACTS_PY, (
        f"scripts/contracts.py carries {actual} `pragma: windows-only` markers; "
        f"this test pins the count at {_WINDOWS_ONLY_MARKERS_IN_CONTRACTS_PY}. "
        f"Update the literal here AND TESTING.md's enumeration together — the marker is "
        f"documentation of what the Windows coverage leg is responsible for exercising, "
        f"and it must NOT be registered as a coverage exclusion "
        f"(see test_coveragerc_does_not_register_platform_markers)."
    )


def test_coveragerc_does_not_register_windows_only_marker():
    """Mutation gap: re-registering `pragma: windows-only` in .coveragerc's exclude_lines would
    silently re-exclude platform-specific code from the union gate — the very pattern #276
    overturns. The plugin's standing rule (in TESTING.md) is that platform code is MEASURED on a
    coverage leg per platform, not excluded. Inverting the prior registration pin, this test
    asserts the marker is ABSENT from the `[report] exclude_lines` block of .coveragerc, so a
    regression that restores it fails here and the reader sees the failure mode rather than a
    green 100% that hides an exclusion. Uses the parsed list rather than a whole-file substring
    so explanatory prose that names the dropped token doesn't trip the assertion."""
    registered = _registered_exclude_lines()
    assert "pragma: windows-only" not in registered, (
        "FAIL: `pragma: windows-only` is registered in plugins/voice-loop/.coveragerc's "
        "exclude_lines — the platform-exclusion table that #276 retired. voice-loop's standing "
        "rule (TESTING.md) is that platform-specific code gets a coverage leg per platform, "
        "not an exclusion. Remove the entry from .coveragerc's exclude_lines; the marker stays "
        "in the source as documentation of what the Windows leg is responsible for exercising."
    )


def test_speak_py_macos_only_marker_count_is_exactly_the_allow_list():
    """Mutation gap: same shape as the windows-only pin. The macOS token has one marker in
    speak.py — `_ps_cmdline_of`, the macOS `ps -p` helper. After #276 the marker is documentation
    of platform-specificity, not a coverage exclusion. A drift here either grows the source-side
    documentation (silently) or removes the marker (and re-exposes the function body)."""
    text = (_SCRIPTS / "speak.py").read_text(encoding="utf-8")
    actual = text.count("pragma: macos-only")
    assert actual == _MACOS_ONLY_MARKERS_IN_SPEAK_PY, (
        f"scripts/speak.py carries {actual} `pragma: macos-only` markers; "
        f"this test pins the count at {_MACOS_ONLY_MARKERS_IN_SPEAK_PY}. "
        f"Update the literal here AND in the PR body together — the marker is documentation "
        f"of what the macOS coverage leg is responsible for exercising, and it must NOT be "
        f"registered as a coverage exclusion."
    )


def test_coveragerc_does_not_register_macos_only_marker():
    """Mutation gap: same shape as the windows-only un-registration test. The macOS token must
    be ABSENT from .coveragerc's exclude_lines — the rule this plugin adopted in #276 is that
    macOS-specific code is measured by a coverage leg per platform, not excluded. Uses the
    parsed list rather than a whole-file substring so explanatory prose that names the dropped
    token doesn't trip the assertion."""
    registered = _registered_exclude_lines()
    assert "pragma: macos-only" not in registered, (
        "FAIL: `pragma: macos-only` is registered in plugins/voice-loop/.coveragerc's "
        "exclude_lines — the platform-exclusion table that #276 retired. voice-loop's standing "
        "rule (TESTING.md) is that platform-specific code gets a coverage leg per platform, "
        "not an exclusion. Remove the entry from .coveragerc's exclude_lines; the marker stays "
        "in the source as documentation of what the macOS leg is responsible for exercising."
    )


def test_coveragerc_holds_the_plugin_scoped_rule():
    """Mutation gap: the rule voice-loop adopted in #276 has TWO halves that must both hold
    in `.coveragerc`, and the mirror-image failures of either half are exactly what this test
    guards against. The first half (the one #276 actually carried out) is that no
    `pragma: .*-only` entry is registered — empty `exclude_lines` would re-exclude the
    `voice_server.py:2245` shell and un-exclude sill-core's platform fork through the combined
    rcfile, so this test asserts that `pragma: no cover` is STILL registered. The mirror-image
    failure mode the FIRST draft of #276 hit was emptying `exclude_lines` outright, which
    re-excluded the server `__main__` shell — that mistake is the second half this test pins.

    Both halves are scoped to voice-loop: the rule and its guard live in this plugin and bind
    no other. The shelf-wide rule that was promoted in an earlier revision was pulled back on
    2026-08-24 because `agent-handbook` has no coverage job and a shelf-wide rule would bind
    it anyway.
    """
    text = _COVERAGERC.read_text(encoding="utf-8")

    # Locate the `[report]` exclude_lines block — exclude_lines is parsed by coverage.py as a
    # list of strings under `[report]`, so we look there specifically rather than scanning the
    # whole file for the substring (which would also match a comment).
    in_report = False
    in_exclude_lines = False
    registered = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_report = line == "[report]"
            in_exclude_lines = False
            continue
        if in_report and line.startswith("exclude_lines"):
            in_exclude_lines = True
            continue
        if in_exclude_lines:
            if not line or line.startswith("["):
                in_exclude_lines = False
                continue
            registered.append(line)

    # First half — no platform exclusion is registered. A regression that adds
    # `pragma: windows-only` or `pragma: macos-only` (or any other `pragma: .*-only` token) back
    # fails here with a message naming the offending entry.
    for entry in registered:
        assert not entry.lstrip().startswith("pragma:") or "-only" not in entry, (
            f"FAIL: `{entry}` is registered in plugins/voice-loop/.coveragerc's exclude_lines — "
            f"the platform-exclusion table that #276 retired. voice-loop's standing rule "
            f"(TESTING.md) is that platform-specific code gets a coverage leg per platform, not "
            f"an exclusion. Remove the entry; the marker stays in the source as documentation of "
            f"what the platform leg is responsible for exercising."
        )

    # Second half — `pragma: no cover` is STILL registered. The mirror-image failure mode is
    # emptying `exclude_lines` outright, which un-excludes the `voice_server.py:2245` shell and
    # reds the non-negotiable server gate on every leg. This assertion catches that regression.
    assert "pragma: no cover" in registered, (
        "FAIL: `pragma: no cover` is no longer registered in plugins/voice-loop/.coveragerc's "
        "exclude_lines. The only `pragma: no cover` line in this plugin is "
        "`server/voice_server.py:2245`'s `if __name__ == \"__main__\":` shell, declared in "
        "TESTING.md's server section; it must stay in the exclude list or the server's "
        "non-negotiable 100% gate will red on every leg. The mirror-image mistake this guard "
        "catches is emptying exclude_lines entirely — do not do that."
    )
