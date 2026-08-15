"""Entry-surface contracts for native Windows and published pages."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "plugins" / "voice-loop" / "scripts"


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
