"""Real-process tests for the agent-statusline settings writer.

These tests use a temporary HOME and the shipped Node helper, so they cover the actual
filesystem and process boundary rather than a second implementation of its file logic.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest


HELPER = Path(__file__).parents[1] / "plugins/agent-statusline/settings-edit.js"
STATUS_PATH = "/tmp/.claude/tools/agent-statusline.js"
PLUGIN = HELPER.parent


def run_helper(tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None):
    home = tmp_path / "home"
    settings_dir = home / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["node", str(HELPER), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def settings_path(tmp_path: Path) -> Path:
    path = tmp_path / "home/.claude/settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_set_and_remove_change_only_status_line(tmp_path):
    """L2 GAP: catches edits that reorder, drop, or mutate unrelated settings keys."""
    path = settings_path(tmp_path)
    original = {
        "permissions": {"allow": ["Read"]},
        "statusLine": {"old": True},
        "hooks": {"x": 1},
    }
    path.write_text(json.dumps(original, indent=2) + "\n")

    result = run_helper(tmp_path, "set", STATUS_PATH)
    assert result.returncode == 0, result.stderr
    updated = json.loads(path.read_text())
    assert list(updated) == list(original)
    assert updated["permissions"] == original["permissions"]
    assert updated["hooks"] == original["hooks"]
    assert updated["statusLine"] == {
        "type": "command",
        "command": f"node {STATUS_PATH}",
    }
    assert path.read_text() == json.dumps(updated, indent=2) + "\n"

    result = run_helper(tmp_path, "remove")
    assert result.returncode == 0, result.stderr
    removed = json.loads(path.read_text())
    assert list(removed) == ["permissions", "hooks"]
    assert removed["permissions"] == original["permissions"]
    assert removed["hooks"] == original["hooks"]


@pytest.mark.parametrize("payload", ["null", "[]", '"text"', "7", "true"])
def test_invalid_json_root_is_rejected_without_touching_file(tmp_path, payload):
    """L2 GAP: catches accepting non-object roots and losing their original bytes."""
    path = settings_path(tmp_path)
    path.write_text(payload)
    before = digest(path)
    result = run_helper(tmp_path, "set", STATUS_PATH)
    assert result.returncode != 0
    assert "settings.json must contain a JSON object" in result.stderr
    assert result.stdout == ""
    assert digest(path) == before
    assert not list(path.parent.glob(".settings.json.tmp-*"))


def test_unparseable_settings_is_rejected_without_touching_file(tmp_path):
    """L2 GAP: catches parse failures that truncate or replace the user's settings."""
    path = settings_path(tmp_path)
    path.write_text('{"permissions":')
    before = digest(path)
    result = run_helper(tmp_path, "remove")
    assert result.returncode != 0
    assert result.stderr
    assert result.stdout == ""
    assert digest(path) == before
    assert not list(path.parent.glob(".settings.json.tmp-*"))


def test_missing_settings_has_no_backup_and_remove_is_noop(tmp_path):
    """L2 GAP: catches creating backups or an empty settings file for first install/removal."""
    result = run_helper(tmp_path, "set", STATUS_PATH)
    assert result.returncode == 0, result.stderr
    path = settings_path(tmp_path)
    assert not list(path.parent.glob("settings.json.bak.*"))
    assert (
        json.loads(path.read_text())["statusLine"]["command"] == f"node {STATUS_PATH}"
    )

    path.unlink()
    result = run_helper(tmp_path, "remove")
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert not list(path.parent.glob("settings.json.bak.*"))


COLLIDE_PRELOAD = (
    "const fs=require('node:fs'); const path=require('node:path'); "
    "const d=path.join(require('node:os').homedir(), '.claude'); "
    "fs.writeFileSync(path.join(d, `.settings.json.tmp-${process.pid}-${'b'.repeat(32)}`), 'keep'); "
    "const c=require('node:crypto'); c.randomBytes=()=>Buffer.from('b'.repeat(32),'hex');"
)


@pytest.mark.parametrize("failure", ["read-only-directory", "temp-collision-exhaustion"])
def test_failed_first_set_leaves_settings_absent(tmp_path, failure):
    """L2 GAP: catches a failed first-time set that leaves an empty, partial or backup settings file."""
    path = settings_path(tmp_path)
    extra_env = None
    if failure == "read-only-directory":
        if os.geteuid() == 0:
            pytest.skip("read-only directory semantics are bypassed by root")
        path.parent.chmod(0o555)
    else:
        preload = tmp_path / "collide.cjs"
        preload.write_text(COLLIDE_PRELOAD)
        extra_env = {"NODE_OPTIONS": f"--require={preload}"}
    try:
        result = run_helper(tmp_path, "set", STATUS_PATH, extra_env=extra_env)
    finally:
        path.parent.chmod(0o755)
    assert result.returncode != 0
    assert result.stderr
    assert result.stdout == ""
    assert not path.exists()
    assert not list(path.parent.glob("settings.json.bak.*"))
    assert sorted(p.read_text() for p in path.parent.glob(".settings.json.tmp-*")) == (
        ["keep"] if failure == "temp-collision-exhaustion" else []
    )


