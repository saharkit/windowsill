# voice-loop conformance — v0.6.0

A versioned acceptance checklist for the voice-loop plugin. This file is pinned to the
plugin version it tests: a release that changes behaviour changes this checklist, and a
checklist whose version does not match `plugin.json` is a stale checklist that must not
be used.

## How to run

In a Claude Code session with voice-loop installed:

```text
/conformance
```

The skill walks this checklist interactively: it asks you for the physical acts (tap the
hotkey, confirm you heard the sound) and probes the machine for everything else. The result
is one report file (`conformance-v0.6.0-YYYYMMDD.md`) with every row adjudicated. The
report is then offered through the same three transports as `/report-bug` — a GitHub issue
(with the `conformance` label), a pre-filled new-issue URL, or a mailto:.

Each row below has a **verdict cell** (PASS / FAIL / SKIP) and a free-text **evidence**
field. A row left empty at the end of the pass is a FAIL — every row must have a verdict.
SKIP needs a reason in the evidence cell (e.g. "Wayland-only guard, tested on X11").

## Environment under test

| field | value |
|---|---|
| date | _(filled at runtime)_ |
| tester | _(filled at runtime)_ |
| OS / version | _(filled at runtime)_ |
| desktop / session (GNOME-Wayland, KDE, X11, macOS) | _(filled at runtime)_ |
| plugin version | 0.6.0 |
| backends chosen (stt / tts) | _(filled at runtime)_ |
| language | _(filled at runtime)_ |

## 1. Install — clean machine, marketplace to working contour

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 1.1 | Marketplace add | `claude plugin marketplace add saharkit/windowsill` (shell), or `/plugin marketplace add saharkit/windowsill` in a session | marketplace added, no errors | | |
| 1.2 | Plugin install | `/plugin install voice-loop@windowsill` | plugin installs; it appears in `/plugin` | | |
| 1.3 | Fresh session start | Start a fresh Claude Code session after install | no hook errors in the session preamble; the Stop hook is registered | | |
| 1.4 | Setup discoverable | `/voice-setup` is offered or listed | the skill is available and can be invoked | | |
| 1.5 | Setup under default permission mode | Run `/voice-setup` with normal (default) permission mode — not bypass | the agent states its plan before acting; ≤3 permission prompts for the whole install | | |
| 1.6 | Language question | During setup, the language question comes first | one confirm for the common case, pre-answered from the environment | | |
| 1.7 | Backend choice | The backend choice is offered per direction | cost and privacy tradeoff stated for each; user makes an informed choice | | |
| 1.8 | No silent root | No `sudo` is ever executed silently during setup | any root step is PRINTED for the user to run, not executed | | |
| 1.9 | Default paste tier | The default dictation paste tier is clipboard (no root, no consent) | `auto_paste: false` unless the user explicitly opted in | | |
| 1.10 | Config written | `~/.config/voice-loop/config.json` is written | `jq . ~/.config/voice-loop/config.json` parses cleanly | | |
| 1.11 | No secret in config | No API key or token is written inline into config.json | keys are in a `key_file` or an env var only | | |
| 1.12 | Setup ends with proof | Setup finishes by running a verification | with HTTP endpoints: green selftest reported; with command-only backends: ear-check offered and explained | | |

## 2. Dictation — the push-to-talk loop

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 2.1 | Selftest standalone | `bash "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.sh"` (or repo-relative path from a checkout) | exits 0, prints said/heard/similarity | | |
| 2.2 | Dictation round-trip | Press the dictation hotkey, say a short sentence, press again | the transcript appears in the prompt (or on the clipboard, tier 1) within a few seconds | | |
| 2.3 | Dictation in `send` mode with auto-paste | Enable auto-paste, set mode to `send`; press hotkey, speak, press again | text is pasted AND Enter is pressed once — exactly once, not twice | | |
| 2.4 | Long dictation (~30 s of speech) | Press hotkey, speak continuously for ~30 s, press again | no truncation of the tail; the last words are present in the transcript | | |
| 2.5 | Debounce — held key | **Hold** the dictation hotkey down for 2–3 seconds, then release | ONE recording starts (and is still recording on release); `dictate.log` shows one `recording via …` and `toggle ignored — key repeat` lines, no `clip too short` | | |
| 2.6 | Debounce — quick re-tap after release | Release the key, then tap again ~1 s later | the second tap stops the recording normally; transcript arrives | | |
| 2.7 | Paste-at-focus (auto-paste on, `paste_target: any`) | Start dictation in Claude Code, switch to another app mid-speech, stop | text is pasted into the app switched TO — documented behaviour, not a bug. Confirm the README warning describes what happened | | |
| 2.8 | Same-window guard (auto-paste on, `paste_target: same-window`, macOS/X11 only) | Same as 2.7 but with the guard on | NOTHING is pasted anywhere; notification says "focus moved — text is in the clipboard"; `dictate.log` records the focus start and the suppression | | |
| 2.9 | Same-window guard, no switch | `paste_target: same-window`, stay in the same window | pastes exactly as normal — the guard is invisible when you stay put | | |
| 2.10 | Same-window guard on Wayland | Enable `paste_target: same-window` on a Wayland session | it pastes anyway (degrades to `any`); `dictate.log` says `focus at start: unknown …` — a suppressed paste here would be the bug | | |
| 2.11 | Cloud provider is a config entry | With a cloud STT key configured, set `stt.cloud.provider` to a second registry provider (`openai`, `elevenlabs` or `deepgram`) and dictate again — no other edit | the transcript arrives, from that provider's API, with no code change anywhere | | |
| 2.12 | Unknown provider says so | Set `stt.cloud.provider` to a name the registry does not carry (e.g. `"nosuchvendor"`), then dictate | `dictate.log` says the provider is not a known one and names the default it used instead — a silent fall-through would be the bug | | |

