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
   live server on both Linux and macOS;
7. **`tests/test_report_bug.py`** for the bug-report collector, where the property under test is what
   a bundle must NOT contain. A whole fake install is planted — a config with a live-shaped key, a
   LAN address, a username, both logs carrying real transcript and spoken text, a third-party
   stderr line — and every one of those strings is then hunted in the rendered bundle. The
   collector's `LOG_RULES` table classifies log lines written by two *other* files in this plugin,
   so a second test reads `speak.py` and `dictate.py` and fails if either grows a log call the table
   does not know, or keeps a row nothing writes any more. An unclassified line is redacted at
   runtime regardless — the table falling behind costs diagnostics, never a leak. The transports are
   unit-tested at their seams (an injected subprocess runner for `gh`, URL round-trips through
   `urlsplit`/`parse_qs` for the other two): no issue is ever created, no mail is ever sent.
7. **`tests/test_tls_probe.py`** unit-tests `tls-probe.py` the same way — the https request and the
   repair spawn are both injected callables, so the classification (verified / certificate /
   unreachable), the python.org layout detection, and "`--fix` only claims green after a SECOND
   probe" are pinned without a socket. What a fake cannot prove, **two real invocations in CI** do,
   and they are deliberately kept apart because they reach *different branches* of the diagnosis:
   - both lanes probe a real host and expect green (an unreachable host is exit 2 and warns, not a
     red), then probe again with `SSL_CERT_FILE`/`SSL_CERT_DIR` pointed at an empty store. That
     second one proves the **env-override** branch and only that — an override in force is
     diagnosed first and unconditionally, so this shape can never reach the python.org remedy;
   - so a separate step stands up the **python.org trap itself**: an interpreter that really lives
     under `/Library/Frameworks/Python.framework/Versions/X.Y`, a real
     `Install Certificates.command` at the path the message must name, and a real certificate
     failure from a self-signed listener on the runner. It asserts the acceptance criterion of
     row 5.8 below — the message names that installer verbatim — through both the prose and
     `--json`'s `fix.kind`, and then runs `--fix` for real to prove the installer is *spawned* (one
     argv, space in the path) and that a repair which exits non-zero is still reported red.

   The one thing no runner can offer is a genuine python.org-installer Python with its own empty
   store; that is row 5.8, checked by a human.

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
| 2.10 | **Re-run** `/voice-setup` on a `local` install and pick `cloud` (or `lan`) for **both** directions | it notices the now-unused local service and offers to `systemctl --user disable --now voice-loop.service`; after accepting, `is-enabled` says `disabled` and the unit file, venv and model caches are still there (switching back is one `enable --now`, no re-download) | | |

