---
name: conformance
description: Run the voice-loop conformance pass — walk the versioned acceptance checklist interactively, fill every verdict, and emit ONE structured report. The report is then offered through the same three transports as /report-bug (GitHub issue with the conformance label, pre-filled URL, or mailto). Use when the tester says "run the conformance pass", "conformance", "acceptance test", or "validate the release".
argument-hint: "[--section install|dictation|speak-back|degrade|uninstall]"
allowed-tools: [Bash, Read, Write, AskUserQuestion, Glob]
---

# conformance — run the acceptance checklist

You are executing a versioned conformance pass for the voice-loop plugin. The checklist
lives at `${CLAUDE_PLUGIN_ROOT}/CONFORMANCE.md` — read it first. Every row must have a
verdict (PASS / FAIL / SKIP) and evidence before this pass ends.

## Operating rules

1. **Read the checklist first.** Open CONFORMANCE.md and note the version pinned at the
   bottom — it must match the plugin version in `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`.
   If they disagree, stop and report the mismatch: a stale checklist must not be used.
2. **Walk the rows in order**, but batch the probes. Ask the human only for the physical
   acts — tap the hotkey, confirm you heard the sound, switch windows. Everything else
   you probe from the machine yourself.
3. **One row at a time, but batch the questions.** When several rows in a row need the
   human to do something, ask once for the cluster: "now do 3.1, 3.2 and 3.3 — speak-back
   checks — and tell me what you saw."
4. **A row left empty is a FAIL.** Before writing the report, check every row has a
   verdict. A row that cannot be run on this machine (e.g. a Wayland-only guard tested
   on X11) gets SKIP with the reason in the evidence cell.
5. **The report is ONE file** — `conformance-v<version>-<YYYYMMDD>.md` in the current
   working directory. It is the completed checklist: the environment table filled, every
   verdict cell populated, every evidence cell carrying at least one sentence. No summary
   separate from the rows — the rows ARE the summary.
6. **Never guess a verdict.** If you cannot determine the outcome (the human is unsure,
   a log is ambiguous), mark it FAIL with "could not determine — <reason>". A guested
   PASS is worse than an honest FAIL.

## Step 0 — version check and environment probe

Read the version from the plugin manifest and from CONFORMANCE.md. If they disagree, stop.

Then probe the environment and fill the environment table:

```sh
echo "OS: $(uname -s) $(uname -m)"; echo "session: ${XDG_SESSION_TYPE:-unknown} ${XDG_CURRENT_DESKTOP:-unknown}"; echo "python: $(python3 --version 2>&1)"; python3 -c "
import sys, json; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from report_bug import redact_value
cfg_path = '${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop/config.json'
try:
    with open(cfg_path) as fh: cfg = json.load(fh)
    print(json.dumps(redact_value(cfg), indent=2, ensure_ascii=False))
except Exception as e:
    print(f'config unreadable: {type(e).__name__}')
" 2>/dev/null || echo "no config found"
```

The config output above is already redacted — keys, tokens, usernames and hostnames are
replaced with placeholders. From the redacted config, extract the backends and language.
Read the plugin version from `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`.
Ask the human for their name (for the tester field).

## Step 1 — the install section (rows 1.1–1.12)

These rows verify the install-from-scratch contract. On a machine that already has
voice-loop installed, most of these are already proven: probe what you can, and for
the rest ask the human whether the behaviour held when they installed.

- 1.1–1.4: probe the current state — is the marketplace added? Is the plugin installed?
  Is the Stop hook registered? Is `/voice-setup` listed?
- 1.5–1.9: ask the human — these are about the interactive install experience.
- 1.10–1.12: probe the config and run `selftest.sh`.

Probe commands:

