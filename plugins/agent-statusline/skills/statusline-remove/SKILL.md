---
name: statusline-remove
description: Remove the agent-statusline status line — delete only the statusLine key from ~/.claude/settings.json, leave every other key untouched, and delete ~/.claude/tools/agent-statusline.js. Use when the user asks to remove, uninstall or disable the status line.
allowed-tools: [Bash, Read, Edit, AskUserQuestion]
---

# statusline-remove — uninstall the agent-statusline status line

You are removing `agent-statusline`: the renderer file at `~/.claude/tools/agent-statusline.js`,
and the `statusLine` key from `~/.claude/settings.json`. Nothing else changes.

## Operating rules

1. **Confirm first.** Print the two things you will remove and ask the user to confirm before
   doing either one.
2. **Touch only `statusLine`.** Every other key in `~/.claude/settings.json` keeps its value
   and its relative order; the file's overall indentation is rewritten to jq's canonical form
   (two-space indent, one trailing newline). The key is deleted, not emptied.
3. **Do not delete `~/.claude/settings.json.bak`.** That is the user's own rollback file, made
   by `/statusline-setup` if `statusLine` was overwritten. Whether to keep it is the user's
   decision, not this skill's.
4. **POSIX-only paths.** Mirrors `statusline-setup` — the install paths are POSIX, so the
   removal paths are too.

## Step 1 — confirm scope

Tell the user, in order:

- the `statusLine` key in `~/.claude/settings.json` will be deleted (every other key stays);
- `~/.claude/tools/agent-statusline.js` will be deleted;
- `~/.claude/settings.json.bak`, if present, is left alone.

Ask **once** for confirmation. Proceed on yes; print "nothing was removed" and stop on no.

## Step 2 — read the existing settings

```sh
[ -f ~/.claude/settings.json ] || { echo "no settings file to clean up"; exit 0; }
```

If the file is absent, only the renderer file at `~/.claude/tools/agent-statusline.js` remains
to clean up — proceed to Step 4.

If the file exists but does not parse, **stop**. Print the parse error and the file path. Do
not edit a file you cannot read. The user decides how to repair it before this skill proceeds.

If the file exists and parses but has no `statusLine` key, say so and proceed to Step 4
(renderer removal only).

## Step 3 — remove the `statusLine` key

Read the JSON, delete the `statusLine` key, write the rest back. The file is rewritten with
jq's canonical two-space indent and one trailing newline. **Every other key and its value
survive**, in their original order.

A read-modify-write is the right tool here — `jq` deleting one key and writing the result back
preserves every other key and their values, including nested ones.

```sh
jq 'del(.statusLine)' ~/.claude/settings.json > ~/.claude/settings.json.tmp
mv ~/.claude/settings.json.tmp ~/.claude/settings.json
```

`jq` is the right tool for this — it preserves every other key, every nested value, and the
key order; what it does NOT preserve is the source file's indentation, since jq always emits
its canonical two-space form. If the user's `~/.claude/settings.json` was hand-formatted with
a different indent, jq will normalize it; that is the trade for the rest of the file staying
intact. (On a host with no `jq`, the equivalent read-modify-write by hand is acceptable; the
contract is "only `statusLine` is touched" — meaning its key and its value, not its bytes.)

## Step 4 — remove the renderer file

```sh
rm -f ~/.claude/tools/agent-statusline.js
```

`rm -f` is intentional: if the file does not exist (a partial previous uninstall), this is a
no-op rather than a failure. Do not error in that case.

## Closing

Report in one paragraph: that the `statusLine` key was deleted (or was already absent), that
`~/.claude/tools/agent-statusline.js` was deleted (or was already absent), and that no other
file was touched. Mention `~/.claude/settings.json.bak` is still where it was, in case the
user wants it.

The plugin directory itself stays installed — `/statusline-remove` does not call
`/plugin uninstall`. That is a separate command.