## 3. The loop itself

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 3.1 | `bash plugins/voice-loop/scripts/selftest.sh` | exits 0, prints said/heard/similarity | | |
| 3.2 | **Dictation round-trip**: press hotkey, say a sentence, press again | the transcript appears in the prompt (or on the clipboard, tier 1) within a few seconds | | |
| 3.3 | Dictation in `send` mode with auto-paste enabled | text is pasted AND Enter is pressed once — exactly once | | |
| 3.4 | Long dictation (~30 s of speech) | no truncation of the tail; the last words are present | | |
| 3.4a | **Hold** the dictation hotkey down for two or three seconds, then release | ONE recording starts (and is still recording on release) — not a burst of start/stop cycles, and no second cycle however long the hold. `dictate.log` shows one `recording via …` and a `toggle ignored — key repeat` line per repeat, no `clip too short`. A tap ~1 s after release stops it normally | | |
| 3.4b | **Paste-at-focus, the feature** (auto-paste on, `paste_target` at its default `any`): start dictation in Claude Code, switch to another app while still speaking, stop | the text is pasted into the app you switched TO — that is the documented behaviour, not a bug. Confirm the README's warning describes what you just saw | | |
| 3.4c | **The same-window guard** (`dictate.paste_target: "same-window"`, auto-paste on), same switch as 3.4b — **macOS/X11 only** | NOTHING is pasted anywhere; the notification says "focus moved — text is in the clipboard"; your paste key still pastes it. `dictate.log` has `focus at start: …` and a `paste suppressed` line | | |
| 3.4d | The guard with **no** window switch | pastes exactly as before — the guard is invisible when you stay put | | |
| 3.4e | The guard on **Wayland** (GNOME/KDE/sway) | it pastes anyway (degrades to `any` — no portable focus query exists) and `dictate.log` says `focus at start: unknown …`. A suppressed paste here would be the bug | | |
| 3.5 | **Speak-back**: assistant replies with a 🔊 line | it is audibly spoken, once, and matches the text | | |
| 3.6 | Unmarked lines | are NOT spoken | | |
| 3.7 | Two turns in a row | the second turn speaks the new line, not a repeat of the first (dedup) | | |
| 3.8 | A fast turn right after another | no overlapping playback — the fresher line wins | | |
| 3.9 | Stress/pronunciation of your own proper names (ru/uk) | acceptable after adding them to `stress.json` | | |
| 3.10 | **Streaming**: a 🔊 line of three or more full sentences | playback starts after roughly one sentence's worth of synthesis, not after the whole line; no audible gap between chunks | | |
| 3.11 | The timing log after 3.10 | `speak.log` shows `played … chunks=N` with N > 1, and a `timings` line whose `first_audio_ms` is well below `total_ms` | | |
| 3.12 | **Eager mode** (`speak.eager: true`): ask for a long tool-heavy turn whose assistant writes two 🔊 lines mid-turn, several tool calls apart | both are narrated **before the turn ends**, in the order written, one after the other (never overlapping) — and each is spoken **once**: the `Stop` hook at the end of the turn adds nothing they already said | | |
| 3.13 | Turn eager on **mid-session**, then let one tool call fire | the session so far is NOT recited; only lines written from that point on are spoken (`speak.log` shows `seeded N line(s) of history`) | | |
| 3.14 | With eager **off** (the default), three turns whose 🔊 line is `Done.`, then `Working.`, then `Done.` again | all three are spoken — the repeat is not swallowed. `spoken.ledger` is never created: eager-off behaviour is unchanged from before eager mode existed | | |
| 3.15 | With eager **on**, the same three turns | all three are spoken here too — the ledger keys on the message, not just the text | | |
| 3.16 | A 🔊 line long enough to play for ~10 s, then send the next prompt straight away so its reply lands mid-clip | the second line is **spoken** (after the first clip, or in place of it — never skipped), and `speak.log` shows `queued, not dropped` | | |
| 3.17 | Any turn whose 🔊 line you did **not** hear | `speak.log` has a line saying why — a give-up, a dedup, a synthesis failure. A turn that produced *no* log line at all is a bug | | |

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
| 5.2 | `/voice-setup` explains the Accessibility dialog BEFORE it appears — the explanation names the dialog text verbatim ("Claude wants to control this computer"), says the dialog fires at FIRST actual dictation not during setup, and states what Allow/Decline each cost | setup presents the macOS auto-paste question with the full explanation; the dialog appears ONLY during the first actual dictation, *after* the explanation | | |
| 5.2a | **Decline** the Accessibility permission when it appears during dictation | the notification says "accessibility permission denied — text is on the clipboard"; the text IS on the clipboard and pastes with Cmd+V; the next toggle falls back to clipboard silently (no second dialog, no second denial notification) — the script detected the osascript error once and stopped retrying | | |
| 5.3 | `say -v <voice>` fallback path | speaks with the built-in voice when configured | | |
| 5.4 | Apple Silicon: whisper.cpp path if chosen | transcription is noticeably fast; `stt.command` wired correctly | | |
| 5.5 | Nothing in the install required root | true / false | | |
| 5.6 | `/voice-setup` on a **Touch Bar** Mac | it detects the Touch Bar (or asks), offers a physical chord (⌘I) rather than `F9`, and states the `fn` caveat | | |
| 5.7 | The chord it wired actually toggles dictation | one press records, one press stops — from any focused app, with no `fn` gymnastics | | |
| 5.8 | **python.org-installer Python that has never had `Install Certificates.command` run** (the real trap — a Homebrew or Xcode python will NOT reproduce it): run `/voice-setup` | setup probes TLS *before* installing; the probe goes red, its message **names `/Applications/Python 3.x/Install Certificates.command` verbatim**; the agent asks once, runs it, re-probes green, and the install then completes — **without the user having to know any of this** | | |
| 5.9 | Same Mac, decline the fix when asked | setup stops with the command printed for you to run, rather than starting a `pip install` that will fail the same way | | |
| 5.10 | `scripts/tls-probe.sh` by hand on a healthy Mac | exit 0, one OK line naming the interpreter it checked | | |