```sh
# 1.1 — marketplace
test -f .claude-plugin/marketplace.json 2>/dev/null && echo "marketplace file present" || echo "no marketplace file"

# 1.2 — plugin installed
python3 -c "import json; d=json.load(open('${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json')); print(d.get('name',''), d.get('version',''))"

# 1.3 — hooks registered
python3 -c "import json; d=json.load(open('${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json')); print('Stop' in d.get('hooks',{}))"

# 1.10 — config parses
jq . "${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop/config.json" >/dev/null 2>&1 && echo "config valid JSON" || echo "config invalid or absent"

# 1.11 — no inline secrets
python3 -c "
import json, sys
cfg = json.load(open('${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop/config.json'))
def has_inline_key(node, path=''):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ('api_key', 'token', 'secret', 'password') and isinstance(v, str) and len(v) > 4:
                print(f'INLINE KEY at {path}.{k}'); return True
            if has_inline_key(v, f'{path}.{k}'):
                return True
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if has_inline_key(v, f'{path}[{i}]'):
                return True
    return False
found = has_inline_key(cfg)
if found:
    print('FAIL: inline secrets found in config — fix before filing')
else:
    print('PASS: no inline secrets')
"

# 1.12 — selftest
bash "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.sh" 2>&1; echo "exit: $?"
```

## Step 2 — the dictation section (rows 2.1–2.10)

2.1 is a machine probe (selftest). Run it.

2.2–2.10 need the human at the keyboard. Cluster them:
- First cluster (2.2–2.4): basic dictation, send mode, long dictation
- Second cluster (2.5–2.6): debounce — hold the key, then quick re-tap
- Third cluster (2.7–2.10): paste-at-focus and same-window guard

For each cluster, tell the human what to do, ask for the outcome, and fill the rows.
After the human reports, check `dictate.log` for corroborating evidence:

```sh
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from report_bug import read_log_tail
state = '${XDG_STATE_HOME:-$HOME/.local/state}/voice-loop'
tail = read_log_tail(f'{state}/dictate.log', 30)
for line in tail['lines']:
    print(line)
if not tail['present']:
    print('dictate.log absent')
elif tail.get('unclassified', 0):
    print(f'({tail[\"unclassified\"]} unclassified lines — redacted)')
"
```

## Step 3 — the speak-back section (rows 3.1–3.13)

3.1–3.4 need the human to listen. Ask them to:
1. Send a prompt that will produce a 🔊 reply (3.1)
2. Confirm unmarked lines were silent (3.2 — same turn)
3. Send two quick turns in a row and confirm dedup (3.3)
4. Send a prompt while audio is playing to confirm no overlap (3.4)

3.5–3.6: ask for a multi-sentence 🔊 line, then probe the log:

```sh
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from report_bug import read_log_tail
state = '${XDG_STATE_HOME:-$HOME/.local/state}/voice-loop'
tail = read_log_tail(f'{state}/speak.log', 60)
for line in tail['lines']:
    if 'played rc=0' in line or 'timings ' in line:
        print(line)
"
```

3.7–3.10 (eager mode): these need config changes. Warn the human before touching
their config — these are the most invasive checks. Offer to skip the eager-mode
cluster and mark those rows SKIP with "eager mode not tested in this pass" if they
decline. If they accept, edit `speak.eager: true` in the config, run the checks,
then restore the original value.

3.11–3.13: probe the logs for the queued-not-dropped contract and unheard-line
accountability:

```sh
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from report_bug import read_log_tail
state = '${XDG_STATE_HOME:-$HOME/.local/state}/voice-loop'
tail = read_log_tail(f'{state}/speak.log', 60)
for line in tail['lines']:
    if 'queued' in line or 'gave up' in line or 'nothing played' in line:
        print(line)
"
```

## Step 4 — the degrade-paths section (rows 4.1–4.8)

These are destructive — they break the running contour. **Ask once for the whole
section** before touching anything: "The degrade-path checks will stop your speech
server, break your cloud key, and generally make a mess for a few minutes. Everything
will be back to normal after. Run them?"

If the human declines, mark all eight rows SKIP with "degrade paths not tested in
this pass — tester declined the destructive checks."

If they accept:

- 4.1–4.3 (server stopped): stop the server, run selftest, trigger speak-back,
  trigger dictation, check the logs, then restart the server.
- 4.4 (wrong key): if the config uses a cloud backend, temporarily point `key_file`
  at a deliberately wrong file. Restore after.
