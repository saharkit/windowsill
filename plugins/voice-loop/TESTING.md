# voice-loop — testing

Two halves: what CI proves mechanically (this file), and the human acceptance pass
([CONFORMANCE.md](CONFORMANCE.md)) for everything a machine structurally cannot reach.

## What "tested" means here — the honest version

The code in this plugin is two different materials, and they get two different guarantees. Saying
"100% coverage" without this paragraph would be marketing; here is exactly what the number is.

### Python (`server/voice_server.py`) — 100%, gated

`pytest --cov=voice_server --cov-fail-under=100` — run from this directory, `plugins/voice-loop`,
where this plugin's `pytest.ini` and `.coveragerc` live — runs on **every commit**, on **Python 3.10,
3.11, 3.12 and 3.13**, and the build fails below 100% — statements *and* branches (`branch = True` in
`.coveragerc`). It is a **gate, not a score**: it does not drift, and it is not a claim that the
server is bug-free. It is a claim that no line and no branch of that file is unexercised, so a change
cannot quietly add an untested path.

What makes 100% honest rather than decorative:

- **No models, no network, no audio hardware in the unit tests.** Every expensive dependency is
  loaded through a seam the tests replace: the recognizer comes from an importable module
  (`faster_whisper`), the voices from `torch.hub.load`, the accentuators from the packages named in
  `ACCENTUATORS`. The tests install fakes and then run the **real function bodies**.
- **The parts whose behaviour is the contract are real**: real torch tensors, real WAV encoding by
  `soundfile`, real FastAPI request handling. A `/tts` test asserts an actual `RIFF` file comes back —
  the same thing `selftest.sh` checks before feeding those bytes to recognition. The Ukrainian
  accentuator's **output format** belongs to that list too: a fake can only pin what we already
  believe about it, so one test runs the real `ukrainian-word-stress` (dictionary-only mode, the trie
  ships inside the wheel — still no model, still no network) and asserts the acute it emits is the one
  `acute_to_plus()` normalizes. It skips, alone, where the package is not installed.
- **One exclusion, declared:** the `if __name__ == "__main__":` guard, whose entire body is a call to
  `main()` — and `main()` itself is tested (with `uvicorn.run` patched). Nothing else is excluded.
- Accentuation is **off by default in the fixtures**, so a language package that happens to be
  installed in someone's environment can never make the tests reach for a model over the network.