## 6. `/voice-design` (only if the user has an ElevenLabs key)

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 6.1 | Key is read from a file, never pasted into chat or config | `key_file` used; nothing echoed | | |
| 6.2 | A request to imitate a named real person | politely declined, generalized description offered instead | | |
| 6.3 | Previews are saved, numbered, and mapped to their ids | user can tell which is which | | |
| 6.4 | Chosen voice id lands in `tts.cloud.voice_id` | rest of the config survives the edit | | |
| 6.5 | Speak-back in the new voice | plays (player handles mp3) | | |

## 7. `/voice-remove` (run this LAST — it ends the machine's install)

Run it on the same machine sections 1–5 were run on, **before** `/plugin uninstall`. Same permission
discipline as section 2: default mode, count the prompts.

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 7.1 | `/voice-remove` on a machine with the full install | inventory printed **first** — every path with its size — before anything is deleted | | |
| 7.2 | **Permission prompt count for the whole removal** | **≤ 3** | count: | |
| 7.3 | The local service | `systemctl --user is-active voice-loop.service` → inactive, `is-enabled` → not found; the unit file is gone; it does not come back after a re-login | | |
| 7.4 | The hotkey | the binding is gone (GNOME: the voice-loop path is out of `custom-keybindings` **and the user's other custom shortcuts still work**; macOS: only the `dictate-toggle` line left `skhdrc`) | | |
| 7.5 | Decline every deletion offer | nothing at all is deleted; the report says so plainly | | |
| 7.6 | A cloud `key_file` present, and "delete the config" accepted **without** accepting the key question | `config.json` gone, `*.key` **still there**, the directory still exists, and the report names the kept key path | | |
| 7.7 | Model caches | each is listed with a size and offered separately; the shared parents (`~/.cache/huggingface/hub`, `~/.cache/torch/hub`) are **never** deleted wholesale — an unrelated HF model in that cache survives | | |
| 7.8 | The `CLAUDE.md` convention line | the matching line (and its blank line) is removed from the file the user picked; the rest of the file is byte-identical; a custom `speak.marker` is matched too | | |
| 7.9 | The closing report | lists what was intentionally left — kept keys/caches, shared packages, the root `ydotoold` daemon with its removal command **printed not run**, macOS Accessibility consents — and ends with `/plugin uninstall voice-loop@windowsill` | | |
| 7.10 | No `sudo` is ever executed | any root step is PRINTED for the user to run | | |
| 7.11 | `/voice-remove` on a machine with **nothing** installed | says there is nothing to remove and stops — no invented work, no errors | | |
| 7.12 | After 7.1–7.9, start a fresh session and end a reply with a 🔊 line | nothing is spoken, no hook errors, no stray processes; `/plugin uninstall voice-loop@windowsill` then completes cleanly | | |

---

**Sign-off**

| | name | date | verdict |
|---|---|---|---|
| Tester | | | pass / fail |
| Waived items (with reason) | | | |
