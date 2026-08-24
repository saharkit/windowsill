---
name: statusline-setup
description: Install the agent-statusline status line for Claude Code — copy the renderer to ~/.claude/tools/, write the statusLine key into ~/.claude/settings.json preserving every other key, and prove it works by rendering the bundled fixture. Use when the user asks to set up, install or enable a status line, or to show context use, weekly spend or the model name in Claude Code.
allowed-tools: [Bash, Read, Write, Edit, AskUserQuestion]
---

# statusline-setup — install the agent-statusline status line

You are installing `agent-statusline`: one line at the bottom of every Claude Code turn that
shows context use, weekly spend and the active model. Nothing host-specific — every figure is
read from the payload the harness pipes to the renderer on stdin.

The install has three steps: copy the renderer to `~/.claude/tools/`, write the `statusLine`
key into `~/.claude/settings.json` (preserving every other key), and prove the result works
by piping the bundled fixture through the installed renderer.

The renderer lives at `${CLAUDE_PLUGIN_ROOT}/statusline.js`.

## Operating rules

1. **POSIX-only paths.** The script's installation paths assume `~/.claude/` and `~/.claude/tools/`.
   On Windows the file lives somewhere else and the setup skill is not the place to bridge that —
   this is a POSIX path plugin and the README says so.
2. **`node <path>`, never a bare path.** The `statusLine.command` value is `node <path>`, not
   `<path>` — no shebang and no executable bit needed. The file is invoked by node, period.
3. **Preserve every existing key in `~/.claude/settings.json`.** The install is a single-key
   merge — no other key is touched, reordered or rewritten. Keys appear in their existing order;
   `statusLine` is appended if it is new.
4. **No network, no model, no host state read.** The renderer reads the stdin payload only. The
   plugin sends nothing anywhere and writes no snapshot of that payload to disk.
5. **Back up before overwrite.** When `statusLine` already exists, take `~/.claude/settings.json.bak`
   before any change — and ask the user to confirm first.

## Step 1 — copy the renderer

```sh
mkdir -p ~/.claude/tools
cp "${CLAUDE_PLUGIN_ROOT}/statusline.js" ~/.claude/tools/agent-statusline.js
```

`~/.claude/tools/` is created if absent. Any existing `~/.claude/tools/agent-statusline.js` is
overwritten — that is the upgrade path, and the renderer is byte-stable for a given version.

Why the copy is **outside** the plugin root: upgrading or uninstalling the plugin must not leave
`~/.claude/settings.json` pointing at a file that no longer exists.

## Step 2 — write the `statusLine` key

The renderer path written into `~/.claude/settings.json` is the absolute path of the copied
file, with `$HOME` expanded to its absolute form (e.g. `/home/<user>/.claude/tools/agent-statusline.js`).

The full `statusLine` value is:

```json
{
  "type": "command",
  "command": "node /home/<user>/.claude/tools/agent-statusline.js"
}
```

### If `~/.claude/settings.json` does not exist

Create it with `statusLine` as its only key. Two-space indent. One trailing newline.

```sh
set -e
PATH_VALUE="$HOME/.claude/tools/agent-statusline.js"
cat > ~/.claude/settings.json <<JSON
{
  "statusLine": {
    "type": "command",
    "command": "node $PATH_VALUE"
  }
}
JSON
```

### If `~/.claude/settings.json` exists but does not parse

**Stop.** Print the parse error and the file path. Write nothing. The user decides how to repair
the existing file before this skill can do its job.

### If `~/.claude/settings.json` exists and parses

Read it. Preserve every existing key and their order. Two-space indent. One trailing newline.

**If no `statusLine` key is present:** append `statusLine` to the existing object, after the
last existing key, preserving the rest verbatim.

**If a `statusLine` key is already present:** print the existing value to the user, name what the
new value would replace it with, and **ask the user to confirm before overwriting**. On
confirmation:

```sh
cp ~/.claude/settings.json ~/.claude/settings.json.bak
```

then write the merged content back, with `statusLine` updated to the renderer-form above. On
**refusal**: change nothing, remove the copied script from `~/.claude/tools/`, and say so.

(The `.bak` file is the user's own rollback. `/statusline-remove` does NOT delete it — that is
the user's decision to make.)

## Step 3 — prove it worked

Mirror the `voice-setup` ending: render the bundled fixture through the installed renderer,
show the rendered line, and confirm to the user.

```sh
cat "${CLAUDE_PLUGIN_ROOT}/fixtures/payload-pro.json" | node "$HOME/.claude/tools/agent-statusline.js"
```

The line should read `◐ ctx 84k/200k (42%) wk 63%/5h 15% Opus 5` (with colour escapes the
terminal will render). If it does not, the install did not take and the next step is to read
`~/.claude/settings.json` and the file at `~/.claude/tools/agent-statusline.js` — do not claim a
green install until the fixture line is in front of you.

## Closing

Report in one paragraph: the absolute path the renderer was copied to, the merged settings
content (with `statusLine` highlighted), and the rendered fixture line. Name the way back out:
`/statusline-remove` deletes the `statusLine` key and the copied renderer, and leaves every
other settings key untouched.
