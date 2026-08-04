---
name: report-bug
description: Collect a redacted voice-loop diagnostics bundle (versions, config, log tails, server health, recent job states), show the user the exact bytes, ask for explicit consent, and only then file it — as a GitHub issue via the gh CLI, as a pre-filled new-issue URL, or as a mailto. Use when voice-loop misbehaves and the user asks to report a bug, file an issue, or send diagnostics about dictation or speak-back.
argument-hint: "[a sentence about what went wrong]"
allowed-tools: [Bash, Read, AskUserQuestion]
---

# report-bug — assemble the evidence, then ask

Something in the voice contour misbehaved. Your job is to do the log archaeology **for** the user and
to hand them one bundle they can read in full before any of it leaves the machine.

The collector is `${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh`. It does the collecting and the
redacting; you do the showing, the asking and the sending. **Never** hand-assemble a report by
reading logs yourself — the redaction rules live in the collector and only there.

## The four steps, in order, no skipping

### 1. Collect

Take the user's own words for what went wrong (the skill argument, or ask in one line), then:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh" collect --summary "<their words, verbatim>"
```

One bundle file lands in the state dir (`~/.local/state/voice-loop/bug-report-<stamp>.md`) and the
**exact same bytes** go to stdout. The path is on stderr, deliberately: stdout is the bundle and
nothing else.

It gathers plugin and server versions, OS/hardware class, the config with secrets stripped, the tails
of `dictate.log` and `speak.log`, `/health` for whichever endpoints the config names, the state-dir
file sizes and ages, and the last 20 job states.

### 2. Redaction — say what it did, do not re-do it

The collector already removed: keys and tokens (by shape and by config key name), the username and
home paths, hostnames other than loopback, and **all transcript and spoken text**. A log line that
carries speech keeps its event and its character count — `transcript: <redacted 30 chars>` — because
the length is a diagnostic and the words are not.

Two things are worth telling the user in one sentence each, because they are the bundle's honest
limits:

- **third-party output is withheld whole** (`<tool output, N chars, withheld>`): those lines are a
  recorder's or player's stderr appended to the same log, and a `stt.command` engine can print a
  transcript there — unclassifiable, so it does not travel;
- **an unrecognised line is cut, not trusted** — if the bundle ends with a note about unclassified
  lines, this machine's scripts are a different version from its collector.

If the user wants something back that was redacted, they can add it themselves in step 4. Do not
edit the bundle for them, and never paste a redacted value back into it.

### 3. Show — the whole thing, in chat

Print the bundle to the user **verbatim**, in a fenced block. Not a summary, not "the interesting
parts": the point of this step is that they see every byte that could leave. Then say, in one line,
how long it is and where the file is.

If it is very long, still show all of it — a report they did not read is a report they did not
consent to.

### 4. Ask — explicitly, with the destination named

Check what this machine has:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh" transports
```

Then ask **one** AskUserQuestion whose options are only the available transports plus "don't send".
Name the destination in the option text — "a **public** issue at github.com/saharkit/windowsill" is
the phrase, because a public issue is exactly what it is.

A "yes" to sending is a yes to **this** bundle, once. Re-collecting means asking again.

## The three transports

**gh CLI, authenticated** — the primary path. Triage lives where the fixes live:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh" gh \
  --title "voice-loop: <short symptom>" --bundle "<bundle path>"
```

It prints the issue URL. **This is the only subcommand that sends anything** — run it only after an
explicit yes.

**A GitHub account but no CLI** — print a pre-filled new-issue URL and let them press Submit:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh" url \
  --title "voice-loop: <short symptom>" --bundle "<bundle path>"
```

Give them the URL to open themselves. The body is trimmed to fit a browser address bar and says so
where it was cut; the full bundle is the file on disk. Nothing is sent until they submit the form —
say that, it is what makes this tier easy to accept.

**No GitHub at all** — a mailto: to the project's intake mailbox, which the maintainer forwards into
an issue:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/report-bug.sh" mailto \
  --title "voice-loop: <short symptom>" --bundle "<bundle path>"
```

**This tier is not live yet** and `transports` will say so: the project's domain publishes no MX
record, so there is no mailbox to receive it and a message sent there would vanish looking like
success. Until that changes, the honest offer is: leave the bundle on disk and give them the
pre-filled URL, or the repository's issue page to paste the bundle into by hand. An operator who
does have a mailbox points the collector at it with `VOICE_LOOP_REPORT_MAILBOX=<address>`; a mailto
body is trimmed harder than a URL one (2 KiB is the smallest handler limit), so the file stays on
disk for them to attach.

## Rules

1. **Nothing is sent before an explicit yes**, and the yes must have heard the destination.
2. **One bundle, one artifact.** What you show, what is on disk, and what is sent are the same
   bytes. Never assemble a "fuller" version for the maintainer.
3. **Do not read the logs yourself** and do not quote them outside the bundle — the redaction is in
   the collector, and a hand-quoted line has been through none of it.
4. **A failing transport is not a reason to try another one silently.** Report what failed, then ask.
5. If the user declines, say where the bundle file is and stop. It is theirs; it never expires and it
   never leaves.
