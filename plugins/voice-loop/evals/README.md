# evals — what a machine can check about this plugin

Everything here is read by `claude plugin eval`, which runs each case as a real agent session with
this plugin loaded and scores the result. It is the **machine-checkable subset** of the plugin's
conformance story; the parts that need a human (a real hotkey, a real microphone, whether you
actually heard it) stay in [TESTING.md](../TESTING.md).

```sh
# from the repository root
claude plugin eval plugins/voice-loop

# one case, streamed, with the no-plugin baseline arm so you can see the delta the plugin makes
claude plugin eval plugins/voice-loop --case config-shape --verbose --ablation with-without
```

A case is a directory holding a `case.yaml` (the other accepted shape, `prompt.md` +
`graders/*.md`, is not used here — a single file keeps the prompt and the assertions in one
reviewable place).

| case | what it pins |
|---|---|
| `config-shape` | the config file this plugin owns: its path, and the keys behind a "German, over my LAN" request |
| `speak-marker-contract` | the green-but-silent install — the missing convention line, not the audio path |
| `cloud-key-indirection` | the secrets rule: a cloud key reaches the config by reference (`api_key_env` / `key_file`), never as a value |

## How these are written, and why that way

- **Free graders only.** Every assertion is a `regex` grader over the final message, so a verdict
  costs nothing beyond the agent run itself and is the same on every machine. The `llm` and
  `baseline` grader types are available and deliberately unused: a judge model would make the
  result drift with the judge.
- **Read-only cases.** No case grants a gated tool (`allowed_tools` is empty and each prompt says
  so in words), so a fresh-machine run needs no `--allow-tools` grant and nothing is installed,
  written or executed on the machine that runs it. Nothing here uses `scaffold_script`, so
  `--scaffold` is never needed either.
- **The assertions are contracts, not vibes.** Each pattern pins a value the plugin's own skill and
  docs state — the config path, `"backend": "lan"`, the marker, the absence of an `api_key` field.
  A case going red means either the plugin stopped teaching that, or the contract moved and the
  case is the thing to update.

## What is not here yet

- **The redaction contract** — the case that pins what a spoken line may never carry lands with
  [issue #55](https://github.com/saharkit/windowsill/issues/55), which is where that contract
  itself is being defined.
- **CI.** These cases are not a gate: they spend model tokens on every run, and the `claude` CLI is
  not on the CI runners. They are run by hand on a real machine, alongside the human-in-the-loop
  conformance pass tracked in [issue #56](https://github.com/saharkit/windowsill/issues/56) —
  this directory is that pass's machine-checkable subset, not a replacement for it. A run's results
  (`evals/results/`, and anything `--output-dir` or `--report` writes) are generated artifacts and
  stay out of the tree — see the repository `.gitignore`.
- **A second plugin's cases.** These live under `plugins/voice-loop/` because evals belong to their
  plugin exactly like its tests do; a second plugin seeds its own `evals/` and the two never share
  a root.
