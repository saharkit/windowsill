# Publishing — listing windowsill in public plugin directories

Tracker: issue [#20](https://github.com/saharkit/windowsill/issues/20).
Prepared: 2026-08-05.

## Scout verification

Two candidate targets were scouted and cross-checked against the public repos (2026-08-05). The two
in-app submission-form URLs below must be re-confirmed at actual submission time — this document was
prepared without submission credentials.

### Candidate 1: `anthropics/claude-plugins-official`

**Verdict: WRONG TARGET — use `claude-plugins-community` instead.**

`anthropics/claude-plugins-official` is Anthropic's curated marketplace. There is no application
process — Anthropic decides what to include at its discretion. It is not a venue for community
submission.

The correct target is **`anthropics/claude-plugins-community`**, the public community marketplace
where third-party plugins land after review. Users add it with:

```
/plugin marketplace add anthropics/claude-plugins-community
```

Two in-app submission forms exist:

| form | URL | requirement |
|---|---|---|
| claude.ai | <https://claude.ai/admin-settings/directory/submissions/plugins/new> | Team/Enterprise org, directory management access |
| Console | <https://platform.claude.com/plugins/submit> | Individual author (no org needed) |

**Important**: These forms submit **individual plugins**, not marketplaces. Windowsill is a
marketplace. To list windowsill's reach here, submit each plugin individually.

**Prepared plugin entries:**

#### voice-loop

| field | value |
|---|---|
| name | voice-loop |
| source | <https://github.com/saharkit/windowsill/tree/main/plugins/voice-loop> |
| description | Talk to Claude Code and hear it answer: a Stop hook speaks marker-tagged lines, a push-to-talk script dictates into the prompt, and /voice-setup installs the whole contour (local, LAN, or cloud speech). Ships its own self-hostable speech server with STT (faster-whisper) and TTS (Silero, XTTS-v2, Ukrainian dedicated voices). |
| license | MIT |
| author | Sahar |
| version | 0.5.0 |
| validation | `claude plugin validate ./plugins/voice-loop` must pass before submission |
| keywords | voice, tts, stt, speech, dictation, hook |

#### sill-core

| field | value |
|---|---|
| name | sill-core |
| source | <https://github.com/saharkit/windowsill/tree/main/plugins/sill-core> |
| description | Shared core for windowsill plugins: assistant state store with atomic writes, remind-once etiquette, and schema-versioned JSON persistence. One file per plugin under XDG_STATE_HOME, separate from config. |
| license | MIT |
| author | Sahar |
| version | 0.1.0 |
| validation | `claude plugin validate ./plugins/sill-core` must pass before submission |
| keywords | state, persistence, reminders, atomic-write |

### Candidate 2: Community awesome-list(s)

**Verdict: MIXED — one viable path, one blocked, one irrelevant.**

Three community lists were checked:

#### `rdmgator12/awesome-claude-plugins`

**Verdict: BLOCKED — gate requires official-catalog listing first.**

This community-maintained directory (333 plugins) requires every entry to be "publicly listed at
claude.com/plugins." Windowsill plugins are not yet in the official catalog. This becomes viable
**after** the Anthropic community submissions above are approved.

Contribution: edit `data/plugins.json`, run `python3 scripts/generate_readme.py`, PR all three
generated files. Format:

```json
{
  "name": "voice-loop",
  "url": "https://github.com/saharkit/windowsill/tree/main/plugins/voice-loop",
  "category": "productivity",
  "surfaces": ["Claude Code"],
  "anthropic": false,
  "slug": "voice-loop",
  "description": "Two-way voice for Claude Code: TTS, STT, and dictation through a self-hostable speech server.",
  "use_case": "Hands-free coding sessions, voice-driven prompts, hearing Claude Code's responses aloud."
}
```

#### `Chat2AnyLLM/awesome-claude-plugins`

**Verdict: VIABLE — explicitly tracks marketplaces, not just plugins.**

This curated list tracks **77 marketplaces and 1,275 plugins**. It is the only community directory
found that explicitly catalogs marketplaces alongside plugins. Windowsill fits here as a marketplace
source.

Contribution goes through the companion config repo `Chat2AnyLLM/awesome-repo-configs` — the repo
every actionable step and reference link below points at; the catalog rebuilds from its merged
config. The expected entry format (from the studiomeyer-marketplace precedent):

```json
"saharkit/windowsill": {
  "name": "windowsill",
  "description": "Plugins and skills the saharkit agent school shares with everyone: voice-loop (two-way voice with STT/TTS) and sill-core (assistant state store with atomic writes). Install with: claude plugin marketplace add saharkit/windowsill",
  "enabled": true,
  "type": "marketplace",
  "repoOwner": "saharkit",
  "repoName": "windowsill",
  "repoBranch": "main"
}
```

**Contribution process** (scouted 2026-08-05):

1. Fork `Chat2AnyLLM/awesome-repo-configs`
2. Add the entry above to `plugin_repos.json`
3. Run the validation tests:
   ```
   python3 tests/test_json_validation.py
   python3 tests/test_pr_review_config.py
   python3 tests/test_readme_pr_reminder.py
   ```
4. Open a PR to `awesome-repo-configs`
5. The `awesome-claude-plugins` catalog rebuilds from the merged config

[CONTRIBUTING.md](https://github.com/Chat2AnyLLM/awesome-repo-configs/blob/main/CONTRIBUTING.md)
[plugin_repos.json](https://github.com/Chat2AnyLLM/awesome-repo-configs/blob/main/plugin_repos.json)

#### `GiladShoham/awesome-claude-plugins`

**Verdict: NOT A DIRECTORY — it is itself a marketplace.**

This is a Claude Code plugin marketplace that users install via `/plugin marketplace add`. It
accepts individual plugin submissions (not marketplace listings). Not the right venue for listing
windowsill itself, but voice-loop and sill-core could be submitted as individual plugins here after
they're in the community marketplace.

## Submission order

The targets form a dependency chain:

0. **Confirm the gate**: issue #20 makes submission conditional on the v0.2 wave (XTTS engine #12,
   streaming #18, dictate rewrite #19) being merged — confirm that before anything below
1. **Validate**: `claude plugin validate ./plugins/voice-loop` and `./plugins/sill-core`
2. **Anthropic community marketplace** — submit voice-loop and sill-core via the Console form
3. **`rdmgator12/awesome-claude-plugins`** — once plugins appear at claude.com/plugins, add entries
4. **`Chat2AnyLLM/awesome-claude-plugins`** — submit windowsill as a marketplace source
5. **Link back** — record the accepted submissions here and in ACHIEVEMENTS.md, and add each
   accepted directory listing (badge or link) to the README — the ticket names that deliverable

## Credentials needed

Every submission above requires GitHub credentials and/or a claude.ai/platform.claude.com account.
This document was prepared credential-less — it scouted the targets and prepared the content. The
actual PRs and form submissions must be executed by an operator with the required access.

## Links

- Anthropic community marketplace catalog: <https://github.com/anthropics/claude-plugins-community>
- Console submission form: <https://platform.claude.com/plugins/submit>
- `rdmgator12/awesome-claude-plugins` CONTRIBUTING: <https://github.com/rdmgator12/awesome-claude-plugins/blob/main/CONTRIBUTING.md>
- `Chat2AnyLLM/awesome-claude-plugins`: <https://github.com/Chat2AnyLLM/awesome-claude-plugins>
- Windowsill marketplace: <https://github.com/saharkit/windowsill>
