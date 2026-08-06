# windowsill

[![selftest](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml/badge.svg)](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Plugins and skills the **saharkit agent school** shares with everyone.

A windowsill is where you put things out for whoever walks past: tools that were built for real work,
generalized until they are useful to someone else's machine. Everything here is packaged as a Claude
Code plugin, installs in a couple of commands, and is meant to be read as well as run.

## Add the marketplace

In your shell:

```sh
claude plugin marketplace add saharkit/windowsill
```

(or, from inside a running Claude Code session, the slash-command form: `/plugin marketplace add
saharkit/windowsill`.)

Then install what you want — this one is typed **inside a session**, not in your shell:

```text
/plugin install <plugin>@windowsill
```

## Running on Windows

The recommended route is **WSL2 with WSLg** on Windows 11 — and honesty first: nobody has run
this end to end on a real Windows machine yet.
[#41](https://github.com/saharkit/windowsill/issues/41) tracks that verification pass; until
it lands, this is the route we recommend, not a tested guarantee.

From an elevated PowerShell:

```powershell
wsl --install
```

Restart when it asks; on first launch the distro prompts for a UNIX username and password. If
the install fails with a virtualization error, enable virtualization in your UEFI/BIOS first.
(Windows 10 21H2+ can also run WSLg via the Store WSL package, but Windows 11 is the route we
intend to verify.)

Inside the distro, **Add the marketplace** above works exactly as written — the marketplace
add and the `/plugin install` both. A plugin's own setup may have platform-specific steps;
its README is the place to check — for voice-loop's audio and server notes on WSL, see
[its README](plugins/voice-loop/README.md).

## What is on the sill

| plugin | version | what it does | |
|---|---|---|---|
| **sill-core** | 0.1.0 | Shared core for windowsill plugins: assistant state store with atomic writes and remind-once etiquette. Schema-versioned JSON persistence — one file per plugin under `XDG_STATE_HOME`, separate from config. | [README](plugins/sill-core/README.md) |
| **voice-loop** | 0.6.0 | Two-way voice for Claude Code with **pluggable speech providers** — a config registry picks OpenAI, ElevenLabs or Deepgram per direction, or keeps every clip local/self-hosted on the bundled server (faster-whisper STT; Silero, XTTS-v2 or Ukrainian TTS). Switching backends is a config entry, never a code change. A `Stop` hook speaks marked lines, a push-to-talk script dictates into the prompt, and `/voice-setup` installs the whole contour. | [README](plugins/voice-loop/README.md) |

Every plugin owns its own README, its own tests, its own docs and its own version — open its README
for anything about it. More will land on the sill over time.

Not a plugin, but on the sill too:

| | what it is |
|---|---|
| [tales/](tales/README.md) | the shelf's story content — bedtime tales from the school, in Russian, some with a voiced edition. They never explain the machinery they came from: a tale hands you a fishing rod, not a fish. |
| [ACHIEVEMENTS.md](ACHIEVEMENTS.md) | the milestone ledger for the shelf and the school. Every row carries a date and a public proof link, and lands no earlier than the thing it claims — so a short page here means little has been earned yet, not that little is written down. |

## What a plugin brings to the shelf

Everything a plugin *is* lives under `plugins/<name>/`. **The root belongs to the shelf**, so a new
plugin adds one directory and nothing else at the top level:

```
plugins/<name>/
  .claude-plugin/plugin.json   the manifest: name, description — and the version, which lives HERE
  README.md                    the plugin's own front page (the table above links to it)
  hooks/ scripts/ skills/      whatever the plugin actually ships
  tests/ + its runner config   its own suite, invoked from its own directory
  docs/                        its own deeper reading, if it needs any
```

The rules that follow from that:

- **The version lives in `plugins/<name>/.claude-plugin/plugin.json`** and is mirrored into
  `.claude-plugin/marketplace.json` and into the catalog row above. All three must agree, nothing
  else records a version, and no CI job checks the agreement — a bump touches all three or it is a
  review finding.
- **Tests belong to the plugin.** Its runner configuration (`pytest.ini`, `.coveragerc`, …) sits in
  its directory with paths relative to it, and the suite is invoked from there — plugins never share
  a test root, and adding one never disturbs another.
- **The catalog row above is part of the plugin's own PR**, together with its `marketplace.json`
  entry. A plugin that is not in the table is not on the shelf.
- **CI is shared, per-plugin.** One workflow in `.github/workflows/` runs every plugin's checks; a
  new plugin adds its own jobs (or its own matrix entry) there rather than a second workflow —
  the shape to copy: scope every job with `working-directory: plugins/<name>` and prefix its job id
  with the plugin name so two plugins' checks stay distinguishable on a PR.

Root-level, and this is the whole list: this README (the catalog), `.claude-plugin/marketplace.json`
(the manifest `marketplace add` reads), `.github/` (shared CI), `plugins/`, `tales/`,
`ACHIEVEMENTS.md` (the milestone ledger), `LICENSE`, `.gitignore`, `CLAUDE.md` (the passport an
agent reads first) and `.claude/` (its review-lens map).

## Conventions (every plugin here follows them)

- **Every plugin is testable without hardware.** If a plugin talks to the world, it ships a loopback
  or contract test that CI can run on Linux and macOS.
- **The least-privilege path is the default path.** Anything needing root or a system consent is an
  explicit opt-in, with the command printed rather than silently executed.
- **Configuration lives in the user's config file, never in the code.** No endpoints, keys or paths
  are baked in.
- **Install is a skill, not a page of instructions.** Where setup is fiddly, an agent does it and then
  proves it worked.

## Author

**Sahar** — AI engineer at saharkit.

## License

MIT — see [LICENSE](LICENSE).
