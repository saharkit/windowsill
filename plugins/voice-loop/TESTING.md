# voice-loop — testing

Two halves: what CI proves mechanically (below), and the human acceptance checklist (further down)
for everything a machine structurally cannot reach.

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
   key-file handling — stdlib + pytest, no network, no player, no state dir;
3. a **real invocation of the Stop hook in CI**: a synthetic transcript, the actual `speak.sh`, the
   actual speech server, a no-op player — asserting that the marked line was extracted, that an
   unmarked line was *not* spoken, and that synthesis and playback returned success;
4. the **loopback selftest** (`selftest.sh`), which is itself one of the scripts, exercised against a
   live server on both Linux and macOS.

Real invocation is the guarantee for the runtime path. Every spoken run also logs
`timings extract_ms=… first_audio_ms=… total_ms=…` to `~/.local/state/voice-loop/speak.log`, so a
latency claim is checkable against the state log rather than taken on faith.

### What neither of those covers

The hotkey, the microphone, the paste keystroke into a real window, the Accessibility consent, and
whether you can actually *hear* it. That is the checklist below, and it needs a human.

## Human acceptance checklist

Run this before every release, on a machine that has never had voice-loop on it (a clean container or a
fresh VM for Linux; a teammate's untouched Mac for the macOS branch). "Works on the machine it was
built on" is not a result.

**How to use it:** one row per check. Fill in *observed* and *pass/fail*, and sign off at the bottom.
A failed row is a release blocker unless it is explicitly waived in writing with a reason.

Environment under test:

| field | value |
|---|---|
| date | |
| tester | |
| OS / version | |
| desktop / session (GNOME-Wayland, KDE, X11, macOS) | |
| plugin version | |
| backends chosen (stt / tts) | |
| language | |

## 1. Install from the marketplace, clean machine

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 1.1 | `claude plugin marketplace add saharkit/windowsill` (shell), or `/plugin marketplace add saharkit/windowsill` in a session | marketplace added, no errors | | |
| 1.2 | `/plugin install voice-loop@windowsill` | plugin installs; it appears in `/plugin` | | |
| 1.3 | Start a fresh session | no hook errors in the session; the Stop hook is registered | | |
| 1.4 | `/voice-setup` is offered / discoverable by name | the skill is listed | | |

## 2. `/voice-setup` under DEFAULT permission mode

Run Claude Code with its normal (default) permission mode — **not** bypass. Count every permission
prompt the setup causes.

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 2.1 | The agent states its plan before acting | a short plan, then work | | |
| 2.2 | **Permission prompt count for the whole install** | **≤ 3** (more = FAIL — that is the acceptance bar) | count: | |
| 2.3 | Language question comes first and is pre-answered from the environment | one confirm for the common case | | |
| 2.4 | Backend choice is offered per direction with the cost/privacy tradeoff stated | user makes an informed choice | | |
| 2.5 | No `sudo` is ever executed silently | any root step is PRINTED for the user to run | | |
| 2.6 | Default paste tier is clipboard (no root, no consent dialog) | `auto_paste: false` unless the user opted in | | |
| 2.7 | `~/.config/voice-loop/config.json` is written and valid | `jq . ~/.config/voice-loop/config.json` parses | | |
| 2.8 | No secret is written into the config | keys are in a `key_file` or an env var only | | |
| 2.9 | Setup ends by running the selftest | green selftest, reported plainly | | |

## 3. The loop itself

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 3.1 | `bash plugins/voice-loop/scripts/selftest.sh` | exits 0, prints said/heard/similarity | | |
| 3.2 | **Dictation round-trip**: press hotkey, say a sentence, press again | the transcript appears in the prompt (or on the clipboard, tier 1) within a few seconds | | |
| 3.3 | Dictation in `send` mode with auto-paste enabled | text is pasted AND Enter is pressed once — exactly once | | |
| 3.4 | Long dictation (~30 s of speech) | no truncation of the tail; the last words are present | | |
| 3.5 | **Speak-back**: assistant replies with a 🔊 line | it is audibly spoken, once, and matches the text | | |
| 3.6 | Unmarked lines | are NOT spoken | | |
| 3.7 | Two turns in a row | the second turn speaks the new line, not a repeat of the first (dedup) | | |
| 3.8 | A fast turn right after another | no overlapping playback — the fresher line wins | | |
| 3.9 | Stress/pronunciation of your own proper names (ru/uk) | acceptable after adding them to `stress.json` | | |
| 3.10 | **Streaming**: a 🔊 line of three or more full sentences | playback starts after roughly one sentence's worth of synthesis, not after the whole line; no audible gap between chunks | | |
| 3.11 | The timing log after 3.10 | `speak.log` shows `played … chunks=N` with N > 1, and a `timings` line whose `first_audio_ms` is well below `total_ms` | | |

## 4. Negative cases — failures must be legible, never hangs

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 4.1 | Speech server stopped, then run `selftest.sh` | clear "server not reachable" message, non-zero exit, **within the timeout, no hang** | | |
| 4.2 | Speech server stopped, then a 🔊 reply | the turn completes normally; nothing hangs; the reason is in `~/.local/state/voice-loop/speak.log` | | |
| 4.3 | Speech server stopped, then press the dictation hotkey | notification says nothing was recognized; no stuck recording; a second press behaves sanely | | |
| 4.4 | Wrong/expired cloud key | clear error naming the key source (`key_file` / env var); no key echoed anywhere | | |
| 4.5 | No microphone / no recorder installed | clear message naming what to install; no silent no-op, no stuck PID file | | |
| 4.6 | Unsupported TTS language requested | HTTP 400 listing the supported languages, not a stack trace | | |
| 4.7 | Kill the recorder process mid-recording | next hotkey press starts a fresh recording (stale PID file is cleared) | | |
| 4.8 | `jq` not installed | scripts fall back to defaults instead of crashing the turn | | |

## 5. macOS branch (run on a Mac, first teammate = beta acceptance)

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 5.1 | `/voice-setup` picks the macOS adapters | `afplay` / `pbcopy` / `osascript`, Homebrew for anything missing | | |
| 5.2 | Accessibility consent is requested once, explained before it appears | one dialog, then auto-paste works | | |
| 5.3 | `say -v <voice>` fallback path | speaks with the built-in voice when configured | | |
| 5.4 | Apple Silicon: whisper.cpp path if chosen | transcription is noticeably fast; `stt.command` wired correctly | | |
| 5.5 | Nothing in the install required root | true / false | | |

## 6. `/voice-design` (only if the user has an ElevenLabs key)

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 6.1 | Key is read from a file, never pasted into chat or config | `key_file` used; nothing echoed | | |
| 6.2 | A request to imitate a named real person | politely declined, generalized description offered instead | | |
| 6.3 | Previews are saved, numbered, and mapped to their ids | user can tell which is which | | |
| 6.4 | Chosen voice id lands in `tts.cloud.voice_id` | rest of the config survives the edit | | |
| 6.5 | Speak-back in the new voice | plays (player handles mp3) | | |

---

**Sign-off**

| | name | date | verdict |
|---|---|---|---|
| Tester | | | pass / fail |
| Waived items (with reason) | | | |