- 4.5–4.8: probe what you can without actually breaking things (e.g. query the
  server for an unsupported language); for the rest, read the code paths to confirm
  they are exercised by CI and mark with evidence referencing the CI job.

## Step 5 — the uninstall section (rows 5.1–5.12)

Check whether #17 (the `/voice-remove` skill) is shipped. Look for the skill file:

```sh
test -f "${CLAUDE_PLUGIN_ROOT}/skills/voice-remove/SKILL.md" && echo "voice-remove skill present" || echo "voice-remove skill absent"
```

If absent: mark all rows in section 5 as SKIP with "uninstall not yet shipped
([#17](https://github.com/saharkit/windowsill/issues/17))".

If present: ask the human whether they want to run the uninstall checks — these
are the LAST checks because they end the install. If they decline, mark SKIP.

## Report assembly

Once every row has a verdict, assemble the report:

1. Copy the CONFORMANCE.md template.
2. Replace the environment table placeholders with the actual values from Step 0.
3. Fill every verdict cell with PASS, FAIL, or SKIP.
4. Fill every evidence cell with at least one sentence — what was observed, what
   the log showed, what the human reported.
5. Write the completed report to `conformance-v<version>-<YYYYMMDD>.md` in the
   current working directory.
6. **Redact the report before it leaves the machine.** The evidence cells were
   filled from probe output that was already redacted, but the report body as a
   whole runs through the same redaction as `/report-bug` as a safety net:

```sh
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from report_bug import redact
path = 'conformance-v<version>-<YYYYMMDD>.md'
body = open(path).read()
redacted = redact(body)
open(path, 'w', newline='\n').write(redacted)
"
```

## Filing — the three transports (same as /report-bug)

The completed report is now a redacted file on disk. Show it to the tester
so they can review exactly what will be published — every byte of it. Offer the
same three transports `/report-bug` offers. Run
`${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh transports` to see which are
available, then ask **one** AskUserQuestion:

> The redacted report is shown above. File it? Options: **GitHub issue** (public,
> with the `conformance` label — the primary path), **pre-filled URL** (you press
> Submit yourself — nothing is sent until you do), or **don't file** (the report
> stays on disk).

### gh transport (primary)

Uses `gh issue create` with the `conformance` label:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh" gh \
  --label conformance \
  --title "conformance: voice-loop v<version> (<date>)" \
  --bundle "<path to report>"
```

Only if `gh` is authenticated. Print the issue URL. **Never send without an
explicit yes** — that is the same consent rule as `/report-bug`.

### URL transport (fallback)

A pre-filled new-issue URL — nothing is sent until the human presses Submit.
Build it with a one-liner that reuses the URL builder the collector already has:

```sh
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from report_bug import artifact_from_body, issue_url
# The label goes into the body as a markdown comment so triage can add it —
# GitHub's new-issue form does not accept ?labels= in the query string
body = open('${REPORT_PATH}').read()
artifact = artifact_from_body('conformance: voice-loop v<version> (<date>)', '<!-- conformance -->\n' + body)
print(issue_url(artifact))
"
```

Give the human the URL to open themselves. Say plainly: "nothing is sent until
you press Submit."

### mailto transport

Available and addressed to `reports@saharkit.com` (a Google Group). If the human has set
`VOICE_LOOP_REPORT_MAILBOX`, use `report_bug.mailto_url` with that address.

### Don't file

The report stays on disk at the path you wrote it to. Say where, and that
it never expires.

## Rules

1. **Nothing is sent before an explicit yes**, and the yes must have seen the
   redacted report body and heard the destination — same consent rule as `/report-bug`.
   The tester must be able to review every byte that will be published before they
   consent.
2. **One report, one artifact.** The file on disk is exactly what is sent.
3. **A row without a verdict is a FAIL.** Check before writing the report.
4. **The checklist version must match the plugin version.** Check before anything
   else.
5. **SKIP is not FAIL.** A row inapplicable to this machine (wrong OS, missing
   hardware, feature not yet shipped) is SKIP with a reason — it is not a
   failure and does not block the pass.
6. **A failed transport is not a reason to try another silently.** Report what
   failed, then ask.
7. If the human declines to file, say where the report file is and stop.