- **What a faked seam structurally cannot check, a separate job does.** Faking `from TTS.api import
  TTS` is what keeps the suite model-free — and it means a green 100% says nothing about whether the
  XTTS install we document still works. It stopped working upstream with no change in this repo
  (#34). So `.github/workflows/xtts-install-probe.yml` installs the pinned recipe into a clean venv
  weekly and imports it for real (no model download, no license prompt). Coverage cannot substitute
  for that probe, and nothing else in CI would notice the break.

### The hook scripts — shellcheck, a pytest for the pure parts, and a real invocation

There is deliberately **no line-coverage number for the hook scripts** (and `speak.py` is *not*
under the 100% gate above — that gate is scoped to `server/voice_server.py`). Line coverage is not
a meaningful metric for glue that spends its life calling players, recorders, `wl-copy` and
`ydotool`: such code can be 100% "covered" by mocks and still fail on the only thing that matters —
the real runtime. So the guarantee is layered differently:

1. `bash -n` and **shellcheck** (`-S warning`) on every script, every commit; the speak logic
   itself is Python (stdlib-only `scripts/speak.py`, launched by a thin `speak.sh`);
2. **`tests/test_speak.py`** unit-tests the parts of `speak.py` with no I/O in them at all — the
   sentence chunker that drives streaming, the transcript extractor, the config-precedence table,
   key-file handling — stdlib + pytest, no network, no player, no state dir. Eager mode's own
   invariants are driven through the real `main()` there too, with every I/O seam faked into
   `tmp_path`: the spoken-ledger (a line is spoken once, whichever event path reached it first — and
   the test fails on a double-speak if the ledger claim is removed, and a line the assistant wrote
   twice in one message is one claim and one utterance), first-run seeding (a session is never
   recited back), the non-blocking lock (a firing that loses the race speaks nothing, claims
   nothing, and waits for nobody; a rival is still locked out mid-playback, not merely during the
   claim; `Stop` supersedes a holder it cannot wait out), and — pinned from
   both sides — that **none of that machinery exists with `speak.eager` off**, where `Stop` keeps
   exactly its pre-eager prev-utterance dedup and never touches the ledger. The **give-up** is
   pinned there too, in both directions: a line that lands ten seconds into a real playing chain
   (a live child in `playing.pid`, read back through the real identity guard — only the clock is
   faked) is spoken rather than dropped; the wait is bounded by its poll count however long a
   player wedges for; a line that was already there never waits for anybody; and every exit that
   abandons a line logs its reason, while the one exit that abandons nothing — an eager firing
   with nothing new, which fires on every tool call — deliberately stays out of the log;
3. a **real invocation of the Stop hook in CI**: a synthetic transcript, the actual `speak.sh`, the
   actual speech server, a no-op player — asserting that the marked line was extracted, that an
   unmarked line was *not* spoken, and that synthesis and playback returned success;
4. a **real invocation of the queue in CI** — two real hook processes, because what is being waited
   on is one process reading another's `playing.pid`, and no fake can show that the file is written
   early enough or read back as live. A first firing takes the stage with a long-running player; a
   second fires *while that clip is in the air* and its own marked line only reaches the transcript
   five seconds later, past the whole 2.65 s ladder. The step asserts the second firing logged
   `queued, not dropped`, never logged a give-up, and actually spoke that late line;
5. a **real invocation of eager mode in CI** — the step beside it, on the same live server, because
   a subsystem that only ever ran under fakes is a subsystem nobody has run. A second config turns
   `speak.eager` on (the first one, and the step above, stay default-off) and a second state dir
   keeps the run countable, so "spoken exactly once" is a number rather than a hopeful grep. Real
   `speak.sh` processes then walk the whole thing: first-run **seeding** (the line already in the
   transcript is ledgered, never spoken), a **`PostToolUse`** firing that speaks a marked line the
   moment it appears, a **`Stop`** firing over that same last message that stays quiet because the
   **ledger** already accounts for it — and a later one that does speak the closing line no eager
   firing ever saw — and finally two firings launched back to back over one fresh line, asserting
   that exactly ONE of them played it while the other logged its line *unclaimed* instead of
   queueing behind the speaker (the **lock**, non-blocking, in real processes);
6. the **loopback selftest** (`selftest.sh`), which is itself one of the scripts, exercised against a
   live server on both Linux and macOS.

Real invocation is the guarantee for the runtime path. Every spoken run also logs
`timings extract_ms=… first_audio_ms=… total_ms=…` to `~/.local/state/voice-loop/speak.log`, so a
latency claim is checkable against the state log rather than taken on faith.

### What neither of those covers

The hotkey, the microphone, the paste keystroke into a real window, the Accessibility consent, and
whether you can actually *hear* it. That is the **conformance pass**, and it needs a human.

## The conformance pass — where the human half lives now

The checklist that used to sit at the bottom of this file is now
**[CONFORMANCE.md](CONFORMANCE.md)**: the same coverage, restructured so an agent executes it rather
than a person transcribing chat messages by hand. A tester says *"run the conformance pass"* (or
`/voice-conformance`); Claude runs every machine-checkable row itself, asks the human only for the
physical acts, records a **PASS / FAIL / SKIP verdict with evidence for every row**, and emits one
report file — `conformance-<version>-<date>.md`, stamped with the version read out of
`.claude-plugin/plugin.json` at run time — which it then offers to file as a `conformance`-labelled
issue.

The division of labour between the two files is worth stating once, because it is the reason there
are two:

- **This file** describes the guarantees — what the 100% number covers, what it deliberately does
  not, and which real invocations in CI stand in for the parts coverage cannot honestly claim.
- **CONFORMANCE.md** is the run. It is a form with a verdict cell per row, and the release bar reads
  off it: a full pass on a clean machine, no unwaived FAIL, and every SKIP carrying a reason.

Neither replaces the other, and a green CI run is not a conformance pass.
