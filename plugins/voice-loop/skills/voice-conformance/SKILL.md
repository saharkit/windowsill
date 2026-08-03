---
name: voice-conformance
description: Run the voice-loop conformance (acceptance) pass end to end — walk the numbered checklist in the plugin's CONFORMANCE.md, run every machine-checkable row yourself, ask the human only for the physical acts (press the hotkey, confirm you heard it), record a PASS/FAIL/SKIP verdict with evidence for every row, write one report file stamped with the plugin version, and offer to file it as a labelled GitHub issue. Use when the user asks to run the conformance pass, the acceptance test, the release checklist, or to sign off a voice-loop release.
argument-hint: "[section number to run only that section]"
allowed-tools: [Bash, Read, Write, AskUserQuestion]
---

# voice-conformance — run the acceptance pass and file one report

The checklist is **data, and it lives in one place**: `${CLAUDE_PLUGIN_ROOT}/CONFORMANCE.md`. Read it
first, every time. Do not work from a copy of the rows in this file — there is none, deliberately, so
the checklist can never drift from the thing that executes it.

Your job is the execution: metadata, then the rows in order, then one report, then delivery.

## Step 0 — read the checklist and stamp the run

```sh
jq -r .version "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json"; \
git -C "${CLAUDE_PLUGIN_ROOT}" rev-parse --short HEAD 2>/dev/null || echo "installed (no checkout)"; \
date -u +%F; uname -sr; sw_vers 2>/dev/null | tr '\n' ' '; \
echo "XDG_SESSION_TYPE=$XDG_SESSION_TYPE XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP"; \
jq . ~/.config/voice-loop/config.json 2>/dev/null || echo "no config yet"
```

Then read `CONFORMANCE.md` and ask the human the two things the machine cannot know: **who they are**
(a name or handle they are willing to see in a public issue) and whether this machine is **clean**
(never had voice-loop) or **reused**. On a reused machine, section 1 is `SKIP` with that reason —
say so up front rather than at the end.

Announce the plan in three or four lines: how many sections apply, which ones you already expect to
skip (not macOS, no cloud key, uninstall blocked on #17), and roughly how much of their attention
you will need. Then start.

## Step 1 — walk the rows

The protocol is in `CONFORMANCE.md` under *How Claude executes this*; it governs, and these are the
three rules that get broken first:

- **Evidence or it did not pass.** Every verdict cell has a companion evidence cell, and "looks fine"
  is not evidence. Paste the exit status, the log line, or the tester's own words.
- **Ask only for physical acts.** Anything runnable, you run. One instruction at a time — press the
  hotkey, say a sentence, tell me what appeared — never a wall of ten.
- **A FAIL does not stop the pass, and you do not repair the machine mid-run.** Capture the
  diagnostic, note the fix as a follow-up, move to the next row.

Batch the machine-checkable rows of a section into one Bash call so the tester sees a handful of
permission prompts, not fifty. Keep a running table as you go and show the section's verdicts when it
closes — a tester who has to wait until the end to see anything has no way to tell you a row was
misread.

If the user named a section number as an argument, run only that section, and say plainly in the
report that it is a **partial** pass: a partial run is a useful diagnostic and is never a release
sign-off.

## Step 2 — write the report

One file, outside the repository (this repo keeps no generated artifacts):

```
~/.local/state/voice-loop/conformance/conformance-<version>-<date>.md
```

`<version>` from `plugin.json` at Step 0, `<date>` from `date -u +%F`. If that path already exists,
append `-2` (then `-3`) rather than overwriting, and say which run it was.

Mirror `CONFORMANCE.md`'s shape with the cells filled, keeping every row id byte-identical so two
passes diff cleanly. Before writing a cell, flatten it: newlines to spaces, `|` to `\|`, and anything
longer than two lines truncated in the cell with the full text placed in a fenced block under the
section's table. An unescaped pipe silently eats the row it was supposed to document.

Then count. `PASS + FAIL + SKIP` must equal the number of rows in the sections you ran — if it does
not, a row was missed, and the fix is to run it, not to adjust the total.

## Step 3 — redact, confirm, deliver

**The report goes to a public tracker.** Read the finished file top to bottom and strip what
`CONFORMANCE.md`'s *Before you send* section lists: key material, LAN hostnames and internal
endpoints, home paths carrying a real name. Then show the tester the path and the content and ask
for an explicit yes. Nothing is filed without it.

Delivery is `CONFORMANCE.md`'s *Delivery* section, and it is short: if a `/report-bug` skill is
installed, hand it the report and let it own the transport; otherwise `gh issue create --body-file`
(never the body on the command line), falling back to a pre-filled `issues/new` URL when `gh` is
missing or unauthenticated, and never offering the `mailto:` route — there is no MX for it yet.

If `gh` rejects the `--label conformance` because the label does not exist, retry once without it and
say so: creating a label is maintainer-hands.

## Step 4 — report to the tester

Close with the counts, every FAIL by id in one line each, the report's path, and the issue URL if one
was filed. If there is an unwaived FAIL, say the plain thing — **this build is not conformant** — and
do not soften it. If a section was skipped, name it and its reason. A conformance pass whose summary
has to be reconstructed from the conversation defeats the point of having an artifact.
