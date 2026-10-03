"""The Anthropic plugin directory's per-folder submission gate, expressed as a pytest.

The Anthropic plugin directory runs ``claude plugin validate --strict`` against every
plugin manifest, but it also enforces rules the validator does not check on its own:

  * no ``category`` key in the plugin manifest — it belongs in the marketplace entry,
    where it is documented;
  * an ``icon`` field pointing at an SVG, or a PNG of exactly 512x512 pixels, inside
    the plugin folder;
  * a ``classification`` object with exactly the five keys the directory reads:
    ``object_acted_on``, ``work_department``, ``industry``, ``life_area``, ``subject``,
    each value a non-empty string;
  * no shipped file in the plugin folder at or above 262 144 bytes (the directory's
    256 KiB inspection limit, plus a one-byte tolerance that makes the boundary
    byte-exact instead of approximate);
  * at most 512 files in the plugin folder — the directory's per-plugin file-count cap.

The directory's own rescan is webhook-driven (saharkit/windowsill#20), so this test
runs against the PR head and is the gate that catches a regression before a new
version of a plugin is published.

Stdlib plus pytest only — reading the PNG size from the IHDR bytes is four lines of
``struct.unpack``, no image library needed.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_ROOT = REPO_ROOT / "plugins"

PLUGIN_NAMES = ("voice-loop", "sill-core", "agent-handbook", "agent-statusline")
CLASSIFICATION_KEYS = frozenset(
    {"object_acted_on", "work_department", "industry", "life_area", "subject"}
)

# The directory's documented per-file inspection limit is 256 KiB (262 144 bytes).
# The acceptance criterion in fix(#5816) reads the threshold as 262 144; that is the
# number below which every shipped file must stay, with the limit inclusive (a file
# AT 262 144 bytes is held). The directory's literal cap is 262 144 bytes.
SIZE_LIMIT_BYTES = 262144
FILE_COUNT_LIMIT = 512


def _manifest_path(plugin_dir: Path) -> Path:
    return plugin_dir / ".claude-plugin" / "plugin.json"


def _read_manifest(plugin_dir: Path) -> dict:
    return json.loads(_manifest_path(plugin_dir).read_text(encoding="utf-8"))


@pytest.mark.parametrize("plugin", PLUGIN_NAMES)
def test_plugin_manifest_has_no_category_key(plugin: str) -> None:
    """``category`` belongs in the marketplace entry, not the plugin manifest.

    The directory's validator warns on it in a plugin manifest; the marketplace entry
    already carries the same value. Dropping it from the plugin manifest clears the
    warning without losing the listing data.
    """
    manifest = _read_manifest(PLUGINS_ROOT / plugin)
    assert "category" not in manifest, (
        f"{plugin}: plugin manifest must not carry a `category` key — "
        "the marketplace entry already holds it; the directory's validator warns on it here"
    )


@pytest.mark.parametrize("plugin", PLUGIN_NAMES)
def test_plugin_manifest_classification_has_five_keys(plugin: str) -> None:
    """The directory reads exactly five classification keys, each a non-empty string.

    The portal has no picker and the listing tab is read-only, so the values come only
    from the manifest; the test asserts the SHAPE (key set, non-empty strings) without
    pinning the values themselves — the directory's submission review owns the values.
    """
    manifest = _read_manifest(PLUGINS_ROOT / plugin)
    classification = manifest.get("classification")
    assert isinstance(classification, dict), (
        f"{plugin}: plugin manifest must carry a `classification` object; "
        f"got {type(classification).__name__}"
    )
    assert set(classification.keys()) == CLASSIFICATION_KEYS, (
        f"{plugin}: classification keys must be exactly "
        f"{sorted(CLASSIFICATION_KEYS)}; got {sorted(classification.keys())}"
    )
    for key in CLASSIFICATION_KEYS:
        value = classification[key]
        assert isinstance(value, str) and value.strip(), (
            f"{plugin}: classification.{key} must be a non-empty string; got {value!r}"
        )


@pytest.mark.parametrize("plugin", PLUGIN_NAMES)
def test_plugin_manifest_icon_resolves_to_svg_or_512x512_png(plugin: str) -> None:
    """The icon must resolve inside the plugin folder to an SVG or a 512x512 PNG.

    An SVG is recognised by the literal ``<svg`` prefix in the first 256 bytes (the
    parser-allowed leading whitespace / XML declaration). A PNG's size is read from
    the IHDR chunk's width and height big-endian uint32s at bytes 16 and 20; a 512x512
    PNG passes, anything else fails.
    """
    plugin_dir = PLUGINS_ROOT / plugin
    manifest = _read_manifest(plugin_dir)
    icon_value = manifest.get("icon")
    assert isinstance(icon_value, str) and icon_value, (
        f"{plugin}: plugin manifest must carry an `icon` string path"
    )
    icon_path = (plugin_dir / icon_value).resolve()
    # the directory rejects paths that escape the plugin folder
    assert plugin_dir.resolve() in icon_path.parents, (
        f"{plugin}: `icon` ({icon_value!r}) must resolve inside the plugin folder"
    )
    assert icon_path.is_file(), f"{plugin}: `icon` path {icon_value!r} does not exist"
    head = icon_path.read_bytes()[:256]
    if head.lstrip().startswith(b"<svg") or head.lstrip().startswith(b"<?xml"):
        return  # SVG, any size — the validator renders it
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        # IHDR width and height live at bytes 16-23 (after the 8-byte signature and
        # 8-byte chunk header), as two big-endian uint32s
        if len(head) < 24:
            head = icon_path.read_bytes()[:24]
        width = struct.unpack(">I", head[16:20])[0]
        height = struct.unpack(">I", head[20:24])[0]
        assert width == 512 and height == 512, (
            f"{plugin}: PNG icon must be exactly 512x512; got {width}x{height}"
        )
        return
    pytest.fail(
        f"{plugin}: `icon` ({icon_value!r}) is not an SVG or a PNG; "
        "the directory rejects other formats"
    )


@pytest.mark.parametrize("plugin", PLUGIN_NAMES)
def test_no_file_in_plugin_folder_at_or_above_size_limit(plugin: str) -> None:
    """No shipped file in the plugin folder is 262 144 bytes or larger.

    The directory holds any shipped file of 256 KiB or more under the
    BINARIES_NOT_INSPECTED policy hold; that is the hold voice-loop v0.8.0 sat on
    because its dictation test module was 323 596 bytes. The threshold is inclusive:
    a file AT 262 144 bytes is held, so the gate is ``>=``.
    """
    plugin_dir = PLUGINS_ROOT / plugin
    too_big = [
        p.relative_to(plugin_dir)
        for p in plugin_dir.rglob("*")
        if p.is_file() and p.stat().st_size >= SIZE_LIMIT_BYTES
    ]
    assert not too_big, (
        f"{plugin}: shipped files at or above {SIZE_LIMIT_BYTES} bytes "
        f"(directory's 256 KiB inspection limit): {sorted(too_big)}"
    )


@pytest.mark.parametrize("plugin", PLUGIN_NAMES)
def test_plugin_folder_has_at_most_file_count_limit(plugin: str) -> None:
    """The directory caps a plugin folder at 512 files.

    The count is over every regular file (including images) under the plugin folder,
    not over the files the directory itself would inspect — a plugin whose test
    fixtures alone exceed the cap is held, even if every counted file is tiny.
    """
    plugin_dir = PLUGINS_ROOT / plugin
    count = sum(1 for p in plugin_dir.rglob("*") if p.is_file())
    assert count <= FILE_COUNT_LIMIT, (
        f"{plugin}: shipped file count is {count}, above the directory's "
        f"{FILE_COUNT_LIMIT}-file cap"
    )