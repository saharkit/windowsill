---
name: statusline-setup
description: Install this plugin's agent-statusline status line for Claude Code. Use only when the user explicitly asks to install agent-statusline by name or invokes /statusline-setup.
allowed-tools: [Read, Edit(~/.claude/tools/agent-statusline.js), Edit(~/.claude/settings.json), AskUserQuestion]
---

# statusline-setup — install the agent-statusline status line

You are installing `agent-statusline`: one line at the bottom of every Claude Code turn that
shows context use, weekly spend and the active model. The renderer reads the payload on stdin.

The install has three steps: copy the renderer to `~/.claude/tools/`, update the
`statusLine` key in `~/.claude/settings.json`, and prove the result with the bundled fixture.

## Step 1 — copy the renderer

```sh
mkdir -p ~/.claude/tools
cp "${CLAUDE_PLUGIN_ROOT}/statusline.js" ~/.claude/tools/agent-statusline.js
```

## Step 2 — write the `statusLine` key

The copied renderer path must be absolute. Run the plugin's Node helper:

```sh
node "${CLAUDE_PLUGIN_ROOT}/settings-edit.js" set "$HOME/.claude/tools/agent-statusline.js"
```

The helper preserves every other key and its order, validates the existing JSON object, creates
an exclusive timestamped backup when an existing settings file is changed, and writes atomically.
If it fails, report the stderr diagnostic and do not claim installation succeeded.

## Step 3 — prove it worked

```sh
cat "${CLAUDE_PLUGIN_ROOT}/fixtures/payload-pro.json" | node "$HOME/.claude/tools/agent-statusline.js"
```

The line should read `◐ ctx 84k/200k (42%) wk 63%/5h 15% Opus 5` (with colour escapes the
terminal will render). If it does not, do not claim a green install.

## Closing

Report the absolute renderer path, the `statusLine` value, and the rendered fixture line. Do not
print or report all settings keys. Name the way back out: `/statusline-remove` deletes the
`statusLine` key and copied renderer while leaving every other settings key untouched. Run
`/statusline-remove` before uninstalling this plugin.
