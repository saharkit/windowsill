---
name: statusline-remove
description: Remove the agent-statusline status line — delete only the statusLine key from ~/.claude/settings.json, leave every other key untouched, and delete ~/.claude/tools/agent-statusline.js. Use when the user asks to remove, uninstall or disable agent-statusline.
allowed-tools: [Read, Edit(~/.claude/settings.json), AskUserQuestion]
---

# statusline-remove — remove the agent-statusline status line

Confirm once before changing anything. Tell the user that the `statusLine` key will be deleted,
every other settings key will remain, and `~/.claude/tools/agent-statusline.js` will be deleted.

After confirmation, update the settings file through the plugin's Node helper:

```sh
set -e
node "${CLAUDE_PLUGIN_ROOT}/settings-edit.js" remove
rm -f "$HOME/.claude/tools/agent-statusline.js"
```

The helper validates an existing settings file, preserves every other key and its order, creates
an exclusive timestamped backup when it changes an existing file, and writes atomically. If it
fails, report the stderr diagnostic and do not remove the renderer. If the settings file is absent
or has no `statusLine` key, the helper succeeds and the renderer removal still proceeds.

## Closing

Report that the `statusLine` key and copied renderer were removed (or were already absent), and
that other settings keys were left untouched. Run `/statusline-remove` before uninstalling the
plugin; if it was already uninstalled, remove the `statusLine` key by hand and delete
`~/.claude/tools/agent-statusline.js` before reinstalling the plugin to run this skill.
