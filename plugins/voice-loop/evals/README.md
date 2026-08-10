# voice-loop evals — the machine-checkable half of conformance

[`CONFORMANCE.md`](../CONFORMANCE.md) is the human-in-the-loop checklist: it asks a
tester to tap a hotkey and say whether they heard a sound. Nothing mechanical can do
that. But a good part of what this plugin promises is not about audio at all — it is
about **what the agent says and does when the plugin is loaded**, and that part a machine
can check.

This directory is that part. Each case pins one contract the plugin's skills carry, in
the shape `claude plugin eval` runs.

## Run them

```sh
claude plugin eval plugins/voice-loop
```

That resolves the plugin from the path and runs every case under `evals/`. To measure
whether the plugin is actually doing anything, run the ablation — the same prompts with
and without the plugin loaded, reported as a score delta:

```sh
claude plugin install voice-loop@windowsill
claude plugin eval voice-loop@windowsill --ablation with-without
```

A single case, or the whole tag:

```sh
claude plugin eval plugins/voice-loop --case report-bug-redaction
claude plugin eval plugins/voice-loop --tag contract
```

Useful knobs: `--runs N` overrides each case's own run count, `--threshold 0..1` sets
the score a case must reach to pass (the default is `1.0` — every grader must pass),
and `--report <path>` writes a self-contained HTML report of prompts, traces and grader
verdicts.

The runner writes `evals/results/<timestamp>/aggregate-result.json` by default. That is
a generated artifact and is gitignored — pass `--output-dir` if you want it somewhere
else.

## What is here

| case | contract it pins | where the contract is written |
|---|---|---|
| `config-diagnosis` | a config value that explains the behaviour is diagnosed as a **choice**, not as a bug or a broken install — `/doctor`'s first bin | [`skills/doctor/SKILL.md`](../skills/doctor/SKILL.md), [`check_manifest.py`](../skills/doctor/check_manifest.py) |
| `report-bug-redaction` | `/report-bug` names the collector as the only redactor, says what never travels (keys, user, host, **all** spoken and transcribed text), and sends nothing without a chosen destination | [`skills/report-bug/SKILL.md`](../skills/report-bug/SKILL.md), [`scripts/report_bug.py`](../scripts/report_bug.py) |
| `cloud-key-never-inline` | a cloud API key goes in a `key_file` the config points at or an `api_key_env` the config names — **never** inline in `config.json` | [`skills/voice-setup/SKILL.md`](../skills/voice-setup/SKILL.md) rule 3 |

## How a case is built here, and why

- **The prompt carries its own inputs.** Every fixture a case needs is inlined in
  `execution.prompt`. No `scaffold_script`, so no case here needs `--scaffold` — which
  runs author-supplied bash as you, and is a trust decision a reader should not have to
  make to run the suite.
- **No gated tools.** `allowed_tools` stays inside `Read`/`Glob`/`Grep`, so no case needs
  the `--allow-tools` operator grant. A case that wants `Bash`, `Write`, `Edit` or
  `WebFetch` has to earn it and say so here.
- **Graded on what the assistant said**, not on files it wrote — these contracts are
  about the answer, and the answer is what a user acts on.
- **The negative graders are narrow on purpose.** `cloud-key-never-inline` forbids the
  key appearing *inside a JSON assignment*, not the key appearing at all: quoting the
  user's own key back while explaining where it will live is fine, writing it into a
  config is the violation.
- **Every case is answerable from a skill body**, so a real run does not depend on the
  agent reaching outside the sandbox for plugin source.

Adding a case: one directory, one `case.yaml`, one row in the table above. Keep it to a
contract that is written down somewhere in this plugin — an eval that pins behaviour no
document claims is a test of the model, not of the plugin.

The machine-checkable eval rail complements the human acceptance pass in
[`CONFORMANCE.md`](../CONFORMANCE.md), the conformance work tracked in [#56](https://github.com/saharkit/windowsill/issues/56), and the directory-readiness work tracked in [#20](https://github.com/saharkit/windowsill/issues/20).
