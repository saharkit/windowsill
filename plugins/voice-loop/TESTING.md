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
  weekly and imports it for real (no model download, no license prompt). The same file's second job
  does the same for the `ukrainian` engine: installs `ukrainian-tts`, imports it, and asserts the
  API surface the server fakes (`Stress.Dictionary.value`, the voice names, the `tts(text, voice,
  stress, file)` signature) — no voice download. Coverage cannot substitute for those probes, and
  nothing else in CI would notice the break.
- **The RVC recolor stage is faked at the opener, and the 100% says nothing about a real converter.**
  The stage POSTs a WAV to a service that runs on somebody else's GPU, so the tests replace the
  urllib opener and everything above it runs for real — the URL check, the request that is built, the
  bounded read of the answer, and the degrade-to-the-base-voice rule on every way that can fail. What
  that structurally cannot check is whether an actual RVC deployment honours the contract in
  `server/README.md`, and there is no probe standing in for it: unlike the XTTS install, there is no
  canonical thing to install here — the converter is the operator's own, and the contract is the
  agreement between them. A green suite means the client half is right, and only that.

### The hook scripts — shellcheck, a pytest for the pure parts, and a real invocation

There is deliberately **no line-coverage number for most of the hook scripts** (the rest of
`scripts/` — `dictate.py`, `contour_poll.py`, `doctor.py`, `report_bug.py`, `install_ledger.py`,
`preview.py`, `rvc_corpus.py`, `tls-probe.py`, `wsclient.py`, the `voice-loop-dictate` entry point
— sit under the ratcheting `scripts/*` 80%-and-up gate in `selftest.yml`, not under the 100% one).
`speak.py` itself is the deliberate exception (#156 B2): the play-back / no-play / give-up paths
and the cloud-TTS error-document branch are pinned in a way mocks would not catch, and they earn
the 100% gate that the server also carries; the platform-only statements that are unreachable on
Linux (the Windows-only `msvcrt` byte-range lock, the `ctypes.WinDLL` process probe, and the
Windows-only no-`killpg` arm of `_kill_process_group`; plus the macOS-only `_ps_cmdline_of` helper
that wraps `ps -p`) are excluded by the registered `pragma: windows-only` and `pragma: macos-only`
markers in `.coveragerc` and pinned by marker-count tests so a silent grow or shrink of either
allow-list cannot drift unnoticed behind a green 100%. Line coverage is
not a meaningful metric for the rest of the glue that spends its life calling players, recorders,
`wl-copy` and `ydotool`: such code can be 100% "covered" by mocks and still fail on the only thing
that matters — the real runtime. So the guarantee is layered differently:

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
   with nothing new, which fires on every tool call — deliberately stays out of the log.
   Since #106 the **second** give-up is pinned beside it — the one with an empty stage, where the
   fixed 2.65 s ladder simply ended before a long turn's own message was flushed. The transcript's
   activity `(size, mtime_ns)` is what extends the wait: a file still being appended to buys the
   late line its polls (asserted end to end through the real `main()`, with nothing playing at
   all), a file that stops growing ends the wait, an idle one costs a single `stat` and keeps the
   2.65 s exactly, and the whole extension is bounded by a poll count like the queue's is. The
   three waits **compose in sequence** inside one firing — 2.65 s of ladder + up to 20 s of waiting
   out a wedged player + up to 12.5 s of a still-growing transcript = **35.15 s of sleep budget**.
   That is not a wall-clock bound: every poll re-parses the transcript from byte zero, so parse time
   can dominate on a growing multi-MB file. The hook also carries a structural `t0 + HOOK_BUDGET_S`
   deadline checked per poll. The test pins the sleep budget and the deadline relation separately. The wait is
   **eager-off only**: with `speak.eager` on, the Stop firing has a successor one tool call away
   and holds `speaking.lock` while it waits, so waiting there would mute the very path that would
   have said the line. The
   other half of that ticket is asserted as an absence of silence: **every** `Stop` exit writes one
   reason line — nothing marked, a bare marker, the ledger's veto, `speak.enabled: false` — which
   is conformance row 3.12 ("a turn with NO log line at all is a FAIL") stated as tests, and the
   eager path's silence is pinned from the other side so the fix cannot leak into a line per tool
   call;
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
   does not know, or keeps a row nothing writes any more. That second direction is what keeps the
   table honest when a message MOVES: the per-provider "no key" wording collapsed into one line when
   the provider registry landed, and the row it replaced could not be left behind quietly. An
   unclassified line is redacted at runtime regardless — the table falling behind costs diagnostics,
   never a leak. The transports are
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
8. **`tests/test_contour_poll.py`** for the contour poller (#40), same seam shape: the health
   fetch and the clock are injected callables, so the alert rules (unreachable, device demoted —
   only when `expect_device` declares the dependency, VRAM under the floor, `oom_overflows` on a
   rise *or a restart* and never on the level), the atomic status write and the `0/1/64` exit
   contract are all pinned without a socket. Three things there are deliberately **not** faked,
   because faking them is what let them ship unexecuted: `sample_vram`'s default runner really
   spawns a subprocess (a stub script, never `nvidia-smi` — no GPU is needed for the *spawn* to be
   the thing under test, including a wedged one killed by its own timeout); the atomic write is
   pinned at `os.replace` rather than by its outcome, because an atomic write and a truncating one
   leave the same file behind; and the poll-time bound is asserted where the waits are *spent*.
   The hook half lives in `tests/test_speak.py`: an active alert is voiced once through the
   real `contour_check` (and once through the real `entry()`, proving the page does not depend on
   the turn having a marked line), a persistent condition does not re-page, a cleared-and-returned
   one does, an alert that loses the eager lock stays unannounced for the next firing, and the
   status file is read from `contour.status_path` — the seam that used to let a relocated poller
   page nobody. What a fake cannot prove, **one real invocation in the loopback job does** — the
   *contour-poll contract* step runs the poller against the live server with `contour.status_path`
   pointed somewhere that is **not** the default and no `--status` on the command line, asserts a
   green contour makes the hook say nothing at all (with the turn's own line delivered, so silence
   cannot be a crash in disguise), then declares `expect_device: "cuda"` on runners that have no
   GPU, so a REAL demotion alert fires: exit 1, the alert message, and the real `speak.sh` voicing
   it exactly once across two invocations. Delivery is asserted on `play_text`'s own outcome
   (`played rc=…`, plus a recording player that counts the audio it was handed), never on the
   decision logged before playback starts; and the second firing must leave its own positive mark
   (`contour: already announced`), because `speak.sh` swallows every exception and exits 0, so
   "did not page twice" is otherwise satisfied by a run that crashed before it looked. The
   launcher's two fail-closed guards have their own step beside `tls-probe.sh`'s, and the VRAM
   probe has one that spawns a stub card for real. The `contour.alerts` opt-out,
   `contour.vram.command: false`, and the announced-ledger pruning are unit-tested above; the one
   thing no runner can offer is a real oversubscribed card.
9. **`tests/test_providers.py`** for the cloud provider registry, where the property under test is
   an *absence*: no dispatch path in `scripts/` compares a configured provider against a literal.
   That is checked by grep, in both the forms the code actually uses — a bare `provider ==` **and**
   the `s["provider"] == …` the two dispatch sites are written in — because a check that matched
   only the first would pass over exactly the branches this seam exists to remove. The rest of the
   file exercises every entry at its pure boundary: the request each one builds (host, path, body
   encoding, auth header) and the body each one parses, with no socket anywhere.

   **What the Deepgram fixture does and does not prove.** `tests/fixtures/` holds a pinned
   `POST /v1/listen` response and asserts the entry reads the transcript out of it, so a nesting
   drift goes RED — the failure it guards against is silent, since a parse that finds nothing
   degrades to local whisper under a log line that blames "the cloud". It is a *structural* pin, and
   `fixtures/PROVENANCE.md` says out loud that it is transcribed from the published schema rather
   than captured from a live call: it cannot catch a shape that was wrong on day one. The live
   proof — a real clip through a real key, real text out — is a **human** step recorded in the PR,
   deliberately not a CI gate, because a metered API key does not belong in this repository's CI.
10. **`tests/test_wsclient.py`** for the stdlib websocket client streaming dictation talks over
    (#99), against a **real socket**: every case stands up a listener on 127.0.0.1 and speaks the
    protocol to it by hand. That is the point rather than a convenience — this module exists
    *because* the stdlib has no websocket client, so a fake of "a websocket" would be a fake of the
    thing under test. The server half is written out in bytes in the test file (its own frame
    decoder, its own handshake response), so a client bug cannot cancel out against a server built
    from the same code. Two groups carry the weight: the handshake is **verified** (an endpoint
    answering 101 without the accept token is not a websocket server, and framing audio into
    whatever it actually is would be a dictation nobody receives; a refusal is named by its status
    line and never by its body, which is where a key could be echoed), and the read path is treated
    as the **untrusted input** it is — a declared length past the ceiling is refused from the header
    before a byte is waited for, a masked server frame, a reserved bit, an orphan continuation and
    an interleaved data frame are all refused, and a peer that vanishes is an error rather than a
    silence. The masking of *client* frames is asserted by the test's own decoder, because a
    missing mask is invisible to every server that unmasks anyway.
11. **The streaming dictation path in `tests/test_dictate.py`**, against the same kind of loopback
    socket with a fake provider on the end of it: the recording is forwarded as raw PCM (the WAV
    header never reaches the wire — the `data` chunk is *found*, because 44 bytes is only right for
    the canonical layout and ffmpeg's is not), interims are not assembled into the transcript,
    the tail written after the stop toggle still goes out before `CloseStream`, and the finals the
    server owes arrive during the drain. The URL is built by the **real registry entry**, so the
    query parameters that declare the audio's own shape are exercised rather than described. Every
    degrade has its own case — a socket that will not open, a server that hangs up mid-recording, a
    recorder that produced no header, a worker that misses its bound (killed, not merely
    abandoned), a stream that carried nothing at all versus a genuinely silent clip — because the
    property is that **a recording is never lost to the live path**. The #50 property has its own
    cases too: the worker is dispatched above the debounce and the pidfile mutex, its state is
    cleared on both ends of a cycle, and a clip below the min-clip guard still stops it.
8. **`tests/test_rvc_corpus.py`** for `rvc_corpus.py`, the reader of the RVC training corpus. It
   needs no seam at all — the corpus is a directory of WAV headers, so the tests write real (silent)
   PCM files into `tmp_path` and the script reads them exactly as it would read a real one. What is
   pinned is the arithmetic somebody would otherwise take on trust: that readiness is measured in
   **usable** audio rather than in whatever is on disk (a 0.4 s fragment and a 25 s chunker artefact
   are counted in the total and left out of the training set), and that the exit code says the same
   thing the report does.

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
| 3.4f | **Streaming dictation** (`stt.cloud.streaming: true` with a streaming provider and a real key): dictate for ~60 s | the text lands as usual; `dictate.log` shows `streaming stt done: finals=N` and `dictation latency stop_to_paste_ms=… via=stream`, and that number is markedly lower than the same dictation's `via=batch` one | | |
| 3.4g | Streaming with the socket broken on purpose (a wrong key, or an endpoint nothing listens on) | the text still arrives via the recorded clip; `dictate.log` names the failure and then `via=batch`. Nothing is left behind: no `dictate-stream.*` in the state dir, no `stream-worker` process | | |
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
| 3.17 | Any turn whose 🔊 line you did **not** hear — and any ordinary turn that had no 🔊 line at all | `speak.log` has a line saying why — a give-up, a dedup, the ledger, nothing marked, speech switched off, a synthesis failure. A turn that produced *no* log line at all is a bug (#106) | | |
| 3.17a | A LONG tool-heavy turn ending in a long 🔊 line — the shape that went silent in #106 | it is spoken. Where the flush outran the 2.65 s ladder, `speak.log` says `the transcript was still being written — the line landed …s past the ladder`; a give-up line here (or no line at all) is the bug | | |

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

## 8. WSL2 verification record (2026-08-11)

The following pass ran on 2026-08-11 on Windows 11 24H2 (build 26100.8875), Ubuntu 24.04,
WSL 2.7.11.0, kernel 6.18.33.2-2, and WSLg 1.0.73.2. It is a record of what was measured,
not a claim about untested contours. The distro was a genuine WSL2 guest (`microsoft-standard-WSL2`,
NAT `eth0`, and `wsl -l -v` reported version 2).

| # | check | observed result | verdict |
|---|---|---|---|
| 8.1 | `claude plugin marketplace add saharkit/windowsill` inside the distro | Completed in 7.1 s over HTTPS; no login required. | PASS |
| 8.2 | Install `voice-loop` and `sill-core` | Shell verbs `claude plugin install voice-loop@windowsill` and `claude plugin install sill-core@windowsill` completed; both enabled. | PASS |
| 8.3 | Registered hook command with a `🔊` payload | The command read from the installed `hooks/hooks.json` returned 0 and logged `played rc=0 ... chunks=2 via=stream`. This proves the registered command contract; live Claude Code dispatch and host audibility were not measured. | PASS (contract only) |
| 8.4 | `scripts/selftest.sh` against a `lan` server | With `--endpoint`, a real remote server returned `OK: voice-loop round trip works`, similarity 1.00 against threshold 0.75, rc=0. A valid config was ignored when `jq` was absent, so the config-driven form is not yet green. | PASS (explicit endpoint) |
| 8.5 | Dictation with the CI-style fake recorder | The fake `pw-record` and `wl-copy` pass completed all eight assertions: one recording cycle, held-key debounce, min-clip guard, STT text, transcript log, no error path, and clipboard delivery; `stop_to_paste_ms=1356`. | PASS |

The pass did not exercise a real microphone, WSLg speaker/microphone passthrough, `/voice-setup`
end to end, `wsl --install` from a blank host, or the bundled local server inside WSL. Those
remain unverified. The headless stand had no recorder, clipboard tools, `jq`, ffmpeg, or unzip;
the real dictation path therefore failed closed with `no recorder available`, as expected.

### Still to verify (attended Windows desktop required)

These are the checks the 2026-08-11 pass did not reach (8.6 and 8.9 were reached on 2026-08-14; 8.7
and 8.8 were not); run them on a real Windows 11 + WSLg
machine and fill the rows before claiming them anywhere.

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 8.6 | End a reply with a `🔊` line under live Claude Code hook dispatch | the Stop hook fires and the line is audible on the WINDOWS host's speakers — the row that decides the WSLg audio claim | **Live dispatch confirmed (2nd attempt), 2026-08-14, WIN-TEST (Windows 11 + WSL2 Ubuntu-24.04 + WSLg, plugin 0.8.0, server 0.5.0).** Hook is registered by the PLUGIN, not user settings (`hooks/hooks.json` carries `Stop` + `PostToolUse`, `async`, timeout 90; `jq .hooks` on both `settings.json` files is `null`). `speak.log` grew 5 to 9 lines, 477 to 817 B, mtime advancing from 2026-08-11T20:07 to 2026-08-14T15:10:45; the delta is that turn's marker line with `played rc=0 bytes=387644 chunks=1 via=stream`, `extract_ms=10 first_audio_ms=1105 total_ms=5300`. **Audio reached the Windows endpoint, not just the renderer:** WASAPI `IAudioMeterInformation` on the default render device (state 1 = ACTIVE) read peak_max **0.73895**, 68 samples above noise over ~4.3 s, against a same-probe silence baseline of peak_max **0** over 64 samples. WSL leg corroborates (RDPSink SUSPENDED to RUNNING to IDLE bracketing the same playback, 15:10:41 to 15:10:45). Cross-checked with a direct `/tts` payload (952844 B, 9.93 s WAV): `aplay -q` rc=0, endpoint peak_max **0.9563**, audio 1.35 to 11.4 s, duration matching the WAV. **CAVEAT — not reliable as written:** the FIRST attempt fired and went silent, logging "nothing marked in the last assistant message". Cause: the final message had been written 195 ms before the hook read the transcript, but was not yet flushed to the `.jsonl`; because `assistant_texts()` skips pure tool-call messages, `scope[-1:]` fell back to an older marker-less message. A parsed-but-unmarked scope returns `''` rather than `None`, so the "'' means exit at once, never backoff" path skips `wait_out_flush` entirely and the flush race is misclassified as a decided "nothing to say". Attempt 2 passed only because a marker was also placed on the preceding message, which is a workaround, not a fix. | **pass** (with the reliability caveat above) |
| 8.7 | `scripts/dictate-toggle.sh` with a real microphone | the log states WHICH recorder `resolve_recorder` selected, and the recorded WAV is non-empty — the row that catches a present-but-daemonless `pw-record` winning the resolution order and then failing | | |
| 8.8 | `/voice-setup` end to end inside the distro | the skill completes and ends with its proof (the green loopback selftest or the ear-check) | | |
| 8.9 | `scripts/selftest.sh` in its config-driven form (no `--endpoint`) | green loopback with the endpoint read from config; the 2026-08-11 pass found a valid config ignored when `jq` was absent, so install `jq` first | Green in the config-driven form (no `--endpoint`), 2026-08-14, same rig. The only blocker was a missing `jq`; with `jq` installed the endpoint was read from config and the loopback passed. No similarity figure or timing was recorded for this run. | **pass** |

---

**Sign-off**

| | name | date | verdict |
|---|---|---|---|
| Tester | Claude (Opus 5) in WSL2 Ubuntu-24.04 / Windows 11 WSLg, plugin 0.8.0, server 0.5.0 | 2026-08-14 | 8.6 pass (with the reliability caveat in the row) and 8.9 pass; 8.7 and 8.8 not reached |
| Waived items (with reason) | 8.7 (real microphone) and 8.8 (/voice-setup end to end) | 2026-08-14 | not waived — genuinely unmeasured; no recorder was ever run on this rig (`dictate.log` does not exist) |

## 9. WSL2 local-server verification record (2026-08-14)

A second pass on the same route as §8, three days later, on the WIN-TEST rig: Windows 11, Ubuntu 24.04
under WSL2 with WSLg, Python 3.12.3, reached over RDP. Where §8 measured a `lan` loopback against a
remote server, this pass measured the **bundled local server running inside the distro** — the contour
§8 explicitly did not exercise. As with §8 this is a record of what was observed, not a claim about
anything else.

| # | check | observed result | verdict |
|---|---|---|---|
| 9.1 | Install the bundled server's dependencies in a WSL venv | torch **2.13.0+cpu** (CPU wheel, no CUDA payload), faster-whisper 1.2.1; all imports OK and `voice_server.py` compiles. Venv 2.2 GB. | PASS |
| 9.2 | Run the server as `systemctl --user` inside WSL | `/health` returns `ok:true`, version 0.5.0, `device=cpu`, `stt_model=small`, `tts_engine=silero`, `streaming=true`, `ru` present in `tts_languages`. | PASS |
| 9.3 | `scripts/selftest.sh` against the **local** backend | Round trip green: **similarity 1.00** against threshold 0.75, rc=0; TTS rendered 396 KB of WAV and STT transcribed it back verbatim. `/health` then reports `stt_loaded=True`, `tts_loaded=['ru']`. | PASS |
| 9.4 | Audible playback through the configured player | `/tts` returned a 507 KB RIFF WAV over HTTP 200 and `aplay -q` played it, **rc=0** — out through WSLg to the Windows sound device (an RDP "Remote Audio" endpoint on this rig). | PASS |
| 9.5 | The service survives a distro restart | After `wsl --terminate`, the server **rebound on its own in ~6 s** (fresh PID, no manual start). Requires `loginctl enable-linger`; without it root's `user@0.service` never starts on a WSL boot and the port stays dead while `is-active` still reports `active`. | PASS (with linger) |
| 9.6 | Total disk cost of the local contour | **~2.7 GB**: venv 2.2 GB, HuggingFace cache 465 MB, torch cache 40 MB. | measured |
| 9.7 | Peak memory of a full round trip (TTS then STT of the result), read two ways | **VmHWM 3 735 744 kB ≈ 3.56 GiB** (process RSS high-water — the honest "how much RAM does this want" figure). cgroup `MemoryPeak` 5 233 324 032 B ≈ 4.87 GiB, which **overstates** need: it includes reclaimable page cache attributable to the unit. Both models resident (`tts_engine=silero`, `stt_model=small`); freshly started, nothing loaded: VmHWM 107 444 kB ≈ 105 MiB, `MemoryPeak` 219 721 728 B ≈ 210 MiB. Host (7930 MB total) afterwards: used 4751, free 76, available 3178 MB. Models load lazily — a peak taken before a real request of each kind means nothing. | measured |

**Two things the install script did not do, and the pass had to.** `/etc/asound.conf` must route ALSA
through WSLg's PulseServer — without it `arecord`/`aplay` find no device at all. And
`loginctl enable-linger` per row 9.5. Both are now known prerequisites of this contour, not incidents.

### What this pass did NOT close

* **A real microphone — and not for want of trying.** The rig's only capture endpoint is a "Line In"
  with nothing attached; host audio is "Remote Audio", i.e. an RDP session with microphone redirection
  off. WSLg's `RDPSource` therefore had nothing to forward and `parecord` produced a bare 44-byte
  header. Microphone privacy for desktop apps was already `Allow`, so that is not the cause. **No
  software choice makes dictation testable on a rig connected this way**; the fix is RDP microphone
  redirection at the client. Row 8.7 stays open.
* **Live Claude Code hook dispatch (row 8.6).** The session driving this pass ran on the *Windows*
  side, where the hook cannot reach a server bound to `127.0.0.1` inside the distro — verified, not
  assumed. Speak-back on this route requires Claude Code itself running inside WSL.
* **`/voice-setup` end to end (row 8.8).** The skill completed steps 0–5 and stopped at step 6; the
  ledger is left `in_progress` with `next_step: step-6-hotkey`, so a re-run resumes there rather than
  repeating the 2.7 GB.

### One structural limit, worth separating from the unmeasured ones

**There is no hotkey host under WSLg.** WSLg runs individual applications, not a desktop session, so
there is no `gsettings` (or equivalent) keybinding host to bind push-to-talk to. This is not a gap that
further testing closes — on this route dictation is invoked as a command
(`~/.local/bin/voice-loop-dictate`, which `/voice-setup` installs), not by a key. Any page describing
the WSL2 route should say so rather than imply the hotkey step applies.

**One incident worth recording because it cost the first run.** `scripts/selftest.sh` died immediately
with `set: -: invalid option` and a run of `$'\r'` errors: the `.sh` files carried CRLF from a Windows
checkout under system-level `core.autocrlf=true`. The pass ran from an LF copy and left the checkout
untouched; the repository-side fix is a `.gitattributes` with `*.sh text eol=lf`.
