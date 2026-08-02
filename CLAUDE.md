# CLAUDE.md — windowsill

Guidance for Claude Code and any other agent working in this repository. It describes what is
actually here today; when it disagrees with the tree, the tree wins and this file gets fixed.

## What this is

A public **shelf**: a multi-plugin Claude Code marketplace (`claude plugin marketplace add
saharkit/windowsill`). **The root belongs to the shelf; each plugin owns its own subtree** under
`plugins/<name>/` — its manifest, README, code, tests, runner config and docs. A new plugin adds one
directory and nothing else at the top level.

Root-level, and this is the whole list (every tracked entry): `README.md` (the catalog),
`.claude-plugin/marketplace.json` (what `marketplace add` reads), `.github/` (shared CI), `plugins/`
(one directory per plugin), `tales/` (story content, not a plugin), `LICENSE`, `.gitignore`,
`.claude/`, this file.

A plugin's **version lives in `plugins/<name>/.claude-plugin/plugin.json`** — that is the source.
It is recorded in **three** places in all: that manifest, its mirror in
`.claude-plugin/marketplace.json`, and the plugin's row in the root `README.md` catalog table. All
three must agree, and **nothing in CI checks that they do** — a bump that misses one is caught by a
reviewer or not at all. Do not add a fourth site (this file deliberately names no version number).

On the shelf today: **`plugins/voice-loop`** — two-way voice for Claude Code.

## Stack, per plugin tree

**voice-loop** — no build step; the plugin is run from its checkout. The hook and hotkey half needs
no install at all; the server half does — `pip install -r server/requirements.txt` (eight required
runtime dependencies, plus two per-language accentuation extras that ship in the file and can be
removed; the XTTS engine's own pins are deliberately NOT in it — see `server/README.md`).

- `scripts/` — **stdlib-only Python 3.10+** (`speak.py`, `dictate.py`), each behind a thin bash entry
  point (`speak.sh`, `dictate-toggle.sh`) so the hook/hotkey registration surface never changes.
  `selftest.sh` is the hardware-free loopback harness.
- `server/voice_server.py` — **one single-file FastAPI app** served by uvicorn (~1.1k lines). STT is
  faster-whisper; TTS is Silero via `torch.hub`, with an optional XTTS-v2 voice-clone engine that
  falls back to Silero on failure. Endpoints: `POST /stt`, `POST /tts`, `POST /tts/stream` (SSE),
  `GET /health`. Every server **setting** is a `VOICE_LOOP_*` environment variable; two file inputs
  sit beside them — the stress dictionary (`$XDG_CONFIG_HOME/voice-loop/stress.json`, relocatable
  via `VOICE_LOOP_STRESS_FILE`) and the STT hallucination blocklist
  (`server/stt_hallucinations.txt`, user-extendable, with no env override). It has **no
  authentication** and binds to loopback by default — that is deliberate, and any change near it is a
  security-lens change.
- `hooks/hooks.json` registers `Stop` and `PostToolUse`; `skills/` ships `voice-setup` and
  `voice-design`.
- `tests/`, `pytest.ini`, `.coveragerc` live in the plugin directory and are invoked from there. The
  suite touches **no models, no network and no audio hardware** — expensive dependencies are faked at
  a seam while the real function bodies run.

## The gates, as CI runs them today

**`.github/workflows/selftest.yml`** — on push to `main`, on every pull request, and manual:

- **`shellcheck` job** — `bash -n` over `plugins/voice-loop/scripts/*.sh`, then `shellcheck -S
  warning` over the same; voice-loop's manifests must parse as JSON (three hardcoded paths —
  `marketplace.json`, the plugin's `plugin.json`, its `hooks.json`; a second plugin's manifests are
  not checked until that step lists them); `selftest.sh --help` and its unknown-argument rejection
  are asserted.
- **`coverage` job** — `cd plugins/voice-loop && pytest --cov=voice_server --cov-report=term-missing
  --cov-fail-under=100`, on Python **3.10, 3.11, 3.12 and 3.13**. `.coveragerc` sets `branch = True`,
  so the 100% is **statements *and* branches**. It is **scoped to `server/voice_server.py`**: the hook
  scripts are deliberately *not* under it (they are proven by real invocation instead — see
  `plugins/voice-loop/TESTING.md`).
