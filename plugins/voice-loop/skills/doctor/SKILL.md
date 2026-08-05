---
name: doctor
description: Three-bin diagnosis for voice-loop — is it a config choice working as chosen, an unfinished install, or a real bug? Runs only when asked; offered (never auto-run) when Claude observes repeated failures in-session.
argument-hint: "[what is not working — a sentence or two]"
allowed-tools: [Bash, Read, Edit, AskUserQuestion]
---

# /doctor — what is actually wrong?

A three-bin diagnosis skill. It does not fix anything without asking, and it
runs only when you invoke it. If the assistant keeps failing at the same thing
in-session, it may OFFER to run `/doctor` — but it never runs it by itself.

## The three bins (in order)

1. **Consequence of your choice** — a config setting works exactly as chosen,
   and that choice explains the behaviour you are seeing. Example: `auto_paste:
   false` means "you chose manual paste — that is why text does not insert
   itself."
2. **Unfinished install** — the step ledger from `/voice-setup` (#48) shows
   that one or more install steps never completed. An interrupted install is
   the most common cause of "it worked before and now it does not."
3. **Real anomaly** — the first two bins are ruled out, and the logs show a
   genuine failure. At this point `/doctor` hands off to `/report-bug` (#55)
   with the evidence already gathered.

Every finding is shown, explained, and **proposed** — nothing is changed
without your explicit yes.

## Entry — collect the raw data

Run the thin `doctor.py` script.  It reads everything it needs (config,
install ledger, log tails), loads the check manifest from the skill directory,
imports the engine from `sill-core`, and prints one JSON object to stdout:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"
```

If you need to override the default paths (e.g. in a test or a non-standard
install):

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" \
  --state-home "${XDG_STATE_HOME:-$HOME/.local/state}" \
  --config-path "${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop/config.json"
```

Parse the JSON output with `jq` or a python one-liner.  The top-level fields:

- `config_present` — `true`/`false`; say "no config found at `<path>`" when false
- `config_path` — where the config was looked for
- `ledger_state` — `"none"`, `"in_progress"`, `"complete"`, or `"cancelled"`
- `findings` — a list of finding objects, each with `bin`, `key`, `title`,
  `explanation`, `fix`, `offer_flip`, `flip_path`, `flip_value`, and
  `evidence`

If `config_present` is false, state that fact before presenting the findings.

## Step 1 — present the findings

Walk through the findings **in order**, grouped by bin:

### If there are NO findings

The diagnosis found nothing wrong.  Say:

> The three-bin diagnosis found nothing — the config, the install ledger, and
> the recent log tails all look healthy.  If something is still not working,
> tell me more about what you are seeing and I can run targeted checks, or
> run `/report-bug` to collect a full bundle for the maintainers.

Do not improvise beyond that.

### Consequence-of-choice findings

Present each one:

> **<title>** — <explanation>
>
> Fix: <fix>

If `offer_flip` is true, ask **one** AskUserQuestion:

> Apply this change? → **Yes**, flip `<flip_path>` to `<flip_value>`.  **No**, keep it as-is.

If yes, edit `~/.config/voice-loop/config.json` to set `<flip_path>` to
`<flip_value>`.  Use `Read` + `Edit` — never `sed -i`.  After the edit, say
what changed and that the fix takes effect on the next dictation / speak-back.

If the fix is the only change and the user declines, say "kept as-is" and move
on.  Never argue with a no — the user chose this setting once, and they may
have a reason you have not considered.

### Unfinished-install findings

Present the finding, then offer to run `/voice-setup`:

> **<title>** — <explanation>
>
> The install ledger is the ground truth for what has been done.  Run
> `/voice-setup` to resume from the last completed step?

If yes, hand off to `/voice-setup` — do not re-implement any of its steps
here.  The install skill already knows how to detect the interrupted state
and offer resume / restart / cancel.

### Real-anomaly findings

Present each one, then offer the handoff at the end:

> **<title>** — <explanation>
>
> Fix: <fix>

After all real-anomaly findings, offer to escalate:

> One or more real anomalies were found.  Run `/report-bug` with these
> evidence items to collect a full redacted bundle and file it?

If yes, run `/report-bug` with the `--summary` populated from the anomaly
findings' titles and explanations.  The evidence dicts travel with the
handoff — mention them in the summary so the maintainer gets the full context.

If no, say where the findings were and that the user can run `/report-bug`
later.

## Step 2 — summary

After all findings have been presented (and any flips applied), print a short
summary:

> Checked: config (<path>), install ledger (<state>, <N>/<total> steps done),
> log tails.  <N> findings: <count> config choices, <count> incomplete steps,
> <count> anomalies.

If nothing was found, say nothing was found.

## Rules

1. **Dormant by default.** This skill only runs on explicit `/doctor`.  The
   assistant may OFFER it once per session when it observes ≥3 failures of
   the same kind (the same error message, the same failing tool) — but it
   never auto-runs.
2. **Offer throttling.** Use `AssistantState` from `sill-core` to record
   that the offer was made (`store.reminder_mark_shown("doctor-offer")`).
   Do not offer again for the same session unless the user says "doctor."
3. **Reader, not fixer.** Every mutation is proposed, shown, and
   consent-gated.  The only exception is the config flip for
   consequence-of-choice findings — and even those require an explicit yes.
4. **The engine is plugin-agnostic.** `sill_core.diagnosis` knows nothing
   about voice-loop.  Every check is declared in the manifest.  A second
   plugin can add a `/doctor` skill by writing its own manifest — the engine
   needs no changes.
5. **Handoff, not reimplementation.**  The install bin hands off to
   `/voice-setup`; the anomaly bin hands off to `/report-bug`.  Neither
   flow is re-done here.
