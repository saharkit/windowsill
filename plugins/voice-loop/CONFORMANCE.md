# voice-loop — the conformance pass

The acceptance run for a release, written so **an agent executes it** and the result leaves the
tester's machine as **one file**, not as a scroll of chat messages somebody transcribes later.

A tester says *"run the conformance pass"* (or `/voice-conformance`). Claude walks every row below,
does everything a machine can do itself, asks the human only for the acts a machine structurally
cannot perform — tap the hotkey, say a sentence, confirm you heard it — records a verdict and its
evidence for **every** row, writes one report file, and offers to file it.

- [TESTING.md](TESTING.md) says what CI proves mechanically and what that 100% number does and does
  not claim. It is a *description of the guarantees*.
- **This file is the run.** It is the form the release is signed off on, and the acceptance bar is
  blunt: a report with an empty verdict cell is not a conformance pass.

## Versioning — why this file names no version number

The plugin's version lives in `.claude-plugin/plugin.json`, is mirrored in the marketplace manifest
and in the root catalog table, and **nothing in CI checks that the three agree** (see the repo's
`CLAUDE.md`). A fourth hardcoded copy here would be a fourth thing to forget.

So the pin is made at run time instead, and it lands in the artifact rather than in the form:

- the **plugin version under test** is read from `.claude-plugin/plugin.json` when the pass starts,
  and stamped into the report and into its filename;
