# agent-statusline

One line at the bottom of Claude Code: how much of the context window is gone, how much of
the weekly budget is spent, and which model is answering. Every figure is read from the
payload the harness pipes to the renderer on stdin — so the line is the same on any machine.

The renderer is a single Node script, no dependencies, no network calls, no host state read.
It targets Node 18+ and uses only the Node standard library.

## What the line renders

```
◐ ctx 84k/200k (42%) wk 63%/5h 15% Opus 5
```

Three figures, separated by a single space:

| figure | shape | source |
|---|---|---|
| context use | `◐ ctx <used>k/<size>k (<pct>%)` | `context_window.total_input_tokens`, `context_window.context_window_size`, `context_window.used_percentage` |
| weekly budget | `wk <wkpct>%` and (when present) dim `/5h <hrpct>%` | `rate_limits.seven_day.used_percentage` and `rate_limits.five_hour.used_percentage` |
| model | display name | `model.display_name` |

Colours are applied to the context and weekly segments based on the percentage remaining
(green above 50, yellow 20–50, red below 20) and the weekly spend (green at or below 60,
yellow above 60, red above 85). The five-hour suffix is dim; the whole budget segment is
omitted (not empty) when `rate_limits` is null.

When the payload is missing the fields the renderer needs (`context_window_size` is 0 or
`used_percentage` is null), it falls back to a dim model name and the last path segment of
`workspace.current_dir` / `cwd`. The line never goes blank.

## Install

`/statusline-setup` is the install ceremony. It copies the renderer from the plugin root to
`~/.claude/tools/agent-statusline.js` and writes the `statusLine` key into
`~/.claude/settings.json` — preserving every other key already there. It ends by piping the
bundled fixture through the installed renderer, the way `/voice-setup` ends in a passing
selftest.

The install needs `node` and nothing else. There is no `jq`, no `npm install`, no
`pip install`. The plugin itself does not run during install — it is a static script and a
settings file.

**The installation paths are POSIX-only.** The script lives at `~/.claude/tools/` and the
settings file at `~/.claude/settings.json`; on Windows the file lives elsewhere and this
plugin is not the bridge for that.

## Remove

`/statusline-remove` deletes only the `statusLine` key from `~/.claude/settings.json` and the
copied renderer file. Every other settings key is untouched. It does not delete
`~/.claude/settings.json.bak` — that is the user's own rollback file, made by
`/statusline-setup` if `statusLine` was overwritten, and its retention is the user's
decision.

## Degradation

The script renders byte-identical output for every stdin payload — but the figures a payload
carries differ by account type. Read this before deciding the plugin is broken.

| figure | works for you? |
|---|---|
| model name | always |
| context percentage | always |
| weekly spend | only on a Claude.ai Pro/Max subscription, and only after the first response. API-key, Bedrock and Vertex users get an empty budget segment. That is degradation, not a bug. |

A Pro-shaped payload (`fixtures/payload-pro.json`) carries both `seven_day` and `five_hour`.
An API-key-shaped payload (`fixtures/payload-apikey.json`) carries `rate_limits: null` —
exactly what API-key, Bedrock and Vertex sessions emit. The renderer omits the whole budget
segment when `seven_day` is null, so the line shows the model name and the context figure
only.

## For developers

The renderer is a plain Node script at `statusline.js`. Two fixtures ship in `fixtures/`:
both pass through `node statusline.js < <fixture>` to produce a known line. The exact
rendered outputs:

| fixture | rendered line |
|---|---|
| `payload-pro.json` | `◐ ctx 84k/200k (42%) wk 63%/5h 15% Opus 5` |
| `payload-apikey.json` | `◐ ctx 84k/200k (42%) Opus 5` |

Off the happy path: unparseable or empty stdin prints nothing to stdout, prints one line
naming the parse failure to stderr, and exits 1. Both fixtures exit 0.

## License

MIT — see [LICENSE](../../LICENSE).
