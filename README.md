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

Then install what you want — in the shell, or inside a session as `/plugin install
<plugin>@windowsill`:

```sh
claude plugin install <plugin>@windowsill
```

### The whole ritual, as a tester runs it

Four commands, in this order, on a machine that has never seen this shelf:

```sh
claude plugin marketplace add saharkit/windowsill   # 1. add the shelf
claude plugin list --available --json               # 2. read what is on it
claude plugin install voice-loop@windowsill         # 3. take one down
claude plugin details voice-loop                    # 4. see what you took
```

Three things in that sequence surprise people, and none of them is a fault of the shelf:

- **`claude plugin list` with no flags lists what is _installed_, not what is available.** Straight
  after step 1 it says "No plugins installed" — which is correct, because you have not installed
  anything yet. The catalog is step 2, and it needs **both** flags: `--available` without `--json`
  is silently ignored and you get the installed list again, which is the exact way this reads as
  "list does not work".
- **There is no `marketplace info`.** `claude plugin marketplace` has exactly four verbs: `add`,
  `list`, `remove`, `update`. What a marketplace carries is read with `list --available --json`.
- **`details` resolves an _installed_ plugin**, so there is no read-it-before-you-install for a
  `plugin@marketplace` id. Until there is, clone and point one invocation at the checkout:

  ```sh
  git clone https://github.com/saharkit/windowsill
  claude --plugin-dir windowsill/plugins/voice-loop plugin details voice-loop
  ```

  `--plugin-dir` is a flag on `claude` itself and goes **before** the subcommand. It loads the
  plugin for that one command, so you get its component inventory and projected token cost —
  skills, hooks, always-on tokens — with nothing installed and nothing left behind.

Two more commands worth knowing, neither of them part of the ritual: `claude plugin validate
<path> --strict` checks a manifest (this repository's own, or a plugin's) and fails on
unrecognized fields and missing metadata; `claude plugin eval <path>` runs a plugin's `evals/`
cases — voice-loop seeds three, see
[`plugins/voice-loop/evals/`](plugins/voice-loop/evals/README.md).

One sentence on pinning, because a directory reader will ask: the official marketplace pins
third-party plugins by `ref` + `sha`, since their sources are other repositories. Everything here
is a **relative source inside this repository** (`./plugins/<name>`), so the marketplace clone you
added *is* the pin — you run what `marketplace add` fetched until you run `marketplace update`.
Each entry also carries the metadata a directory reads without cloning — author, homepage, license,
keywords, category ([issue #20](https://github.com/saharkit/windowsill/issues/20)). That lives in
the manifest; `list --available --json` projects a narrower view of it (id, description, version,
source), so read `.claude-plugin/marketplace.json` for the rest. Keeping this page true to what the
CLI actually does is the conformance pass in
[issue #56](https://github.com/saharkit/windowsill/issues/56).

## What is on the sill

| plugin | version | what it does | |
|---|---|---|---|
| **voice-loop** | 0.3.2 | Talk to Claude Code and hear it answer — a `Stop` hook speaks marked lines, a push-to-talk script dictates into the prompt, and `/voice-setup` installs the whole contour (local, LAN, or cloud speech). Ships its own self-hostable speech server. | [README](plugins/voice-loop/README.md) |

Every plugin owns its own README, its own tests, its own docs and its own version — open its README
for anything about it. More will land on the sill over time.

Not a plugin, but on the sill too:

| | what it is |
|---|---|
| [tales/](tales/README.md) | the shelf's story content — bedtime tales from the school, in Russian, some with a voiced edition. They never explain the machinery they came from: a tale hands you a fishing rod, not a fish. |

## What a plugin brings to the shelf

Everything a plugin *is* lives under `plugins/<name>/`. **The root belongs to the shelf**, so a new
plugin adds one directory and nothing else at the top level:

```
plugins/<name>/
  .claude-plugin/plugin.json   the manifest: name, description — and the version, which lives HERE
  README.md                    the plugin's own front page (the table above links to it)
  hooks/ scripts/ skills/      whatever the plugin actually ships
  tests/ + its runner config   its own suite, invoked from its own directory
  evals/                       its `claude plugin eval` cases, if it has any
  docs/                        its own deeper reading, if it needs any
```

The rules that follow from that:

- **The version lives in `plugins/<name>/.claude-plugin/plugin.json`** and is mirrored into
  `.claude-plugin/marketplace.json` and into the catalog row above. All three must agree, nothing
  else records a version, and no CI job checks the agreement — a bump touches all three or it is a
  review finding. The plugin's directory metadata (author, homepage, license, keywords) is mirrored
  the same way and carries the same obligation; `category` is the one field that belongs **only**
  to the marketplace entry, and `claude plugin validate <path> --strict` says so if you put it in
  the wrong file.
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
(the manifest `marketplace add` reads), `.github/` (shared CI), `plugins/`, `tales/`, `LICENSE`,
`.gitignore`, `CLAUDE.md` (the passport an agent reads first) and `.claude/` (its review-lens map).

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
