# windowsill

[![selftest](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml/badge.svg)](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml)
[![coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fsaharkit%2Fwindowsill%2Fmain%2F.github%2Fcoverage.json)](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml)
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

Then install what you want from your shell:

```sh
claude plugin install voice-loop@windowsill   # voice-loop also needs sill-core — install both:
claude plugin install sill-core@windowsill
```

If you are already in a Claude Code session, the equivalent slash commands are
`/plugin install voice-loop@windowsill` and `/plugin install sill-core@windowsill`.

## Tester ritual

On a fresh machine, the complete marketplace check is:

```sh
claude plugin marketplace add saharkit/windowsill
claude plugin list --available --json
```

The second command lists what the marketplace makes available. `claude plugin list` with no
flags lists **installed** plugins only, so an empty list on a fresh machine is expected — it
does not mean the marketplace failed. Install the plugin from the shell (or use the equivalent
slash command inside a session), then inspect the installed metadata:

```sh
claude plugin install voice-loop@windowsill
```

Back in the shell:

```sh
claude plugin details voice-loop
```

`claude plugin marketplace info` is not a command. There is also no details lookup for an
uninstalled `voice-loop@windowsill`; before installation, clone the shelf and use the local
path workaround instead:

```sh
git clone https://github.com/saharkit/windowsill
claude plugin details --plugin-dir ./windowsill/plugins/voice-loop
```

The in-repository relative sources are pinned by the shelf clone itself. The official catalog
also pins third-party sources by commit reference and SHA; this shelf has no third-party source
entries today.

For the machine-checkable half of the conformance pass, run the seeded eval cases after cloning:

```sh
claude plugin eval ./windowsill/plugins/voice-loop
```

They complement the human checklist in [`plugins/voice-loop/CONFORMANCE.md`](plugins/voice-loop/CONFORMANCE.md)
and the directory-readiness work tracked in [#56](https://github.com/saharkit/windowsill/issues/56)
and [#20](https://github.com/saharkit/windowsill/issues/20).

## Running on Windows

**sill-core is CI-tested on native `windows-latest`** — its atomic-write state store and
remind-once logic run on Windows with the same 100% branch-coverage gate as Linux (the
kill-9 crash-consistency tests are skipped; `fcntl` advisory locking is replaced with
`msvcrt.locking`). The rest of the shelf — voice-loop's server, hooks, hotkeys, audio
capture and playback — still depends on POSIX facilities (process groups, `pkill`,
`fcntl`) and is not a native-Windows path. [#42](https://github.com/saharkit/windowsill/issues/42)
tracks that work.

The documented Windows path for **voice-loop** is **WSL2 with WSLg on Windows 11**. From an
elevated PowerShell, install WSL2 with:

```powershell
wsl --install
```

Restart if Windows asks, complete the distro setup, then open the WSL terminal. Inside the
distro, use the ordinary Linux quickstart above: add the marketplace in the shell, install
`voice-loop` (and its `sill-core` dependency), then run `/voice-setup` in a Claude Code session.
WSLg is the Windows 11 integration intended to provide Linux GUI and audio-device access; an
attended desktop session is required for microphone and speaker checks. If WSL reports a
virtualization error, enable virtualization in UEFI/BIOS first.

A real WSL2 pass on Windows 11 24H2 (Ubuntu 24.04, WSL 2.7.11, WSLg 1.0.73.2) verified the
marketplace install, the registered hook command, dictation with the CI-style fake recorder,
and a `lan` loopback against a remote speech server. The hook contract ran with
`via=stream` and two SSE chunks; live Claude Code hook dispatch, audibility and `/voice-setup`
end to end were not measured. The loopback passed with an explicit `--endpoint`; a fresh distro without `jq`
did not pass the config-driven form. Microphone passthrough was not exercised, so this page
does not claim it. See the
[voice-loop WSL2 verification record](plugins/voice-loop/TESTING.md#8-wsl2-verification-record-2026-08-11)
and [its WSL notes](plugins/voice-loop/README.md#windows-wsl2--wslg).

## What is on the sill

| plugin | version | what it does | |
|---|---|---|---|
| **sill-core** | 0.1.0 | Shared core for windowsill plugins: assistant state store with atomic writes and remind-once etiquette. Schema-versioned JSON persistence — one file per plugin under `XDG_STATE_HOME`, separate from config. | [README](plugins/sill-core/README.md) |
| **voice-loop** | 0.8.0 | Two-way voice for Claude Code — dictation in any language Whisper recognises, and speak-back with bundled local voices for **English, Russian, Ukrainian, German, Spanish and French** (Ukrainian also has an optional dedicated engine; **Turkish and 16 other languages need the optional XTTS-v2 engine plus a reference recording you supply**, or a cloud provider) — with **pluggable speech providers** — a config registry picks OpenAI, ElevenLabs or Deepgram per direction, or keeps every clip local/self-hosted on the bundled server (faster-whisper STT; Silero, XTTS-v2 or Ukrainian TTS). Switching backends is a config entry, never a code change. A `Stop` hook speaks marked lines, a push-to-talk script dictates into the prompt, and `/voice-setup` installs the whole contour. **Requires [`sill-core`](plugins/sill-core/README.md) — install both together.** | [README](plugins/voice-loop/README.md) |

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

## Privacy

Nothing here collects anything — no telemetry, no analytics, no accounts. Your voice goes
only to the backend you configured, and bug reports are stripped and shown to you before
anything is shared. The full page, with the receipts: [PRIVACY.md](PRIVACY.md).

## Author

**Sahar** — AI engineer at saharkit.

## License

MIT — see [LICENSE](LICENSE).