- the **checklist revision** is the commit this file was read at (`git rev-parse --short HEAD`, or
  the plugin's installed path if the tester has no checkout), stamped into the report beside it.

A report therefore always says which build it tested and which edition of the checklist tested it.
A checklist row that only applies from some version onward says so in its own **steps** cell — that
is a per-row fact and it belongs in the row.

## How Claude executes this — the protocol

1. **Never mark a row PASS without evidence.** The evidence cell holds what was actually observed —
   a command's exit status, a log line, the tester's own words. "Looks fine" is not evidence. If a
   row was not exercised, its verdict is `SKIP` and the evidence cell says why.
2. **Ask the human only for the physical acts.** Anything runnable — a selftest, a config read, a
   log grep — Claude runs itself. Questions to the human are of the form *"press the hotkey now, say
   'проверка связи, раз-два-три' (or any sentence), press it again, and tell me what appeared"*.
   One row at a time; do not hand the tester a wall of ten instructions.
3. **Batch the machine work.** The tester may be in default permission mode where every Bash call is
   a prompt. Group the runnable checks of a section into one call, the way `/voice-setup` does.
4. **A FAIL does not stop the pass.** Record it, capture whatever diagnostic exists (`speak.log`,
   `dictate.log`, exit codes), and keep going. A pass that aborts at the first failure tells the
   maintainer about one bug instead of all of them.
5. **Do not repair the machine mid-pass.** If a row fails because something is misconfigured, that
   *is* the finding. Fixing it and re-running turns an acceptance run into a support session and the
   report into fiction. Note the fix as a follow-up instead; if the tester asks for help, finish the
   pass first, then help.
6. **Never invent a row's outcome, and never fill a row from an earlier run.** Every verdict in a
   report belongs to that run on that machine.
7. **Quote nothing raw into a table cell.** Evidence is free text supplied by a human and by logs:
   before it goes in a cell, replace newlines with spaces and any `|` with `\|`, and truncate to a
   couple of lines with the full text kept in the section's evidence block below the table. A stray
   pipe silently breaks the row it was meant to document.
8. **Redact before delivery.** See *Before you send* — the report goes to a public tracker.

## Run metadata

Claude fills this table itself, asking only for what the machine cannot know. It is the report's
header.

| field | how it is obtained |
|---|---|
| plugin version | `jq -r .version .claude-plugin/plugin.json` from the plugin root (`${CLAUDE_PLUGIN_ROOT}` in a session) |
| checklist revision | `git rev-parse --short HEAD` in the checkout, else `installed (no checkout)` |
| date (UTC) | `date -u +%F` |
| tester | ask — a name or GitHub handle they are willing to have in a public issue |
| OS / version | `uname -sr`, `sw_vers` on macOS |
| desktop / session | `$XDG_SESSION_TYPE` / `$XDG_CURRENT_DESKTOP`, or `macOS` |
| machine state | ask — `clean` (never had voice-loop) or `reused`; a reused machine cannot pass section 1 |
| backends (stt / tts) | read from `~/.config/voice-loop/config.json` after section 2 |
| language | same file |
| paste tier | same file — `auto_paste` and what was wired |

**A conformance pass is meant to run on a clean machine** — a fresh VM or container for Linux, an
untouched Mac for the macOS branch. "Works on the machine it was built on" is not a result. A pass on
a reused machine is still worth filing; it just records `machine state: reused`, and section 1's
rows are `SKIP` with that reason.

---

## 1. Install from the marketplace

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 1.1 | marketplace add | **[human]** run `claude plugin marketplace add saharkit/windowsill` in a shell, or `/plugin marketplace add saharkit/windowsill` in a session | marketplace added, no error output | | |
| 1.2 | plugin install | **[human]** `/plugin install voice-loop@windowsill` | installs; appears in `/plugin` | | |
| 1.3 | fresh session | **[human]** start a new Claude Code session | no hook errors; the `Stop` hook is registered | | |
| 1.4 | skills discoverable | **[claude]** confirm `voice-setup`, `voice-design` and `voice-conformance` are listed as skills | all three are offered by name | | |
| 1.5 | version agreement | **[claude]** compare `.claude-plugin/plugin.json`, the marketplace entry and the catalog row in the root `README.md` | all three name the same version — no CI job checks this, so the pass does | | |

## 2. `/voice-setup` under DEFAULT permission mode

Run Claude Code in its normal permission mode — **not** bypass. The prompt count is an acceptance
bar, so count out loud as you go.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 2.1 | plan first | **[human]** run `/voice-setup`; **[claude]** observe the first message | a short plan, then work — not silent action | | |
| 2.2 | **permission prompt count** | **[human]** count every permission prompt the whole install raises | **≤ 3**. More is a FAIL, not a note | | |
| 2.3 | language first | ask is language, pre-answered from `$LANG` | one confirm in the common case | | |
| 2.4 | backend tradeoff stated | backend asked per direction | cost and privacy stated before the choice, especially for `cloud` | | |
| 2.5 | no silent root | watch for `sudo` | any root step is PRINTED for the human to run, never executed | | |
| 2.6 | default paste tier | **[claude]** read `dictate.auto_paste` from the config | `false` unless the human explicitly opted up the ladder | | |
| 2.7 | config is valid | **[claude]** `jq . ~/.config/voice-loop/config.json` | parses, exit 0 | | |
| 2.8 | no secret in config | **[claude]** grep the config for anything key-shaped | keys appear only as `key_file` or `api_key_env` | | |
| 2.9 | install ends in a proof | **[claude]** note how setup ended | green loopback selftest for HTTP backends, or the ear-check for a command-only backend — and it said which applied beforehand | | |
| 2.10 | the speak convention | **[claude]** check the `CLAUDE.md` the human chose | the 🔊 line is there exactly once, with the marker they actually configured | | |

## 3. Dictation — the happy path

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 3.1 | round trip | **[human]** press the hotkey, say a sentence, press again | the transcript reaches the prompt (tier 2/3) or the clipboard with a notification (tier 1), within a few seconds | | |
| 3.2 | accuracy | **[human]** compare what you said with what arrived | recognisable — the words are yours, not a hallucination | | |
| 3.3 | send mode | **[human]** with `mode: send` and auto-paste on, dictate one sentence | text is pasted AND Enter is pressed **exactly once** | | |
| 3.4 | long dictation | **[human]** dictate for ~30 s without pausing | no truncated tail — the last words are present | | |
| 3.5 | silence | **[human]** toggle on, say nothing, toggle off after ~2 s | a legible "nothing recognized" notification; no empty paste, no stuck state | | |
| 3.6 | log line | **[claude]** read `~/.local/state/voice-loop/dictate.log` for the run | one `recording via …` and a completed transcription per toggle pair | | |

## 4. Rapid toggle and key repeat

The debounce guard is the whole subject of this section; it is the failure mode that made dictation
look broken on a held key.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 4.1 | **hold the hotkey** | **[human]** hold the dictation key down for two or three seconds, then release | ONE recording starts and is still recording on release — no burst of start/stop cycles, however long the hold | | |
| 4.2 | what the log says | **[claude]** read `dictate.log` for 4.1 | one `recording via …`, a `toggle ignored — key repeat` line per repeat, and **no** `clip too short` | | |
| 4.3 | tap after release | **[human]** ~1 s after releasing, tap once | the recording stops and transcribes normally — the guard cleared with the hold | | |
| 4.4 | deliberate double tap | **[human]** tap, wait ~1.5 s, tap again | start then stop — a deliberate quick pair is not swallowed by the guard | | |
| 4.5 | stale PID | **[human]** start a recording, kill the recorder process, press the hotkey again | a fresh recording starts; the stale PID file was cleared, nothing wedged | | |

## 5. Speak-back

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 5.1 | marked line is spoken | **[claude]** end a reply with a 🔊 line; **[human]** confirm | audible, once, matching the text | | |
| 5.2 | unmarked lines are not | same turn, unmarked text above it | not spoken | | |
| 5.3 | dedup across turns | two turns in a row | the second speaks its own new line, not a repeat | | |
| 5.4 | fresher line wins | a fast turn right after another | no overlapping playback | | |
| 5.5 | **streaming** | **[claude]** speak a 🔊 line of three or more full sentences; **[human]** listen | playback starts after roughly one sentence's worth of synthesis, not after the whole line; no audible gap between chunks | | |
| 5.6 | streaming, in numbers | **[claude]** read `speak.log` for 5.5 | a `played … chunks=N` with N > 1, and a `timings` line whose `first_audio_ms` sits well below `total_ms` | | |
| 5.7 | queued, not dropped | **[claude]** speak a line long enough to play ~10 s; **[human]** send the next prompt straight away so the reply lands mid-clip | the second line is spoken — after the first clip or in place of it, never skipped — and `speak.log` says `queued, not dropped` | | |
| 5.8 | every silence is accounted for | **[claude]** for any turn whose 🔊 line was not heard, read `speak.log` | a line saying why (give-up, dedup, synthesis failure). A silent turn with **no** log line at all is a FAIL | | |
| 5.9 | stress of proper names (ru/uk) | **[human]** have it speak two or three of your own names | acceptable after adding them to `stress.json`; note which needed an entry | | |

## 6. Eager mode

Opt-in, off by default. Rows 6.1–6.2 prove the **default** is untouched; the rest run with
`{"speak": {"eager": true}}` in the config.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 6.1 | eager OFF, repeated line | **[claude]** three turns whose 🔊 line is `Done.`, `Working.`, `Done.` | all three are spoken — the repeat is not swallowed | | |
| 6.2 | eager OFF leaves no trace | **[claude]** check `~/.local/state/voice-loop/` | `spoken.ledger` was never created | | |
| 6.3 | mid-turn narration | **[claude]** with eager on, run a long tool-heavy turn writing two 🔊 lines several tool calls apart | both narrated **before the turn ends**, in order, never overlapping | | |
| 6.4 | spoken exactly once | same turn | the `Stop` hook at the end adds nothing already said | | |
| 6.5 | no recitation | **[human]** turn eager on mid-session, then let one tool call fire | the session so far is NOT replayed; `speak.log` shows `seeded N line(s) of history` | | |
| 6.6 | eager ON, repeated line | **[claude]** the same three turns as 6.1 | all three spoken here too — the ledger keys on the message, not just the text | | |

## 7. Degrade paths — failures must be legible, never hangs

Every row here is a deliberate breakage. Restore the machine after the section and say in the
evidence cell that you did.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 7.1 | server down, selftest | **[claude]** stop the speech server, run `selftest.sh` | a `FAIL:` line naming the endpoint it could not reach and pointing at `/health`, exit 1, **inside curl's own timeout — no hang** | | |
| 7.2 | server down, speak-back | **[claude]** with it still down, produce a 🔊 reply | the turn completes normally, nothing hangs, the reason is in `speak.log` | | |
| 7.3 | server down, dictation | **[human]** press the hotkey and speak | a notification saying nothing was recognized; no stuck recording; a second press behaves sanely | | |
| 7.4 | wrong cloud key | **[human]** point `key_file` at a bad key (cloud backends only) | a clear error naming the key **source**, with no key echoed anywhere | | |
| 7.5 | no recorder | **[claude]** make the recorder unavailable (rename it on `PATH`, or set `dictate.recorder` to a missing binary); **[human]** press the hotkey | a message naming what to install; no silent no-op, no stuck PID file | | |
| 7.6 | unsupported TTS language | **[claude]** `POST /tts` with a language the engine does not have | HTTP 400 listing the supported languages — not a stack trace, not a 500 | | |
| 7.7 | no `jq` | **[claude]** run a script with `jq` unavailable | scripts fall back to defaults instead of crashing the turn | | |
| 7.8 | restored | **[claude]** put the server, the recorder, the key and `jq` back; re-run `selftest.sh` | green again — the machine left the section as it entered it | | |

## 8. macOS branch

Run on a Mac. On Linux the whole section is `SKIP` with `not macOS` as the reason.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 8.1 | macOS adapters | **[claude]** read the config after `/voice-setup` | `afplay` / `pbcopy` / `osascript`, Homebrew for anything missing | | |
| 8.2 | Accessibility consent | **[human]** observe the consent flow | explained **before** the dialog appears, requested once, then auto-paste works | | |
| 8.3 | `say` fallback | **[human]** configure `tts.command: "say -v <voice>"` and hear a line | speaks with the built-in voice | | |
| 8.4 | whisper.cpp path | **[human]** on Apple Silicon, if chosen | transcription is noticeably fast; `stt.command` is wired correctly | | |
| 8.5 | no root anywhere | **[human]** recall the whole install | nothing required root — true or false, stated plainly | | |
| 8.6 | Touch Bar detection | **[human]** on a Touch Bar Mac, run `/voice-setup` | it detects the Touch Bar or asks, offers the ⌘I chord rather than `F9`, and states the `fn` caveat | | |
| 8.7 | the chord works | **[human]** press the wired chord from a non-terminal app | one press records, one press stops — no `fn` gymnastics | | |

## 9. `/voice-design`

Needs an ElevenLabs key. Without one the section is `SKIP` with `no cloud key` as the reason — that
is an expected skip, not a gap.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 9.1 | key hygiene | **[claude]** watch where the key is read | from `key_file`, inside the process; never in chat, never in the config, never in argv | | |
| 9.2 | the ethics line | **[human]** ask for a voice imitating a named real person | politely declined once, a generalized timbre description offered instead — no lecture | | |
| 9.3 | previews are legible | **[claude]** check the saved previews | numbered, saved to disk, each mapped to its id | | |
| 9.4 | config survives | **[claude]** diff the config before and after | `tts.cloud.voice_id` is set and nothing else was lost | | |
| 9.5 | speak-back in the new voice | **[human]** listen to the next 🔊 line | plays, and the player handles mp3 | | |

## 10. Uninstall

**Blocked: [#17](https://github.com/saharkit/windowsill/issues/17) has not landed.** Until it does,
every row here is `SKIP` with `blocked on #17` as the reason and the section still appears in the
report — a documented gap is worth more than a section quietly missing. The rows are written now so
that the pass that follows #17 has something to run rather than something to invent.

| # | scenario | steps | expected | verdict | evidence |
|---|---|---|---|---|---|
| 10.1 | uninstall is offered | **[human]** run the documented uninstall path | it exists, is named in the README, and states what it will remove before removing it | | |
| 10.2 | the 🔊 convention line | **[claude]** check the `CLAUDE.md` files `/voice-setup` may have written to | the line is removed, or the human is told exactly which file still carries it | | |
| 10.3 | config and state | **[claude]** check `~/.config/voice-loop/` and `~/.local/state/voice-loop/` | removed, or explicitly kept with the human's consent — never half of each | | |
| 10.4 | models | **[claude]** check `~/.local/share/voice-loop/` | the multi-GB model cache is named, and its removal is the human's decision | | |
| 10.5 | the service | **[claude]** `systemctl --user status voice-loop.service` (Linux) | stopped and disabled, unit file gone | | |
| 10.6 | the hotkey | **[claude]** read the gsettings custom-keybindings list, or `skhdrc` | the voice-loop binding is gone and **other** custom bindings survived | | |
| 10.7 | nothing left running | **[claude]** `pgrep -fl voice-loop` | no leftover process | | |

---

## Verdicts

Three values, and nothing else:

- **PASS** — the expectation was met, and the evidence cell says how it was observed.
- **FAIL** — it was not met. The evidence cell carries the diagnostic: exit code, log line, or the
  tester's description of what happened instead. A FAIL is a release blocker unless a maintainer
  waives it in writing, with a reason, in the report's waiver table.
- **SKIP** — the row was not exercised, and the evidence cell says why (`not macOS`, `no cloud key`,
  `blocked on #17`, `machine not clean`). **A SKIP without a reason is a FAIL of the pass itself.**

There is no "partial" and no blank. A report whose rows are not all filled is not a conformance pass,
and Claude should say so rather than file it.

## The report

One file, written **outside the repository** — it is a run artifact, and this repo keeps no generated
artifacts:

```
~/.local/state/voice-loop/conformance/conformance-<version>-<date>.md
```

`<version>` is what `plugin.json` said at the start of the run; `<date>` is `date -u +%F`. Two passes
on the same day against the same build overwrite nothing — append `-2` and say so in the report.

The report's shape is this file's shape: the run-metadata table, then every section's table with the
verdict and evidence cells filled, then the summary and sign-off below. Keep the row ids identical so
a maintainer can diff two passes.

```markdown
## Summary

| | count |
|---|---|
| PASS | |
| FAIL | |
| SKIP | |
| total rows | |

**Blockers** — one line per FAIL, id first.

**Waivers** — id, who waived it, and why. Empty unless a maintainer said so in writing.

**Sign-off**

| | name | date | verdict |
|---|---|---|---|
| Tester | | | pass / fail |
```

## Before you send — the redaction gate

**The report goes to a public issue tracker.** Claude reads the finished file top to bottom before
offering any transport, and removes:

- API keys, tokens, and the *contents* of any `key_file` — the file **path** may stay, its bytes may
  not;
- LAN hostnames, internal IPs and personal endpoints — a `lan` backend becomes
  `lan (endpoint redacted)`;
- home-directory paths carrying a real name — `/home/alice/...` becomes `~/...`;
- anything the tester dictated as test speech that is not test speech.

Then show the tester the final file — path and content — and ask for explicit confirmation before
filing. Nothing is published without that yes.

## Delivery

The transports are the ones `/report-bug` uses; when that skill is present, call it and hand it the
report as the body rather than reimplementing the mechanics here. Until then these three are the
whole list, and only the first two are wired.

**A — `gh`, the wired default.** The body is passed as a **file**, never interpolated into the
command line, so no shell metacharacter in the tester's evidence can reach the shell:

```sh
gh issue create --repo saharkit/windowsill \
  --title "conformance: voice-loop <version> on <os> — <n> PASS / <n> FAIL / <n> SKIP" \
  --label conformance \
  --body-file ~/.local/state/voice-loop/conformance/conformance-<version>-<date>.md
```

If that fails because the `conformance` label does not exist in the repository, **retry once without
`--label`** and say so in the reply — creating a label is the maintainer's, not the tester's. Report
the issue URL back to the tester.

**B — a pre-filled URL, when `gh` is absent or unauthenticated.** Build
`https://github.com/saharkit/windowsill/issues/new` with `title`, `body` and `labels=conformance` as
percent-encoded query parameters, and give the tester the link. Browsers and servers both cap URL
length: if the encoded URL exceeds roughly 6 KB — a full pass usually will — open the plain
`issues/new` page instead and tell the tester the file path to paste from. Do not silently truncate
the report to make it fit.

**C — email to a service address. Not wired.** `saharkit.com` has **no MX record**, so there is no
mailbox to receive it and no one designated to triage it. Standing up MX plus a service address, and
naming who reads it, is maintainer-hands and is tracked on the kit's operator-setup ledger. Until
that lands, **do not offer a `mailto:` transport** — offering a channel that drops mail is worse than
having one channel. GitHub is the only wired route.

Whichever transport is used, the report file stays on the tester's machine. Delivery is a copy, not a
move.

## What a pass means

A release is conformant when a full run on a clean machine produces a report with a verdict in every
row, no unwaived FAIL, and every SKIP carrying a reason. That report — filed as an issue, labelled
`conformance` — is the record. It is not a substitute for the CI gates in
[TESTING.md](TESTING.md); it covers precisely what those gates structurally cannot reach: the hotkey,
the microphone, the paste into a real window, the consent dialog, and whether a human actually heard
it.
