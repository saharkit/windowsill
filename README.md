# windowsill

[![selftest](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml/badge.svg)](https://github.com/saharkit/windowsill/actions/workflows/selftest.yml)
[![coverage: 100% (gated)](https://img.shields.io/badge/coverage-100%25%20%28gated%29-brightgreen)](plugins/voice-loop/TESTING.md)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](server/README.md#requirements)

> The coverage badge is a **`--cov-fail-under=100` gate on the server's Python**, not a measured
> float that drifts — and shell scripts are guaranteed differently (shellcheck + a real Stop-hook
> invocation in CI), because line coverage is not a meaningful metric for glue. The badge links to
> the page that says exactly that.

Plugins and skills the **saharkit agent school** shares with everyone.

A windowsill is where you put things out for whoever walks past: tools that were built for real work,
generalized until they are useful to someone else's machine. Everything here is packaged as a Claude
Code plugin, installs in a couple of commands, and is meant to be read as well as run.

## Add the marketplace

```
claude marketplace add saharkit/windowsill
```

then install what you want:

```
/plugin install voice-loop@windowsill
```

## Plugins

| plugin | what it does | status |
|---|---|---|
| [**voice-loop**](plugins/voice-loop) | Talk to Claude Code and hear it answer — a `Stop` hook speaks marked lines, a push-to-talk script dictates into the prompt, and `/voice-setup` installs the whole contour (local, LAN, or cloud speech). Ships its own self-hostable speech server. | v0.1.0 |

More will land on the sill over time; each plugin owns its own README, its own tests, and its own
version.

## Docs

- [docs/architecture.md](docs/architecture.md) — the loop: both paths, the three backends, where
  config and state live.
- [docs/troubleshooting.md](docs/troubleshooting.md) — by failure class: paste modes, recorder and
  transcript races, robotic-sounding voices, firewalled servers, selftest messages.
- [docs/faq.md](docs/faq.md) — resources, privacy per backend, languages, custom voices, permissions.

## What is in this repo

```
.claude-plugin/marketplace.json   the marketplace manifest — what `marketplace add` reads
plugins/<name>/                   one directory per plugin (manifest, hooks, scripts, skills, docs)
server/                           companion services a plugin needs (currently the voice-loop speech server)
tests/                            server unit tests (no models, no network) — 100% gated in CI
docs/                             architecture, troubleshooting, FAQ
.github/workflows/                CI: every plugin's automated checks
```

## Conventions

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