- **`loopback` job** — ubuntu/Russian and macOS/English lanes. Starts the **real server** and then
  runs **real-invocation smokes**: the `selftest.sh` TTS→STT loopback at `--strict --threshold 0.7`;
  the Stop-hook contract (asserts `via=stream` and `chunks>1`, so a silent regression to the blob
  fallback fails); the eager-speaking contract (history seeding, the spoken-ledger, the non-blocking
  lock, "spoken exactly once" as a count); and the dictation contract (fake recorder and clipboard,
  real STT).

**`.github/workflows/xtts-install-probe.yml`** — weekly cron (Mondays 05:17 UTC) and manual. Installs
the documented XTTS pins into a clean venv and imports them for real, and asserts the same pins are
documented in `server/requirements.txt`, `server/README.md` and `server/voice_server.py`. No model
weights are ever downloaded.

**CodeQL** — code scanning is enabled on the repository through GitHub's **default setup**. This is
a **repository setting, not tree state**: a checkout can only confirm the negative half (there is no
CodeQL workflow file in `.github/workflows/`), so verify it under Settings → Code security rather
than by grepping. Its findings are binding, and resolved ones are recorded where they were fixed
(the "information exposure through an exception" reasoning in `server/voice_server.py`, the
linear-segmentation rewrite in the history).

## Public-repo hygiene

- **English is the prose language** — code, comments, docs, commit messages, issues. `tales/` is the
  one deliberate prose exception (Russian by design). Language *data* is whatever the feature
  speaks and is not covered by this rule: the STT hallucination blocklist, the loopback job's
  Russian test phrase, stress dictionaries, examples.
- **MIT.** Everything added here ships under it; do not vendor code that cannot.
- **No secrets, keys, tokens, hostnames or personal endpoints — ever**, not even in an example.
  Configuration lives in the user's own config file or environment; nothing is baked into the code.
- **No links to private sessions, internal trackers or internal infrastructure.** Issue references
  mean *this* repository's issues.
- **No generated artifacts.** See `.gitignore`: a `.coverage` file is a binary carrying the absolute
  paths of whoever ran the suite.

## The review bar

> A substantive code change gets **TWO review lenses BEFORE merge** — **architecture + QA** at
> minimum. Add a **security** lens when input parsing, keys, or network surfaces change. Add a
> **capacity** lens when concurrency or resource limits change. **CI green is NOT a review.**

`.claude/review-profile.yml` maps path classes to the lenses they require. Every rule there carries
architecture + QA and only ever *adds* to them — the escalations can add security and capacity but
cannot restore a baseline lens a rule dropped, so no rule drops one. The single exemption is prose,
and it is scoped: this file, the review map itself, `README.md` and `TESTING.md` are reviewed like
code, because they state the bar rather than describe a feature.

## Conventions

- **Branches** are `<type>/<issue>-<slug>` (issue number leading). The PR body carries `Closes #N`,
  and the commit/PR subject names the area it touches — `server:`, `speak:`, `ci:`, `docs:`,
  `refactor:`, `chore:`, `tales:`. The list is open: a new area names itself in the same style.
- **Tests live with their plugin**, and **coverage config is per-plugin**
  (`plugins/<name>/.coveragerc`, `plugins/<name>/pytest.ini`) with paths relative to that directory.
  Plugins never share a test root; adding one never disturbs another.
- **CI is shared, per-plugin.** A new plugin adds its own jobs (or matrix entries) to the existing
  workflow rather than a second workflow: scope each job with `working-directory: plugins/<name>` and
  prefix the job id with the plugin name. That shape is for the *second* plugin onward and the tree
  does not yet show it — while voice-loop is alone, its job ids are unprefixed (`shellcheck`,
  `coverage`, `loopback`) and only `coverage` sets `working-directory`; the others use repo-root
  paths. `xtts-install-probe.yml` is a standing exception to the one-workflow rule, earned by its
  own weekly cron trigger rather than by being a second plugin.
- **The catalog row in `README.md` and the `marketplace.json` entry ride the plugin's own PR.** A
  plugin that is not in the table is not on the shelf.
- **Every plugin is testable without hardware**, the least-privilege path is the default path, and
  install is a skill that proves it worked rather than a page of instructions.
