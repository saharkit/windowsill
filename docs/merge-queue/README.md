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

`HEADGREEN` changes what GATES the merge, not how many builds are DISPATCHED. GitHub's documentation
permits both readings and settles neither; the first is the true one.

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
race. `min_entries_to_merge_wait_minutes` expires with fewer than `min` entries green, and GitHub
merges **the largest green PREFIX** — and a no-op has made "green" mean "did not look". Measured twice
at 5m34s and 5m36s after enqueue. Verified by hand on the candidate ref before the merge and on the
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
on, so no deadlock. One expensive run per merge-group attempt, against N.

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
  merely cost money — they contend with every other build the repository is running. That effect was
  observed on a real repository, not on this rig.

---

## Reproducing it

**Prerequisites.** A throwaway repository with a merge queue on its default branch; a `gh` token
authenticated with full `repo` scope on that repository (the script force-pushes branches, opens and
auto-merges PRs, rewrites the ruleset, and sets repository variables — a read-only token is not
enough); `jq` and `python3` on the PATH; a working `git push` to the throwaway repo. `gh auth status`
should report an authenticated account before the first arm runs.

**One-time setup on the throwaway repo:**