def test_backup_collision_and_stray_temp_are_safe(tmp_path):
    """L2 GAP: catches overwriting an old backup or treating a stale temp as this run's temp."""
    path = settings_path(tmp_path)
    path.write_text('{"hooks":{"x":1}}\n')
    backup = path.parent / "settings.json.bak.20261014T073111000Z"
    backup.write_bytes(b"older backup")
    stray = path.parent / (".settings.json.tmp-999-" + "a" * 32)
    stray.write_bytes(b"stray")
    preload = tmp_path / "fixed-clock.cjs"
    preload.write_text(
        "const RealDate=Date; global.Date=class extends RealDate { "
        "constructor(...args){super(...(args.length ? args : ['2026-10-14T07:31:11.000Z']))} "
        "static now(){return new RealDate('2026-10-14T07:31:11.000Z').valueOf()} };"
    )
    result = run_helper(
        tmp_path,
        "set",
        STATUS_PATH,
        extra_env={"NODE_OPTIONS": f"--require={preload}"},
    )
    assert result.returncode == 0, result.stderr
    assert backup.read_bytes() == b"older backup"
    assert (path.parent / f"{backup.name}.1").exists()
    assert stray.read_bytes() == b"stray"


def test_all_temp_collisions_fail_closed(tmp_path):
    """L2 GAP: catches unbounded retries and mutation of pre-existing collision files."""
    preload = tmp_path / "collide.cjs"
    preload.write_text(
        "const fs=require('node:fs'); const path=require('node:path'); "
        "const d=path.join(require('node:os').homedir(), '.claude'); "
        "for(let i=0;i<100;i++) fs.writeFileSync(path.join(d, `.settings.json.tmp-${process.pid}-${'b'.repeat(32)}`), 'keep'); "
        "const c=require('node:crypto'); c.randomBytes=()=>Buffer.from('b'.repeat(32),'hex');"
    )
    path = settings_path(tmp_path)
    path.write_text('{"hooks":{"x":1}}\n')
    result = run_helper(
        tmp_path,
        "set",
        STATUS_PATH,
        extra_env={"NODE_OPTIONS": f"--require={preload}"},
    )
    assert result.returncode != 0
    assert "collision exhaustion" in result.stderr
    assert path.read_text() == '{"hooks":{"x":1}}\n'
    assert len(list(path.parent.glob(".settings.json.tmp-*"))) == 1
    assert next(path.parent.glob(".settings.json.tmp-*")).read_text() == "keep"


def test_invalid_command_and_relative_path_fail(tmp_path):
    """L2 GAP: catches accidental permissive CLI parsing that can write an unintended path."""
    for args in (("unknown",), ("set", "relative.js")):
        result = run_helper(tmp_path, *args)
        assert result.returncode != 0
        assert result.stderr
        assert result.stdout == ""


def test_write_failure_leaves_existing_bytes_intact(tmp_path):
    """L2 GAP: catches in-place truncation when the settings directory cannot accept a write."""
    if os.geteuid() == 0:
        pytest.skip("read-only directory semantics are bypassed by root")
    path = settings_path(tmp_path)
    path.write_text('{"hooks":{"x":1}}\n')
    before = digest(path)
    path.parent.chmod(0o555)
    try:
        result = run_helper(tmp_path, "set", STATUS_PATH)
    finally:
        path.parent.chmod(0o755)
    assert result.returncode != 0
    assert result.stderr
    assert result.stdout == ""
    assert digest(path) == before
    assert not list(path.parent.glob(".settings.json.tmp-*"))


def test_directory_review_text_contract_is_current():
    """L2 GAP: catches documentation, version, and skill-contract drift after helper changes."""
    setup = (PLUGIN / "skills/statusline-setup/SKILL.md").read_text()
    remove = (PLUGIN / "skills/statusline-remove/SKILL.md").read_text()
    readme = (PLUGIN / "README.md").read_text()
    assert not any(
        "jq" in path.read_text() for path in PLUGIN.rglob("*") if path.is_file()
    )
    for path in (
        PLUGIN / ".claude-plugin/plugin.json",
        PLUGIN.parents[1] / "README.md",
        PLUGIN.parents[1] / ".claude-plugin/marketplace.json",
    ):
        assert "0.2.2" not in path.read_text()
    assert "description: Install this plugin's agent-statusline status line" in setup
    assert "context use" not in setup.splitlines()[2]
    assert "weekly spend" not in setup.splitlines()[2]
    assert 'node "${CLAUDE_PLUGIN_ROOT}/settings-edit.js" set' in setup
    assert 'node "${CLAUDE_PLUGIN_ROOT}/settings-edit.js" remove' in remove
    assert "Bash" not in setup and "Bash" not in remove
    assert "merged settings file" not in setup
    assert "`/statusline-remove` before uninstalling" in readme
    assert "fixtures/payload-blank.json" in readme