## 3. Speak-back — the assistant's voice

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 3.1 | Marked line spoken | Assistant replies with a 🔊 line | it is audibly spoken, once, and matches the text | | |
| 3.2 | Unmarked lines not spoken | Assistant replies with plain text lines (no 🔊) | only the 🔊 line is voiced; the rest is silent | | |
| 3.3 | Two turns in a row — dedup | Two consecutive turns, each with a 🔊 line | the second turn speaks its own new line, not a repeat of the first | | |
| 3.4 | Fast consecutive turns — no overlap | Send a prompt immediately after a 🔊 line starts playing | no overlapping playback — the fresher line wins; the first clip is stopped cleanly | | |
| 3.5 | Streaming — multi-sentence line | A 🔊 line of three or more full sentences | playback starts after roughly one sentence's worth of synthesis, not after the whole line; no audible gap between chunks | | |
| 3.6 | Streaming log evidence | After 3.5, check `speak.log` | `played … chunks=N` with N > 1, and a `timings` line whose `first_audio_ms` is well below `total_ms` | | |
| 3.7 | Eager mode — mid-turn narration | `speak.eager: true`; ask for a long tool-heavy turn with two 🔊 lines written mid-turn | both are narrated BEFORE the turn ends, in order, one after the other; each spoken exactly once — Stop adds nothing they already said | | |
| 3.8 | Eager mode — no history recital | Turn eager on mid-session, then let one tool call fire | the session so far is NOT recited; only lines from that point on are spoken (`speak.log` shows `seeded N line(s) of history`) | | |
| 3.9 | Eager off — repeated lines | `speak.eager: false` (default); three turns: `Done.`, `Working.`, `Done.` | all three are spoken — the repeat is not swallowed; `spoken.ledger` is never created | | |
| 3.10 | Eager on — same text, different message | `speak.eager: true`; same three turns as 3.9 | all three are spoken here too — the ledger keys on the message index, not just the text | | |
| 3.11 | Queued, not dropped | A 🔊 line long enough to play for ~10 s; send the next prompt so its reply lands mid-clip | the second line is spoken (after the first clip, or in place of it — never skipped); `speak.log` shows `queued, not dropped` | | |
| 3.12 | Unheard line accounted for | Any turn whose 🔊 line was NOT heard — and, separately, ANY ordinary turn (one with no 🔊 line in it at all) | `speak.log` has a line saying why (give-up, dedup, the ledger, nothing marked, speech switched off, synthesis failure). A turn with NO log line at all is a FAIL — including a turn that had nothing to say (#106) | | |
| 3.13 | Stress/pronunciation (ru/uk only) | Add a proper name to `~/.config/voice-loop/stress.json`, then say it in a 🔊 line | pronunciation is acceptable after the stress entry is added | | |
| 3.14 | Contour alert — the page | Add a `contour.services` entry whose `expect_device` the server does NOT match (e.g. `"gpu"` on a CPU-only box), run `bash scripts/contour-poll.sh`, then end a turn | the poller exits 1 and its line names the demotion; at the turn's end the alert is SPOKEN ("Voice contour: …"); `speak.log` shows `contour: voicing 1 alert(s)` | | |
| 3.15 | Contour alert — once, not every turn | End another turn while the condition persists | the alert is NOT spoken again; `speak.log` shows `contour: already announced` for that turn (a turn with NO contour line at all is a FAIL — the hook died before it looked); then clear the condition (remove the expectation, re-poll), let it recur later — it pages again | | |
| 3.16 | Contour page relocated | Set `contour.status_path` to a path outside the state dir, schedule the poller with NO `--status`, make a service fail, end a turn | the poller writes there and the hook pages from there — one config key, both halves. Nothing is written to the default path | | |
| 3.17 | Cloud TTS provider is a config entry | With a cloud TTS key configured, set `tts.cloud.provider` to a second registry provider (`openai`, `elevenlabs` or `deepgram`) and end a turn with a 🔊 line — no other edit | the line is spoken, synthesized by that provider's API, with no code change anywhere | | |
| 3.18 | Unknown TTS provider says so | Set `tts.cloud.provider` to a name the registry does not carry (e.g. `"nosuchvendor"`), then end a turn with a 🔊 line | `speak.log` says the provider is not a known one and names the default it used instead — a silent fall-through would be the bug | | |
| 3.19 | Long tool-heavy turn — the late flush | Ask for a turn with several tool calls and a LONG final text carrying a 🔊 line (the shape that goes silent — #106) | the line is spoken. If it arrived past the 2.65 s ladder, `speak.log` says `the transcript was still being written — the line landed …s past the ladder`; a turn that logs a give-up here (or nothing at all) is a FAIL | | |

## 4. Degrade paths — failures must be legible, never hangs

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 4.1 | Server stopped — selftest | Stop the speech server, then run `selftest.sh` | clear "server not reachable" message, non-zero exit, within the timeout — no hang | | |
| 4.2 | Server stopped — speak-back | Speech server stopped, then a 🔊 reply from the assistant | the turn completes normally; nothing hangs; the reason is in `speak.log` | | |
| 4.3 | Server stopped — dictation | Speech server stopped, then press the dictation hotkey | notification says nothing was recognized; no stuck recording; subsequent presses behave sanely | | |
| 4.4 | Wrong/expired cloud key | Configure a deliberately wrong cloud API key | clear error naming the key source (`key_file` or env var); no key echoed anywhere | | |
| 4.5 | No microphone / no recorder | Remove or disable the recorder; press the dictation hotkey | clear message naming what to install; no silent no-op, no stuck PID file | | |
| 4.6 | Unsupported TTS language | Request TTS for a language the server does not support | HTTP 400 listing the supported languages, not a stack trace | | |
| 4.7 | Killed recorder mid-recording | Start dictation, then kill the recorder process externally, then press the hotkey again | next hotkey press starts a fresh recording (stale PID file is cleared) | | |
| 4.8 | `jq` not installed | Remove `jq` from PATH temporarily; invoke a script that uses it | scripts fall back to defaults instead of crashing the turn | | |

## 5. Uninstall — `/voice-remove` then `/plugin uninstall`

> **Note:** complete uninstall (the `/voice-remove` skill) is tracked as [#17](https://github.com/saharkit/windowsill/issues/17). Rows in this section carry SKIP with that issue reference until #17 lands. Partial rows (those exercisable before #17) are marked accordingly.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 5.1 | Inventory first | Run `/voice-remove` on a machine with the full install | inventory printed FIRST — every path with its size — before anything is deleted | | |
| 5.2 | Permission prompt count | Count the permission prompts during `/voice-remove` | ≤3 | | |
| 5.3 | Service stopped and disabled | After accepting the service removal | `systemctl --user is-active voice-loop.service` → inactive, `is-enabled` → disabled or not found; the unit file is gone | | |
| 5.3b | Contour schedule stopped | Follow the README's scheduling recipe (timer or cron line), then run `/voice-remove` | the schedule is inventoried in Step 0 and stopped BEFORE the scripts go: `systemctl --user is-enabled voice-loop-contour.timer` → not found, unit files gone; a cron line is PRINTED for the user to delete, never rewritten. A timer still firing a deleted `contour-poll.sh` is a FAIL | | |
| 5.4 | Hotkey unbound | After accepting the hotkey removal | the binding is gone (GNOME: voice-loop path removed from `custom-keybindings`, user's other shortcuts intact; macOS: dictate-toggle line gone from `skhdrc`) | | |
| 5.5 | Decline all deletions | Run `/voice-remove` and decline every deletion offer | nothing is deleted; the report says so plainly | | |
| 5.6 | Key file kept when config deleted | A cloud `key_file` present; accept "delete the config" but decline the key question | `config.json` gone, `*.key` STILL there, the directory still exists; report names the kept key path | | |
| 5.7 | Model caches listed per-entry | Accept model cache deletion | each cache listed with a size and offered separately; shared parents (`~/.cache/huggingface/hub`, `~/.cache/torch/hub`) are never deleted wholesale | | |
| 5.8 | CLAUDE.md convention line removed | Accept convention-line removal | the matching line (and its blank line) removed; rest of file byte-identical; custom `speak.marker` matched | | |
| 5.9 | Closing report | After all deletions complete | lists what was intentionally left (kept keys/caches, shared packages, root daemon with removal command printed not run, macOS consents); ends with `/plugin uninstall voice-loop@windowsill` | | |
| 5.10 | No root executed | During the entire `/voice-remove` flow | no `sudo` is ever executed; any root step is PRINTED for the user to run | | |
| 5.11 | Nothing installed — graceful no-op | Run `/voice-remove` on a machine with nothing installed | says there is nothing to remove and stops; no invented work, no errors | | |
| 5.12 | Post-removal silence | After removal, start a fresh session and end with a 🔊 line | nothing is spoken, no hook errors, no stray processes | | |

---

**Checklist version:** 0.6.0 — pinned to `plugins/voice-loop/.claude-plugin/plugin.json`.
A mismatch between this version and the plugin version is a stale checklist.
