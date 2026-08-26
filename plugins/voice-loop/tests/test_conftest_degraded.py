"""Regression tests for the silent-degradation defect (windowsill#267).

`plugins/voice-loop/tests/conftest.py` carries a hook that drops every test module
except `test_conformance.py` when `voice_server` is not importable. That hook exists so
the shelf-wide verify gate — which warms pytest + pytest-cov but never installs
fastapi / torch — can still run the conformance suite on a bare venv. The defect the
ticket describes is that the hook fires SILENTLY: a run that examined 19 of 1748 tests
prints "19 passed" and exits 0, indistinguishable from a run that examined all of them.

Each test below runs the plugin's suite in a SUBPROCESS. An in-process test would
already have `voice_server` imported by the test's own conftest, so the hook would
never see the degraded state; the only honest way to ask "what does the hook do when
voice_server is missing?" is to start a fresh interpreter with the import blocked at
the meta_path layer.

Three cases, matching the ticket:

* voice_server importable → full collection runs, exit 0 (sanity: nothing regressed).
* voice_server NOT importable and no opt-in → banner names the count and the install
  command, exit code is non-zero (the named refusal the acceptance criterion demands).
* voice_server NOT importable and the opt-in given → banner still appears, exit code 0
  (the conformance-only run remains usable for the reason the hook exists at all).

The subprocess invokes pytest via `python -c "..."` so the meta_path blocker is
installed BEFORE the conftest is imported; that is the only window where
`import voice_server` in conftest.py can be made to fail.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent

# A meta_path finder that raises ImportError for any name under `voice_server`. The
# string is identical across all three subprocess invocations; keeping it as one
# module-level constant means the test cannot drift out of sync with itself.
_BLOCKER_RUNNER = (
    "import sys\n"
    "class _VoiceLoopDegradedBlocker:\n"
    "    def find_spec(self, fullname, path, target=None):\n"
    "        if fullname == 'voice_server' or fullname.startswith('voice_server.'):\n"
    "            raise ImportError(\n"
    "                'voice_server is not importable in this environment '\n"
    "                '(blocked by test_conftest_degraded for the regression assertion)'\n"
    "            )\n"
    "        return None\n"
    "sys.meta_path.insert(0, _VoiceLoopDegradedBlocker())\n"
    "import pytest\n"
    "sys.exit(pytest.main(sys.argv[1:]))\n"
)

# A runner that imports the plugin's conftest as a plain module (via importlib), pushes
# two fake paths into `_IGNORED_AT_COLLECT`, then invokes the conftest's
# `pytest_collectstart(<top-level session>)` to prove it empties the list. Isolating
# the import here means the regression test does not rely on pytest being the runner — it
# tests the contract directly.
_RESET_RUNNER = (
    "import importlib.util, pathlib\n"
    "path = pathlib.Path('tests/conftest.py').resolve()\n"
    "spec = importlib.util.spec_from_file_location('vl_under_test', path)\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "hook = getattr(mod, 'pytest_collectstart', None)\n"
    "assert hook is not None, (\n"
    "    'conftest is missing pytest_collectstart — '\n"
    "    'without it, two runs in the same process accumulate drops'\n"
    ")\n"
    "mod._IGNORED_AT_COLLECT.extend(['/fake/test_a.py', '/fake/test_b.py'])\n"
    "pre = list(mod._IGNORED_AT_COLLECT)\n"
    "assert pre == ['/fake/test_a.py', '/fake/test_b.py'], pre\n"
    "fake_session = type('S', (), {'session': None})()\n"
    "fake_session.session = fake_session  # session is itself → the top-of-tree reset guard\n"
    "hook(fake_session)\n"
    "post = list(mod._IGNORED_AT_COLLECT)\n"
    "assert post == [], (\n"
    "    f'reset hook did not empty _IGNORED_AT_COLLECT: {post!r}'\n"
    ")\n"
    "# And a non-top-level collector must NOT clear the list — the guard exists so this\n"
    "# runs once per pytest.main() call, not once per file.\n"
    "mod._IGNORED_AT_COLLECT.extend(['/fake/test_c.py'])\n"
    "fake_subcollector = type('C', (), {})()\n"
    "fake_subcollector.session = object()  # different from itself\n"
    "hook(fake_subcollector)\n"
    "assert mod._IGNORED_AT_COLLECT == ['/fake/test_c.py'], (\n"
    "    f'reset hook fired on a non-top-level collector: {mod._IGNORED_AT_COLLECT!r}'\n"
    ")\n"
    "print('OK')\n"
)

# Local copy of the opt-in env var name, used to keep the test readable. The conftest
# owns the source of truth; if these drift, the assertion in the third case fails.
OPT_IN_ENV = "VOICE_LOOP_ALLOW_DEGRADED_COLLECTION"

# The install command the conftest banner advertises; a regression that rewrites the
# banner should make the assertion below fail.
_INSTALL_HINT = "pip install -r plugins/voice-loop/tests/requirements.txt"

# The number of server-dependent modules we expect to see dropped. The conftest's
# `pytest_ignore_collect` returns True for any `.py` file in the tests/ tree whose full
# path does not contain the substring "test_conformance". That includes:
#   * every `test_*.py` module except `test_conformance.py` itself (its path matches),
#   * `tests/conftest.py` itself (the substring does not appear in its path).
# Counted from disk (rather than hard-coded) so the test stays honest if a future commit
# adds or removes a module.
_DROPPED_MODULE_COUNT = sum(
    1 for path in _TESTS_DIR.glob("*.py") if "test_conformance" not in str(path)
)


def _run_subprocess(args, env_overrides=None, blocker=True, timeout=120):
    """Invoke `python -m pytest …` from the plugin root, optionally with the blocker.

    The conftest keys its opt-in off `OPT_IN_ENV`, so every case in this file owns
    that variable explicitly: it is REMOVED from the inherited env before any
    overrides are applied, so a stray export in the parent shell (a CI matrix, a
    developer's init) cannot silently prove the opposite of what a test claims.
    Only `env_overrides` decides what the subprocess sees.
    """
    cmd = [sys.executable, "-c", _BLOCKER_RUNNER if blocker else "import pytest, sys; sys.exit(pytest.main(sys.argv[1:]))"]
    cmd.extend(args)
    env = os.environ.copy()
    env.pop(OPT_IN_ENV, None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        cmd,
        cwd=_PLUGIN_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_degraded_collection_full_path_still_green_when_voice_server_is_importable():
    """The normal path: voice_server IS importable → collection runs, exit 0.

    Regression target: a conftest change that accidentally hooks the importable branch
    (e.g. by mutating session.exitstatus unconditionally) would flip this run red.
    """
    result = _run_subprocess(
        ["tests/", "--collect-only", "-o", "addopts="],
        blocker=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"conftest hook regressed the importable branch: rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The header is suppressed by `-q`, but the absence of any "DEGRADED" or "voice-loop:"
    # banner in the summary is itself the assertion: nothing should announce itself when
    # the run is healthy.
    assert "DEGRADED" not in combined, (
        f"banner fired on the importable branch:\n{combined}"
    )
    assert OPT_IN_ENV not in combined, (
        f"opt-in name leaked into the importable-branch output:\n{combined}"
    )
    # And the conformance module was actually collected (the user reads this).
    assert "test_conformance" in combined


def test_degraded_collection_without_opt_in_refuses_with_named_exit_code():
    """voice_server missing, no opt-in: banner names the count + install command, exit != 0.

    Regression target: the exact failure mode of #267 — a silent green. The exit code
    carries the refusal; the banner names the install hint so a reader who never read
    this file still knows what to do.
    """
    result = _run_subprocess(
        ["tests/", "--collect-only", "-o", "addopts="],
        blocker=True,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"silent green regressed: expected non-zero exit, got rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # ExitCode.TESTS_FAILED == 1; we assert the int form so the test is unambiguous
    # even on pytest builds that rename the enum.
    assert result.returncode == 1, (
        f"expected ExitCode.TESTS_FAILED (1), got rc={result.returncode}\n{combined}"
    )
    # Both questions a reader needs answered, in plain text:
    assert _INSTALL_HINT in combined, (
        f"banner missing the install command:\n{combined}"
    )
    assert f"{_DROPPED_MODULE_COUNT} test module" in combined, (
        f"banner missing the dropped-module count (expected {_DROPPED_MODULE_COUNT}):\n{combined}"
    )
    assert "voice_server is not importable" in combined, (
        f"banner missing the reason:\n{combined}"
    )
    assert OPT_IN_ENV in combined, (
        f"banner missing the opt-in name (a reader must know how to silence the refusal):\n{combined}"
    )


def test_degraded_collection_with_opt_in_exits_zero_and_still_announces():
    """voice_server missing, opt-in given: conformance-only still exits 0; banner still appears.

    Regression target: an opt-in that "works" by muting the banner would let the
    conformance-only run go green again — back to the silent-green defect. The opt-in
    exists to permit exit 0, NOT to hide that the run was degraded.
    """
    result = _run_subprocess(
        ["tests/", "--collect-only", "-o", "addopts="],
        env_overrides={OPT_IN_ENV: "1"},
        blocker=True,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"opt-in run refused exit 0: rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The opt-in only suppresses the refusal; the run is still degraded, and the
    # reader still needs the count and the install command.
    assert _INSTALL_HINT in combined, (
        f"banner missing the install command under opt-in:\n{combined}"
    )
    assert f"{_DROPPED_MODULE_COUNT} test module" in combined, (
        f"banner missing the dropped-module count under opt-in:\n{combined}"
    )
    assert "voice_server is not importable" in combined, (
        f"banner missing the reason under opt-in:\n{combined}"
    )
    # And the opt-in name appears too — the banner must say which env var was honoured.
    assert OPT_IN_ENV in combined, (
        f"banner missing the opt-in name under opt-in:\n{combined}"
    )


def test_degraded_collection_with_opt_in_unset_keeps_the_refusal():
    """The opt-in is exact-match. Setting it to anything other than '1' is the same as leaving it unset.

    Regression target: a permissive check (`!= ""`, `in {"1","true","yes"}`) would let
    a stray `VOICE_LOOP_ALLOW_DEGRADED_COLLECTION=true` from a CI matrix quiet the
    refusal — which is the same silent-green defect, just behind a different door.
    """
    for value in ("", "0", "true", "yes", "True"):
        result = _run_subprocess(
            ["tests/", "--collect-only", "-o", "addopts="],
            env_overrides={OPT_IN_ENV: value},
            blocker=True,
        )
        assert result.returncode == 1, (
            f"opt-in value {value!r} wrongly accepted as acknowledgement: "
            f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def test_degraded_collection_does_not_misclassify_no_tests_collected():
    """voice_server missing, no opt-in, `-k no_such_thing` → exit code stays NO_TESTS_COLLECTED (5).

    Regression target: `pytest_sessionfinish` previously lumped `NO_TESTS_COLLECTED` (5)
    in with `OK` (0) and rewrote both to `TESTS_FAILED` (1). NO_TESTS_COLLECTED is
    already non-zero, already honest about what happened — rewriting it tells a reader
    of the exit code that tests failed when none were even collected (the wrong reason
    for a non-zero exit, and an exit code is read precisely by people who cannot read
    the log).

    Setup: voice_server is blocked at the meta_path layer, so `pytest_ignore_collect`
    drops every server-dependent module and keeps `test_conformance.py`. Then `-k
    no_such_thing` selects nothing → pytest's natural exit is NO_TESTS_COLLECTED.
    With the fix, the sessionfinish hook leaves that code alone; with the bug, it
    became TESTS_FAILED (1) and the banner's "tests failed" framing leaked into the
    exit code itself.
    """
    result = _run_subprocess(
        ["tests/", "--collect-only", "-o", "addopts=", "-k", "no_such_thing"],
        blocker=True,
    )

    combined = result.stdout + result.stderr
    # ExitCode.NO_TESTS_COLLECTED == 5; assert the int form so the test is unambiguous
    # even on pytest builds that rename the enum.
    assert result.returncode == 5, (
        f"hook misclassified NO_TESTS_COLLECTED: rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The banner still fires — the conftest should announce the dropped modules even
    # when no tests were selected. The banner is the original loud-failure signal from
    # #267; hiding it would re-open the silent-green door.
    assert _INSTALL_HINT in combined, (
        f"banner missing the install command:\n{combined}"
    )
    assert OPT_IN_ENV in combined, (
        f"banner missing the opt-in name:\n{combined}"
    )


def test_ignored_at_collection_resets_between_runs_in_one_process():
    """`pytest_collectstart` clears `_IGNORED_AT_COLLECT` so two runs in one process don't bleed.

    Regression target: the comment on `_IGNORED_AT_COLLECT` in `conftest.py` promises
    "Reset at the start of each collection so two runs in the same process don't bleed
    into each other." Without a hook that actually performs the reset, that sentence is
    a lie: the second `pytest.main()` call in one process inherits the first run's drops,
    the banner reports the cumulative total, and `pytest_sessionfinish` can flip the
    exit code based on stale state.

    The test loads `tests/conftest.py` directly via `importlib` (so the test does not
    depend on pytest being the runner), populates `_IGNORED_AT_COLLECT` with two fake
    paths, calls the hook with a top-of-tree collector (collector.session is collector),
    and asserts the list is empty. It then re-populates the list and calls the hook with
    a sub-collector to confirm the reset only fires at the top level — not once per
    collected file. If the hook is removed, the `getattr(...)` line trips before the test
    even reaches the population step.
    """
    env = os.environ.copy()
    env.pop(OPT_IN_ENV, None)
    result = subprocess.run(
        [sys.executable, "-c", _RESET_RUNNER],
        cwd=_PLUGIN_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "OK" in result.stdout, (
        f"`pytest_collectstart` did not empty `_IGNORED_AT_COLLECT` between runs.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )