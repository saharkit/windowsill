# Can a GitHub merge queue be made to run one build instead of N?

Four pull requests enter a merge queue. GitHub builds four candidate branches and runs the full suite
four times. Each candidate's tree contains the previous one, so the fourth already tests everything the
first three would.

The question gets asked in public and the answers are all reasoning. This is a rig that measures it,
and the reasoning turned out to be wrong twice in two different directions.

**The short answer: no, and the interesting part is why.** The number of builds is GitHub's to decide
and nothing in a workflow changes it. What a workflow *can* change is what those builds COST — and the
obvious way to do that merges broken code.

---

## The rig

Four files, one throwaway repository. No toolchain to install — `gh`, `jq`, `python3`, and `git` are
already on any reasonable dev box.

| file | what it is |
|---|---|
| `queue-mode.yml` | the instrument — one workflow, three jobs, four modes selected by the repository variable `QUEUE_MODE` |
| `check.sh` | the stand-in for an expensive suite |
| `arm.conf` | the manifest: which cell of the grid this is, and which batch member is the bad one |
| `run-arm.sh` | the driver — runs one cell end to end and prints its verdicts |

**One workflow for every cell.** The mode is a repository variable and the grouping strategy is a
ruleset field, so a cell is *(mode × strategy × where-the-breakage-is)* and every cell runs
byte-identical workflow code. A difference in outcome can never be a difference in the instrument.

**Three jobs. `decide` and `gate` run on the cheap hosted pool; `suite` stands in for the scarce one.**
The rig uses `ubuntu-latest` for all three so the demonstration stays self-contained — on a
self-hosted pool, switching `suite`'s `runs-on` to the scarce pool is the natural change. **A job
whose `if` is false is never dispatched and claims no slot at all** — strictly stronger than switching
`runs-on`, which still claims one. And `gate` carries `if: always()` because a *skipped* required
check leaves the queue waiting forever; one context that always reports is what makes any of this
legal.

**The PR check and the queue check differ on purpose.** `check.sh` passes unconditionally on
`pull_request` and applies the real rule only on `merge_group`. That is not a rig convenience — it is
what real repositories already do, an affected-set split on the PR and the full suite in the group.
It is also the only way to measure a breakage in the FIRST member: a pull request that is red on its
own never enters the queue at all.

**The manifest makes the rig self-healing.** Each cell advances the arm letter, so a batch that merged
while red leaves files the next cell's manifest does not name. The base branch returns to green by
itself. Without that, one bad cell poisons every later measurement — which is not hypothetical: the
`skip` cells DO merge broken code, on purpose, and something has to survive them.

---

## The verdict function

1. **SOUND** — did anything the suite calls bad reach the base branch? Judged by applying *this cell's*
   rule to whatever the branch actually holds, never by running the branch's own `check.sh`, which is
   vacuous when nothing merged. **One red merge refutes a mode. No saving redeems it.**
2. **LIVE** — did the queue resolve? And "still queued" is two different things: a batch that is
   FALLING (entries leaving as they turn red) is progressing, just slower than the ceiling; a batch
   where nothing moved is a deadlock. The rig captures how many of THIS cell's PRs are queued at the
   start of the watch and how many are queued at the end, and reads verdict off whether that count
   shrank — counted in this cell's terms so a previous cell's leftovers cannot masquerade as progress.
3. **CHEAP** — how many expensive `merge_group` runs actually completed in this cell. The README's
   table cell `4 expensive runs` and the prose claim `One expensive run per merge-group attempt,
   against N` are produced by this verdict, not by the reader's eyeball over the run list.
4. **PROGRESSIVE** — did the good prefix land, or did everything fall? Counted from the files this cell
   left on main: good members vs. bad members.

A cheap unsound mode loses to an expensive sound one, always.

---

## What was measured

### The grouping strategy is not the lever

| strategy | pull requests | `merge_group` runs |
|---|---|---|
| `ALLGREEN` | 4 | **4** |
| `HEADGREEN` | 4 | **4** |

`HEADGREEN` changes what GATES the merge, not how many builds are DISPATCHED. That is what the two
rows above measure; the rig did not read or test the documentation, so it makes no claim about what the
documentation says.

**Side finding: candidates form a CHAIN, not a tree.** Each candidate's base is the previous
candidate's head, so depth relative to its own base is always 1 and the last candidate's tree contains
them all.

**A rig trap worth its own line:** with no *required* status check, the queue merged all four with
ZERO `merge_group` builds. Zero runs is not "N for one" — it is "the queue had nothing to wait for".

### The modes, on the shape that separates them

The breakage is in the THIRD of four members, so the deepest survivor after the head is ejected is
itself genuinely red.

| mode | `ALLGREEN` | `HEADGREEN` |
|---|---|---|
| `always` | sound, resolves, **4 expensive runs** | sound, resolves, **4 expensive runs** |
| `skip` | **REFUTED — merged a red tree** | **REFUTED — merged a red tree** |
| `wait` | sound, **DEADLOCK** | sound, **DEADLOCK** |
| `atomic` | **sound, resolves** | **sound, resolves, and clears fastest** |

**`skip` merges broken code, and the mechanism is not the one you would guess.** It is not an ejection
race. In both `skip` cells, `min_entries_to_merge_wait_minutes` expired with fewer than `min` entries
green and the queue merged **the largest green run from the front** — and a no-op has made "green" mean
"did not look". Measured twice, at 5m34s and 5m36s after enqueue. The rig did not test whether this is
the documented behaviour, or the only behaviour. Verified by hand on the candidate ref before the merge and on the
base branch after it: the suite exits 1 on both.

**`wait` is sound and deadlocks, under both strategies.** A shallow candidate that skips only once a
deeper entry reads MERGEABLE never issues green on credit — so at timer expiry there is no green prefix
and nothing merges. But a red head that GitHub does not promptly eject holds every shallower candidate,
and the whole batch sits unchanged until a timeout.

`HEADGREEN` was the plausible escape: if only the head gates, a red head might be ejected promptly and
the deadlock would be a property of collecting every verdict rather than of waiting. It is not. Under
both strategies the batch sat with three entries at `AWAITING_CHECKS` and the fourth already
`UNMERGEABLE`, and nothing moved for the whole watch. The deadlock belongs to the mode.

**`atomic` is the answer.** A shallow candidate mirrors the deepest verdict it can see: deeper green
means skip, **deeper red means red immediately**, no deeper means run. The only green ever issued
stands behind a real run, so no untested tree can reach the base branch — and there is nothing to wait
on, so no deadlock. By construction, one expensive run per merge-group attempt against N — the rig
prints the runs it saw but does not total them, so this figure follows from the design rather than from
a count.

Paired with `HEADGREEN` it also clears fastest, and for a reason that has nothing to do with the flake
tolerance the strategy is usually argued for: under `ALLGREEN` the queue collects every candidate's
verdict before it can clear, so a collapsing batch is still falling; under `HEADGREEN` only the head
gates, and the mode that deliberately turns the whole batch red has one verdict to deliver instead of
four.

**Neither half is sufficient alone.** `HEADGREEN` without `atomic` merges broken code exactly as
`ALLGREEN` does. `atomic` without `HEADGREEN` is correct but slower to clear.

**The table does not move when the batch gets worse.** The same modes were run again with three of the
four members red, and again with all four, under both strategies. Every soundness verdict is
unchanged; `always` and `atomic` still return the queue to empty, and `wait` never returns the queue
to empty in any cell, at any density. What the denser batches add is a negative worth stating: past a
certain amount of red there is no green prefix longer than one, so the grouping strategy has nothing
left to choose between and the two columns become literally identical. The strategy earns its keep on
the ordinary shape, not on the bad one.

---

## Three things this rig got wrong before it got them right

They are here because the wrong turns are more useful than the answer.

**1. It measured a stricter question than the one asked.** The first attempt tested "skip once the
deeper candidate is GREEN", found that no candidate ever observes a deeper one as green — all of them
are dispatched inside a four-second window — and concluded the whole route was closed. That was never
the proposal. The actual one needs no knowledge of the deeper run's state: a non-head candidate's tree
is a strict PREFIX of the head's, so whatever it would test, the head tests too. The strict version
was simply the one there was an instrument for, and a negative result *feels* like honesty. It is not,
if it answers a different question.

**2. The grid ran only its safe shape and came back all-green.** Eight cells, every mode, every
strategy — and every one read SOUND, including both `skip` cells. Six of the eight put the breakage in
the LAST member, which is the one candidate `skip` always runs. **A grid that never constructs its
worst case recommends the refuted mode.** The position of the breakage is not a refinement of this
experiment; it carries the whole result.

**3. A verdict function too coarse to tell "slow" from "stuck" condemned the correct design.** The
liveness check first reported STUCK for a batch that was falling correctly but had not finished inside
the watch window. Same word, two opposite meanings. The fix was to measure whether the entry set
shrank — and then to re-run the affected cells at one common ceiling rather than caveating the
difference in prose, because a caveat is not a controlled variable.

Both re-runs returned the verdict their originals had. The correction changed no conclusion, and it
was still the right spend: before it, two cells in the table had been watched for less time than the
cells they were being compared against, and a table whose squares were not measured the same way is
not a grid — it is a list of results that happen to share a border.

---

## What this does NOT establish

- **The accusation step is not built.** A red head's failure names the failing tests, and a failing
  test is attributable to the pull request that added it or touched what it exercises — so an ejected
  group could be re-queued without the accused candidate, one attempt per accusation instead of a
  bisection. A wrong accusation would cost one more attempt and could never cost a broken base branch,
  because the safety is not in the attribution. That is reasoning, not measurement.
- **`wait`'s deadlock cause.** What was measured is that a candidate GitHub had already marked
  UNMERGEABLE sat in the queue while every shallower candidate held. *Why* GitHub leaves it there is
  unknown.
- **Why the numbers matter more on a bounded pool.** On a self-hosted pool, `merge_group` builds and
  ordinary `pull_request` builds draw from the same runners, so N−1 redundant candidate builds do not
  merely cost money — they contend with every other build the repository is running.

---

## Turning it on in your own repository

Everything above is a rig for answering one question. Turning any of it on for real is a separate
act, and it has two halves that live in two different places.

**Half one is code, and it lives in the repository.** `queue-mode.yml` is the whole shape: a cheap
`decide` job, an expensive job (`suite`) dispatched only when `decide` says to run it, and a `gate`
job that reports `if: always()` so the required check never goes silently missing. Copying this half
alone changes nothing — the workflow only runs jobs differently; it does not create a merge queue,
does not make anything required, and does not set a grouping strategy.

**Half two is configuration, and it does not live in the repository at all.** It is the `merge_queue`
rule on a branch protection ruleset — repository settings, reached through the GitHub API or UI, not
through a file `git` tracks. A reader who copies the workflow and stops there has shipped half a
mechanism: nothing enforces that the workflow's decisions are the ones the queue obeys, because
nothing has told the queue to run as a queue at all, let alone which strategy or thresholds to use.

### The merge_queue fields, and what this kit sets them to

This kit's own `merge_queue` rule is Terraform, in a separate infrastructure repository
(`sahar-infra`), not in the repository the queue governs
(`modules/governance/main.tf:207-242` on `origin/main` of `sahar-infra`; line numbers below are
against that ref, read with `git show origin/main:modules/governance/main.tf` — a bare working-tree
read of that file can be behind `origin/main`, see the next section). The table below reads that
block field by field, with the ratified reasoning the comments there carry — including the ticket
that changed each value away from its previous setting, where one is on record.

| field | what it does | this kit sets it to | why |
|---|---|---|---|
| `merge_method` | which merge strategy the queue uses to land a group | `SQUASH` | no comment in the file ties this explicitly to the queue, but the same file's `github_repository.kit` resource permits only squash merges on the repo (`allow_squash_merge = true`, `allow_merge_commit = false`, `allow_rebase_merge = false` — `main.tf:86-88`); the queue's merge method is the only one the repository itself allows |
| `max_entries_to_build` | how many queue entries GitHub speculatively builds at once, each a full suite | `2` | was `10`. At 10, one ejection invalidated and re-charged the whole ten-way speculative fan-out — a 10x spend amplifier. 2 keeps the queue pipelined (the next group builds while the head merges) without the amplification (kit#1425, `main.tf:209-215`) |
| `max_entries_to_merge` | the cap on how large one merge group can be | `10` | was `5`. A ≤10 all-green batch becomes one group-build (~14 min) instead of splitting into more, smaller ones (~28 min for a 9-PR harvest wave observed at the time). Kept modest at 10, not 20, because `ALLGREEN` re-bisects the whole group on a single-PR failure — a bigger cap is a bigger blast radius (kit#787, `main.tf:216-219`) |
| `min_entries_to_merge` | how many entries the queue waits to accumulate before it will merge at all | `3` | was `1`. At 1, the queue merged as soon as one entry went green, so a wave of PRs drained serially — about one CI cycle per PR — despite the group-build cap above allowing more. Raising the floor to 3 makes the queue accumulate a batch first, so one group-build/merge covers 3+ PRs under a wave (kit#1088, `main.tf:220-225`) |
| `min_entries_to_merge_wait_minutes` | how long the queue waits to reach that floor before merging what it has anyway | `5` | paired with the row above: this bounds the latency of a lone PR on a quiet day — it merges alone after 5 minutes rather than waiting indefinitely for company (`main.tf:220-226`) |
| `grouping_strategy` | `ALLGREEN` (every entry in a group must report green) or `HEADGREEN` (only the head's verdict gates the group) | **declared** `HEADGREEN` (`main.tf:227-240` on `origin/main`, changed from `ALLGREEN` by kit#301, operator-ratified 2026-08-28); **live, in force right now** `ALLGREEN` — see the next section, this is the one field where the two disagree | kit#301's comment gives the reason (`main.tf:227-239`): the strategy does not change how many builds are dispatched — that saving lives in the workflow's `atomic` mode, not here — it only changes what tolerates a flake. Under `ALLGREEN` one flaky context anywhere in the group holds the whole group; under `HEADGREEN` the group clears on the head's verdict alone. Declared safe to flip alone because the strategy changes nothing about what is dispatched, so it cannot change what is tested — that was measured identical under both strategies across the whole grid in the rig above |
| `check_response_timeout_minutes` | how long the queue waits for a required check to report before treating the entry as failed | `60` | no comment in this file states why 60 specifically; treat it as a value to size against how long your own required check can legitimately take, not as a load-bearing constant to copy uncritically (`main.tf:241` on `origin/main`) |

Two of the seven fields (`merge_method`, `check_response_timeout_minutes`) have no ratified rationale
on record in this file — that gap is stated here rather than filled with a guess.

### The queue obeys the live ruleset, not the file that describes it

This kit's own merge-queue configuration is Terraform, in a repository (`sahar-infra`) different from
the one the queue governs (`sahar`) — a config file a reader browsing `sahar` would never see. That
split is this kit's own arrangement, not a recommendation; state it as an instance of the general
problem instead: wherever your configuration lives, a `terraform plan` (or a saved UI form, or a
draft `gh api` payload) that has not been *applied* is not a setting. The queue enforces whatever the
live ruleset holds, and only that — never the file, however recently it was reviewed and merged.

This kit is, right now, its own worked example. Three readings, taken 2026-08-29:

| where | value of `grouping_strategy` |
|---|---|
| `sahar-infra` local working tree (`2b659e9`, a deliberately-held-back checkout, 3 commits behind) | `ALLGREEN` (`main.tf:227` on that commit) |
| `sahar-infra` `origin/main` (`12604ac`) — the DECLARED value | `HEADGREEN` (`main.tf:240`, kit#301, operator-ratified 2026-08-28) |
| live ruleset, `gh api repos/saharkit/sahar/rulesets/18814577` — what the queue actually RUNS | `ALLGREEN` |

The merged, reviewed, ratified commit says `HEADGREEN`. The queue is running `ALLGREEN`. The gap
between them is one `terraform apply` that has not happened yet — nothing more exotic than that, and
nothing that shows up by reading the repository that carries the reasoning. A reader who opened
`main.tf` on `origin/main` and reported "this kit runs `HEADGREEN`" would be repeating a merged PR,
not the queue's actual behavior; every other `merge_queue` field checked the same way (`merge_method`,
`max_entries_to_build`, `max_entries_to_merge`, `min_entries_to_merge`,
`min_entries_to_merge_wait_minutes`, `check_response_timeout_minutes`) agrees between `origin/main`
and the live ruleset on this same date — `grouping_strategy` is the one field currently mid-flight.

Two failure modes sit inside that single example, and a reader adopting this pattern should check for
both, in their own repository: (1) a config file can simply be STALE relative to the branch that
supersedes it — the local working tree above was three commits behind `origin/main` and gave the
pre-ratification answer with nothing marking it stale; read a tracked ref explicitly
(`git show <ref>:<path>`, not a bare file read of an unspecified checkout) rather than trusting
whatever happens to be checked out. (2) even the right ref, read correctly, only tells you what was
DECLARED — it says nothing about whether that declaration was ever applied. "We have it in Terraform"
is not evidence the queue is behaving that way; it is evidence a PR merged. The only check that
answers the question a reader actually has — what will the queue do on the next PR — is one API call
against the live ruleset in their own repository, the same call used in this table.

### How to actually set them

**Through the UI.** Repository Settings → Rules → Rulesets → the ruleset targeting your default
branch → the "Require merge queue" rule → its fields. This is the same rule `run-arm.sh` edits by API
(below) — setup step 2 above already points here for creating one from scratch.

**Through the API**, the shape `docs/merge-queue/run-arm.sh` already runs against a live ruleset
(`run-arm.sh:134-147`): read the ruleset, replace the parameters of the rule whose `type` is
`merge_queue`, `PUT` the whole ruleset back.

```
gh api "repos/<owner>/<repo>/rulesets/<ruleset-id>" > ruleset.json
jq '.rules = [.rules[] | if .type == "merge_queue" then
    .parameters.grouping_strategy = "ALLGREEN"
    | .parameters.min_entries_to_merge = 3
    | .parameters.min_entries_to_merge_wait_minutes = 5
    | .parameters.max_entries_to_build = 2
    | .parameters.max_entries_to_merge = 10
    | .parameters.check_response_timeout_minutes = 60
  else . end]' ruleset.json > ruleset-armed.json
gh api -X PUT "repos/<owner>/<repo>/rulesets/<ruleset-id>" --input ruleset-armed.json
```

`GET` returns fields beyond `rules` (`name`, `target`, `enforcement`, `conditions`, `bypass_actors`,
`node_id`, timestamps, `_links`); `run-arm.sh`'s own filter reconstructs only the fields `PUT` accepts
rather than piping the `GET` body straight back (`run-arm.sh:136-137`) — do the same, or the `PUT`
will fail or silently drop fields the API does not expect on write.

### The order this should happen in

Turn on the workflow's default mode (`always` — every candidate runs the expensive suite; no `decide`
job is even required for this shape) against the default grouping strategy (`ALLGREEN`) first, and
watch it for real traffic before changing anything. "The modes, on the shape that separates them"
above is the reason: `HEADGREEN` without the workflow's `atomic` mode merges broken code exactly as
`ALLGREEN` does, and `atomic` without `HEADGREEN` is sound but slower to clear. Neither the workflow
change nor the ruleset change is sufficient alone — landing them one at a time is the only way to know
which one moved the outcome you're watching.

### What to watch after switching it on

**How many builds a group dispatched.** `gh run list --repo <owner>/<repo> --event merge_group --json
status,conclusion,headBranch` lists the `merge_group` runs directly — this is exactly the query
`run-arm.sh` uses for its own CHEAP verdict (`run-arm.sh:281-287`), reading completed runs rather than
trusting a job's own log to say how many siblings it had.

**Whether the saving is real.** A job whose `if` evaluates false is never dispatched and claims no
runner slot at all — check this from the run list itself: a `suite` job with no run recorded for a
given `merge_group` attempt was skipped, not merely fast. Counting recorded `suite` runs against the
number of candidates in a group is the same measurement this rig's CHEAP verdict makes; a saving that
cannot be shown this way is assumed, not measured.

---

## Reproducing it

**Prerequisites.** A throwaway repository with a merge queue on its default branch; a `gh` token
authenticated with full `repo` scope on that repository (the script force-pushes branches, opens and
auto-merges PRs, rewrites the ruleset, and sets repository variables — a read-only token is not
enough); `jq` and `python3` on the PATH; a working `git push` to the throwaway repo. `gh auth status`
should report an authenticated account before the first arm runs.

**One-time setup on the throwaway repo:**

1. **Default branch named `main`.** `run-arm.sh` hardcodes it — `git fetch -q origin main`,
   `git checkout -q -B ... origin/main`, and `gh pr create --repo "$R" --base main ...` all name the
   branch literally. A throwaway repo whose default branch is called anything else will not work
   without editing the script.

2. **A ruleset that already has a `merge_queue` rule, targeting `main`, enforcement active.**
   `run-arm.sh` does not create a ruleset — it `gh api`-reads one by numeric id and `PUT`s it back with
   two edits: any rule of type `merge_queue` gets its `min_entries_to_merge`,
   `min_entries_to_merge_wait_minutes`, `check_response_timeout_minutes` and `grouping_strategy`
   overwritten, and any existing `required_status_checks` rule is replaced with one that names `gate`
   as the only required context. The jq filter only touches a rule it finds typed `merge_queue`; if the
   ruleset does not already carry one, the `PUT` writes back a ruleset with no merge queue at all and
   every later step stalls. Create the ruleset first (repo Settings → Rules → Rulesets, or `gh api
   repos/<owner>/<repo>/rulesets` to create and then list it), enable its merge queue rule, and read off
   the numeric id for `RULESET_ID` — the script only ever edits it in place, never discovers it.

3. **`queue-mode.yml` copied to `.github/workflows/queue-mode.yml` on `main`.** This is the article's
   "1 file to copy": GitHub only discovers workflow files at that path, and it is the workflow's `gate`
   job (job id `gate`, no `name:` override, so the check reports under that literal name) that produces
   the `gate` status the ruleset above requires. Copy it and let it land on `main` — by a merge or a
   direct push, whichever the throwaway repo allows — before the ruleset is armed with `gate` as a
   required check, or the queue will wait on a context nothing ever reports.

4. **`ci/check.sh` and `ci/arm.conf` do not need to be copied by hand.** `run-arm.sh` writes both itself
   at the start of every cell, into `$SANDBOX_CHECKOUT/ci/`, from a Python heredoc embedded in the
   script — the copies committed at `docs/merge-queue/check.sh` and `docs/merge-queue/arm.conf` are that
   generated output, kept here to read, not to place. Nothing to do for these before the first arm.

5. **"Allow auto-merge" enabled on the throwaway repo** (Settings → General → Pull Requests).
   `run-arm.sh` merges each member with `gh pr merge "$pr" --repo "$R" --squash --auto`; without the
   setting, that call has nothing to enable and the PR never enters the queue.

6. **`SANDBOX_CHECKOUT` is a local clone of the throwaway repo with `origin` resolving to it and push
   already working** — the same access the Prerequisites paragraph above already asks for (full `repo`
   scope, a working `git push`), pointed at a directory on disk. `run-arm.sh` runs `git fetch`,
   `git checkout -B`, and `git push -f` against `origin` inside this checkout for every cell.

**The five configuration values**, each read through a `: "${VAR:?...}"` guard in `run-arm.sh`
(lines 21-25), whose messages are quoted here:

| variable | guard message | holds |
|---|---|---|
| `SANDBOX_OWNER` | "set SANDBOX_OWNER to the org/user that owns your throwaway repo" | the org or user that owns the throwaway repo |
| `SANDBOX_NAME` | "set SANDBOX_NAME to the throwaway repo name" | the throwaway repo's name |
| `SANDBOX_REPO` | "set SANDBOX_REPO to <owner>/<name> of your throwaway repo" | `<owner>/<name>` — passed to every `gh ... --repo` and `gh api repos/$R/...` call |
| `SANDBOX_CHECKOUT` | "set SANDBOX_CHECKOUT to a clone of your throwaway repo" | the path to the local clone from setup step 6 |
| `RULESET_ID` | "set RULESET_ID to the merge-queue ruleset id on that repo" | the numeric id of the ruleset armed in setup step 2 |

**Running one arm.** Once setup is done:

```
export SANDBOX_OWNER=<owner>
export SANDBOX_NAME=<repo-name>
export SANDBOX_REPO=$SANDBOX_OWNER/$SANDBOX_NAME
export SANDBOX_CHECKOUT=/path/to/local/clone/of/$SANDBOX_NAME
export RULESET_ID=<numeric id from setup step 2>
bash docs/merge-queue/run-arm.sh A NONE ALLGREEN 1 3 atomic
```

The six positional arguments are documented at the top of `run-arm.sh` (lines 2-10): arm letter
(advance it — `A`, `B`, `C`, ... — on every cell, never reuse one), an instrument PR number to wait on
first or `NONE`, the grouping strategy (`ALLGREEN` or `HEADGREEN`), `min_entries_to_merge_wait_minutes`,
the comma-separated bad member indices (`3` puts the breakage in the third of four, the shape that
separates the modes in the table above), and the mode (`always`, `skip`, `wait`, or `atomic`). One
invocation drives one cell end to end — sets `QUEUE_MODE`, re-arms `ci/check.sh` and `ci/arm.conf` for
the new letter, opens four pull requests, arms the ruleset, waits for each PR's own `gate`, merges the
green ones with `--auto`, watches the queue for up to six minutes, and prints the SOUND / LIVE / CHEAP /
PROGRESSIVE verdicts read out in the sections above. **Start each cell against a freshly-empty merge
queue** — the script's own cleanup step is best-effort and cannot recover a cell that left entries
mid-merge.

`skip` is REFUTED on purpose: it merges broken code in both grouping strategies (see "The modes, on the
shape that separates them" above). Reproducing that cell means the throwaway repo's `main` receives a
tree the suite calls bad — reserve this rig for a repository nobody depends on.

What is not written above is not determinable from the four files: how `min_entries_to_merge_wait_minutes`
should be sized against a real check's runtime, and whether a repository other than one with an
already-configured merge queue ruleset can be brought to that state through any path other than the
GitHub UI, are both outside what `run-arm.sh`, `queue-mode.yml`, `check.sh`, and `arm.conf` say.
