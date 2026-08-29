# Four pull requests, four full suites, one answer

> This file is the text of record. The published article — the same text with its four figures drawn — is `index.html` beside it, served at <https://saharkit.github.io/windowsill/merge-queue/>. A correction goes into both, and the wording here is the one that wins.

While the project was small, none of this mattered. It grew, and the suite grew with it. A great many tests need a great deal of machine time, and that becomes a choice where both answers cost: run the whole suite on every pull request, or land changes by hand-rebasing each one behind the last. We took the usual way out — a cheap check on the pull request, the expensive suite once, later, where changes are combined before landing. That is what a merge queue is for.

It did not close the problem. The queue runs that full suite once for **every** pull request standing in it. Four waiting to land, four full suites — and the last of them already contains the other three. This article is about that: whether the other three have to run, what happens when you stop them, and which ways of stopping them are safe.

Ask yourself one thing first: do your pull requests ever wait behind one another to land? If they never do, none of this applies to you. If they do, keep reading.

And one measurement, so you can tell whether our numbers are your numbers. Ours: **15,621 tests, 13 to 19 minutes** for a full run — and the count is not what makes the minutes. We measured that too. 93% of the gate is the test step; every linter we run, together, is 30 seconds. 40% of the test step is coverage instrumentation rather than tests. And the floor under all of it is about **226 tests that each start a real process and migrate a real database** — the slowest 1% holds 41% of the work, while the other 10,750 tests are worth at most 47 seconds between them.

That is why deleting tests did not help us, and it is the number to check before it looks like it will help you. A reader with 15,000 fast unit tests has a different problem from a reader with 200 tests that each stand up a database or wait on a browser grid. The redundancy this article is about costs you either way; what changes is what one avoided run is worth.

## §1. How the expensive suite ended up in exactly one place

Nothing here started with a bill. It started with delivery getting slow, and every step after that was reasonable.

**2026-07-12.** The first complaint was that the full suite was the longest step in CI and that everything re-ran it. Six days later the merge queue was live and required, and it was adopted for throughput, not for cost: without it, each merge advanced the trunk, the next pull request had to update and re-run, and the lane was serial at roughly a minute and a half per merge.

**2026-07-20.** "Why has CI got slow?" The test job was the drag — 11 to 16 minutes on a small hosted runner, gating every pull request and re-running in the queue. So the heavy work moved onto machines we already owned and had sitting idle. The same suite that took 11 to 16 minutes finished in 108 seconds.

**2026-07-25 to 27.** Compute moved to a metered cloud builder to survive pull-request waves, and for the first time a bill existed. Within two days it read about $100 a day, against nothing before. On the worst day, 2026-07-26, the queue ran about 189 builds — roughly 2.3 per pull request — and every one of them ran the full suite. The gating work went back onto owned metal, and the hosted builder kept only what volume does not touch: container images, the registry, the deploy.

**And this is the move that matters.** To stop paying for the full suite on every pull request, a pull request now runs only the tests its change affects. The price was stated up front: a pull request can pass its own check and still fail the full suite. So the full suite runs where changes are combined before landing — the merge queue.

Note what that is not. **No tests were deleted.** The suite went from 4,936 that July to 15,621 today; it has never once shrunk. When we did charter a deletion campaign, three weeks later, our own measurement dissolved it: the gate is 1007 seconds, 939 of them the coverage step, and deleting 88% of the suite buys at most 47 seconds. Selection, not deletion, was the lever — and selection is what moved the expensive suite into one place.

Nobody drew the consequence at the time. Once the pull-request tier runs a subset, the full suite runs in exactly 1 place: the merge queue, once per pull request in the group.

On 2026-08-20 that queue ran the full suite 60 times and landed 34 PRs. That is 1.76 full suites per landing, or 12.5 machine-hours in a day, on a shared pool of 4 machines. A successful run took a median of 14 minutes. 22 of the 60 failed, and a failed run costs what a successful one costs: 13.5 minutes against 14.

Nobody had been counting builds on that tier.

The cost of CI is 2 numbers multiplied: how many times a suite runs, and what 1 run costs. Every move above went after the second number — faster machines, a cheaper tier, fewer tests per pull request. Not one of them touched the first. And the last of them moved the expensive suite into the one place that charges per group member.

## §2. The answer, paid in the currency we just promised

A merge queue tests pull requests in **groups**: several PRs taken together, decided as a set. Ours runs the whole suite once per pull request in the group. Done differently, it runs the suite **once for the group instead of 4 times**. That saves about 19 machine-minutes per pull request landed, for about 4 minutes more waiting each.

There are 4 ways to run a group; only one is both safe and cheaper. One merges broken code; one can freeze the group; one is safe but costs what we already pay. The 4th is this article's — our own candidate designs, written into a workflow and tested.

The saving is an upper bound, measured on a sandbox rig — nothing here runs on production; the reason is in §8.

<!-- FIGURE D3 — a 4×2 matrix, rows labelled in plain English by what each merge-queue mode does, four words of verdict in each cell, no mode names or cell letters — drawn on the published page: index.html -->

## §3. What the queue buys and sells

Without a queue, landing several PRs against one trunk means rebasing each by hand whenever an earlier one lands. Its checks re-run too, with a person standing inside that loop.

<!-- FIGURE D1 — the hand-rebase loop drawn beside the queue's loop, with a human figure standing inside the hand-rebase one and outside the queue's — drawn on the published page: index.html -->

A merge queue removes the person from the loop. It takes several PRs, orders them, tests each against the ones ahead of it, and lands the ones that pass. That is what it buys: serialization without a human doing the serializing.

Speculative gating — test the queued changes as the group that would land, merge what passed together — predates GitHub. [Zuul](https://zuul-ci.org/) has done it in the open, self-hosted, longer than GitHub's queue has existed. In [its own deprecation notice](https://github.com/bors-ng/bors-ng), bors-ng directs its users to GitHub's merge queue. GitHub put the machinery behind a setting: no operator, no separate service; a team of 2 can turn it on.

What it sells is a build per pull request in the group, every time — paid by every build queued behind these. Without a queue a person pays a run per rebase too; the work moves, it does not disappear. The queue charges what manual rebasing did — visibly, all at once, on a bill someone eventually reads. That visibility raises the question: does every member of a group need its own full run? Or does the last one already contain the answer for all of them?

## §4. 4 pull requests, 4 builds

Candidates in a merge queue form a chain: each starts where the previous one ended. The last candidate contains everything the ones before it contain. If the 4th candidate passes, the first 3 passed inside it. If it is red from something the 1st broke, that break is visible in the 4th's failure. We tested the claim on a throwaway repository built for it.

<!-- FIGURE D2 — four candidates drawn side by side in a chain, each box containing everything to its left, with the pull request that introduced the break shaded inside the two candidates that contain it — drawn on the published page: index.html -->

4 PRs produced 4 group builds, dispatched within a 4-second window of one another. 20–30 seconds later, each build, asked about the others, reported all 4 still awaiting their checks — including itself. Nothing had finished early enough for a shallower build to look at a deeper one and stand down.

This is the redundancy a merge queue sells: 4 builds where 1 verdict would have answered for all 4. It refutes the simple fix, "have the shallow ones just wait and check the deep one". At the moment a shallow build starts, no deeper build has an answer yet to check.

## §5. Is this GitHub's to fix?

We asked whether GitHub's queue already lets you turn the redundancy off. GitHub's documentation says the merging settings do not combine builds: 1 per candidate is the design. We checked every merge-queue setting GitHub exposes, 1 at a time. The rule that decides whether a candidate is judged against everything ahead of it or only the newest entry changes what *gates* a merge. It does not change how many builds get *dispatched*. We measured this directly: 4 pull requests, 4 builds, under both settings.

The other major hosted forge does not batch either: 1 pipeline per request, no setting required. Self-hostable options split the same way. bors-ng — at `bors-ng/bors-ng` — batches the way we want but has been unmaintained since April 2024. [Kodiak](https://kodiakhq.com/docs/config-reference), which is maintained, does not batch at all. Community discussions [#43988](https://github.com/orgs/community/discussions/43988) and [#58523](https://github.com/orgs/community/discussions/58523) raise this exact doubled cost, and neither has a published fix attached. That absence is what makes this worth building ourselves.

## §6. The rig, and what it computes

We built 1 workflow with 3 jobs. A cheap job reads the queue and locates its own pull request. An expensive suite stand-in is gated on that answer; the third is the always-running required check. A job whose condition is false is never dispatched — no machine time at all, which beats routing it to a cheaper machine. The frugality was forced: July's runs had already spent what there was to spend on hosted compute, so unneeded builds were unaffordable. Which of the 4 modes runs is a single repository setting, so every cell runs the same code.

How a merge-group build sees its group-mates at all is GitHub's surface, not the rig's. The build knows which pull request it is because its candidate ref says so — `gh-readonly-queue/<base>/pr-NN-<sha>` — and the decide job takes the number out of `GITHUB_REF` with one `sed`. The queue around it is 1 GraphQL query, sent with `gh api graphql` and the workflow's own `secrets.GITHUB_TOKEN`: `query($owner:String!, $name:String!) { repository(owner:$owner, name:$name) { mergeQueue { entries(first:50) { nodes { position state pullRequest { number } } } } } }`. Each entry returns its `position` — larger is deeper — its `state` (`MERGEABLE`, `UNMERGEABLE`, or neither, still awaiting its checks), and its pull-request `number`. The workflow's whole permission grant is `permissions:` with `contents: read`, `checks: read`, `pull-requests: read`; reading the queue needs no separate token and no write scope.

That query is not quoted from documentation. It was run against this repository's own live queue on 2026-08-29 and the field set confirmed by schema introspection: `MergeQueueEntry` carries `position`, `state`, `enqueuedAt`, `headCommit`, `baseCommit`, `estimatedTimeToMerge`, `solo`, `jump`, `enqueuer` and `pullRequest`, and `entries` carries `totalCount` alongside `nodes`. An empty queue answers `{"totalCount": 0, "nodes": []}` rather than erroring, which matters because that is the shape a decide job sees most often and the one a first implementation is most likely to mishandle.

One trap sits between this query and the configuration table further down, and it is worth naming because it cost a wrong query on the first attempt. The two are different APIs with different names for the same seven settings. The ruleset REST API — the one the setup recipe uses — spells them `grouping_strategy`, `min_entries_to_merge`, `min_entries_to_merge_wait_minutes`, `max_entries_to_build`, `max_entries_to_merge`, `check_response_timeout_minutes`, `merge_method`. GraphQL's `MergeQueueConfiguration` spells the same seven `mergingStrategy`, `minimumEntriesToMerge`, `minimumEntriesToMergeWaitTime`, `maximumEntriesToBuild`, `maximumEntriesToMerge`, `checkResponseTimeout`, `mergeMethod` — and its durations are seconds where the REST names say minutes. Asking GraphQL for a REST name fails loudly with `undefinedField`, which is the good case; the quiet case is reading `checkResponseTimeout` as 3600 minutes. Read this repository's own settings through GraphQL on the same day gives `ALLGREEN`, 3, 300, 2, 10, 3600, `SQUASH` — the same seven values the ruleset reading reports, in the other API's units.

The PR check and the merge-queue check behave differently on purpose: otherwise "break in the first member" and "break elsewhere" would look identical from outside.

We ranked outcomes in a fixed order: soundness first (does anything unsafe merge), liveness (does the group finish), cost, how much lands. The driver — the shell script driving a whole arm — opens the PRs, sets the mode, watches the queue, records what each cell did. It measures soundness and liveness directly; cost and landing we read off the cells by hand.

## §7. The cases: what was tested and how each behaved

4 modes, 1 grid, 4 questions each: set up, expected, happened, refuted.

**Run every candidate.** Set up: every candidate runs its own full suite, independent of the others. Expected: safe, correct by construction. Happened: sound in every run, every group resolved, 4 full suites dispatched every time. It refutes nothing; it is the baseline.

**Skip the ones that are not last.** Set up: only the deepest candidate runs the real suite; the others report success without running it. Expected: 3 fewer builds per group of 4, if the queue's bookkeeping holds. Happened: refuted. The mechanism is a **wait timer**. With minimum entries to merge set to 4 and a 5-minute wait, a group enqueued at 13:33:23 merged at 13:38:57. That is 5 minutes 34 seconds — the timer plus bookkeeping. The second run cleared at 5 minutes 36; the second pair is in the repository, hand-verified. In both runs, when the timer expired with fewer than the minimum green, the queue merged the largest green prefix it had. This refutes the idea that skip's reporting can stand in for a real run: that prefix can include a candidate that never actually ran.

**Wait for a deeper verdict.** Set up: a shallow candidate holds until a deeper one reports. Expected: safe, at the cost of some waiting. Happened: sound, and dead. The head of the group is marked unmergeable, GitHub never ejects it, and every shallower candidate holds behind it. Nothing moves between 80 seconds and 7 minutes in. It stuck in every run, under both grouping rules. The obvious explanation — a property of the stricter grouping rule — fails. The same shape under the looser rule deadlocks identically. This refutes the grouping rule as the cause; after that test we do not know what causes it. Our mode logic treats 'unmergeable' as still undecided; why the platform never ejects the head is what we do not know.

**Fail the whole group together.** Set up: a shallow candidate mirrors the deepest verdict it can see. Deeper mergeable, it stands down; deeper unmergeable, it fails and the group falls; no verdict yet, it runs for real. Expected: safe and live, at the price of losing an entire group to 1 bad member. Happened: sound in every run; every group resolved except 1, at the shortest liveness setting tried.

The sample is 4 modes by 2 grouping strategies — 8 cells, every one with a verdict, none blank. The breakage sits in the 3rd of 4 members. Re-runs with 3 of 4 members red and again with all 4, under both rules, left every soundness verdict unchanged. Not covered: other group sizes, breakage beyond those re-run positions, and the rig under load.

That 1 unresolved group was not a soundness failure — nothing unsafe merged. It was a liveness edge at an aggressive value, resolving cleanly at a longer one. The other 3 modes each gave something up — skip soundness, wait liveness, run-every the saving this article is about. This one gave up nothing on the 2 questions ranked first.

No untested tree reaches the trunk here: every merge stands behind a real run somewhere in the chain, and §8 prices that safety. This is the mode that won. It refutes the assumption that safety and liveness trade off in this design: fail-together buys both.

Turning it on has 2 halves, and only 1 is a setting. The mode is your own workflow logic: a decide job on cheap hosted runners works out where its pull request sits. The suite runs only when it says so. A gate job reports under `if: always()` so a required check is never left unreported; none of it is switchable from a settings page. The skeleton of that half, as the rig runs it — 3 job ids, the edges between them, and the 2 conditions that carry the design. Everything elided is the decide job's script and the gate's reporting body, both in the rig file linked at the end of this section:

```yaml
on:
  pull_request:
  merge_group:

permissions:
  contents: read
  checks: read
  pull-requests: read

jobs:
  decide:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    outputs:
      verdict: ${{ steps.place.outputs.verdict }}
      mode: ${{ steps.place.outputs.mode }}
    steps:
      - name: Decide what this candidate owes
        id: place
        # ...elided: read the queue (§6), write verdict=RUN|SKIP|FAIL to $GITHUB_OUTPUT
  suite:
    needs: decide
    if: needs.decide.outputs.verdict == 'RUN'
    runs-on: ubuntu-latest          # stands in for the EXPENSIVE pool
    steps:
      - uses: actions/checkout@v4
      - run: bash ci/check.sh
  gate:
    needs: [decide, suite]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Report the one context the queue waits on
        # ...elided: RED if decide failed, if the verdict is FAIL, or if the suite ran and did not pass
```

The decide job's wait is bounded, and the bound belongs in print. It polls the queue once every 15 seconds, at most 60 times — 15 minutes of waiting for a deeper entry to reach a verdict — under a job-level `timeout-minutes: 20`. When the wait expires with no verdict, the job does not skip and does not hang: it writes `verdict=RUN`, logged `timed-out (fail safe)`, and the candidate runs the real suite. RUN is the safe expiry because a skip is earned only on positive evidence — a deeper entry actually observed `MERGEABLE` — and every path that cannot decide falls back to RUN: the ref carries no pull-request number, the queue read fails, this pull request is no longer in the queue, the wait ran out. Each lands on the baseline the queue was charging for already, so a slow or unreadable queue costs money and cannot cost soundness — §6's ranking puts soundness first and cost third. That loop is shared with the `wait` mode, and what separates the 2 modes is one branch, the one `wait` does not have. To `wait`, a deeper entry in state `UNMERGEABLE` is merely not green yet — there is no case for an observed red — so the hold runs on, and the group waits on a head the platform is not ejecting. The winning mode turns an observed red into `FAIL` at once, and the batch falls together. That is the whole of the difference between them in the file. Why the platform never ejects the head stays where §10 left it; what the bound buys is that no verdict ever arriving ends in a run, not a hang.

The platform half is GitHub's merge-queue grouping strategy, with 2 values: `ALLGREEN`, every entry passing on its own, and `HEADGREEN`, gating on the head. `HEADGREEN` without the mode merges broken code exactly as `ALLGREEN` does. The mode without `HEADGREEN` is correct but slower: the queue collects every verdict before the batch falls. Build the mode first, observe it, then change the strategy.

Where the break sits matters as much as which mode runs it. The separating shape is the break 3rd of 4: shallow enough something must wait or guess, deep enough the chain has carried it partway. 1 pair of runs — break-in-3rd under both rules — pins the "wait" mode's deadlock on the mode, not the rule. A 2nd run kills the hope that a **head-green grouping rule**, judging a candidate only against the group's current head, would rescue the waiting mode. The mode deadlocks under that rule too.

<!-- FIGURE D4 — the waiting mode beside the fail-together mode on one timeline: one frozen partway across, one falling to red as a block — drawn on the published page: index.html -->

The full grid, every cell identified, is in the rig repository — https://github.com/saharkit/windowsill/tree/main/docs/merge-queue — for anyone who wants to reproduce it.

## §8. What the safe mode costs

"Fail the whole group together" does not win for free. A cell with a defect merges **nothing**: the whole group falls and re-queues. Under "run every candidate" on the same shape, the pull requests ahead of the break still land.

There is a second cost hiding behind the first, and it is the obvious objection: when the group falls, which member broke it? The group's verdict is one bit, and it does not name a culprit. That question has an answer, and we have one, and it is kept out of this article on purpose — a boundary, not a gap. The line is what travels. The job graph, the queue API it reads, and the decide job's wait policy land on any repository with a merge queue, whatever the stack, and all three are above. Naming the member that broke a group does not travel: it reads that repository's own build results with its own tooling, and an answer shaped to ours would be an answer about our stack. The mode does not need it to be safe — nothing merges untested either way — and what it buys back is the retry ceiling at this section's end. That part is the adopter's to build.

Our inputs: 9 defects in 40 changes (22.5%), groups of 4, a 17-minute suite; the table is per pull request that eventually merges. The 17-minute suite is a round figure, and a little generous: §1's measured median was 14, and every machine-minute below scales straight with it. 40 changes is a small sample, and one repository's habits are not another's — the one input to replace with your own.

| | run every candidate | fail together |
|---|---|---|
| builds | 1.82 | 0.69 |
| machine-minutes | 30.9 | 11.8 |
| wait to land | 7.7 min | 11.8 min |

The model and the measurement agree: 1.82 builds per merged pull request here, against §1's measured 1.76 — 60 runs against 34 landings.

The saving is in machine-minutes, not dollars, because a machine-minute's cost depends on whose machine it is. 4 published lists, observed 2026-08-28 — US dollars per minute, Linux x64.

| vCPU | GitHub Actions | Google Cloud Build | Blacksmith | BuildJet |
|---|---|---|---|---|
| 2  | 0.006 | 0.006 | 0.004 | 0.004 |
| 8  | 0.022 | 0.0156 | 0.016 | 0.016 |
| 32 | 0.082 | 0.0624 | 0.064 | 0.048 |

3 of those columns are printed per size. Blacksmith prints 1 figure, $0.004 a minute, beside a vCPU selector that does not restate it. Its 8- and 32-core cells are derived from its own documented rule: minutes are spent in proportion to vCPU count, so 10 minutes on a 4-core runner spends 20 2-core minutes.

Everything there is a Linux minute; the platform is a larger multiplier than the supplier spread. From [GitHub's published minute rates](https://docs.github.com/en/billing/concepts/product-billing/github-actions), observed 2026-08-28: the standard Windows 2-core runner is $0.010 against Linux's $0.006 — 1.67×. Windows [at 32 cores](https://docs.github.com/en/billing/reference/actions-minute-multipliers) is $0.162 against Linux's $0.082, very nearly double. The standard macOS runner at 3 or 4 cores is $0.062 — 10.33× Linux.

This article's arithmetic is about how many times a suite runs, so whatever multiplies the price of 1 run multiplies the whole result. Running the suite 4 times instead of once costs the same 4× on every platform; on macOS each run starts 10 times higher. 1 boundary: public repositories are not billed at all, Windows and macOS included; these figures bite only on private ones.

Substitute your own rate and the money is yours to compute. 3 things the table says on the way past. The specialists hold 1 rate per vCPU and do not bend it. [Blacksmith](https://blacksmith.sh/pricing) charges $0.002 a vCPU-minute at 2 cores and the same at 32; BuildJet matches it until 32, where it drops to $0.0015. The platforms discount the vCPU as the runner grows — GitHub from $0.0030 to $0.0026, [the hosted builder](https://cloud.google.com/build/pricing) from $0.0030 to $0.0020 — but start above both specialists. At 32 cores that discount overtakes them: $0.0624 against the forge's $0.082 and Blacksmith's $0.064. Hosted compute is not uniformly the expensive option. Vendors' comparison claims overstate their own lists. BuildJet leads with "2x faster and cheaper" and a customer quoted "cut in half"; its own list runs 27–41% below.

The column the table does not have is hardware you already own. Its minute costs amortisation plus electricity over the minutes actually run, so an idle machine's minute costs infinitely much. That is §1's capacity ceiling, written as a formula.

The 19 machine-minutes is a ceiling. The model assumes every retry draws a fresh, independent batch. A real failed group does not: it comes back with the same defect still in it. For a genuine bug, retrying the identical group never succeeds. The modes are asymmetric here: "run every candidate" re-queues only past the first broken one; "fail together" re-queues the entire group. So the dynamics the model leaves out penalize the mode we recommend more than the baseline, not less. An audit of the model found both. The saving is real only where something identifies which pull request broke the group between attempts. That step — read a fallen group's results, name the member that broke it, re-queue the rest without it — is not built.

## §9. 3 things we got wrong before we got them right

The first arm had no required status check attached to the queue. It merged all 4 PRs immediately and dispatched **0** group builds. Zero is not a small answer: the queue had nothing to wait on — a different failure than the one we were measuring. The cost was the whole arm: nothing it produced could be reused once the check was added back. A queue with nothing to gate on cannot tell you what gating costs.

The second: our first grid ran only the break-in-first-member shape, and it came back green across every mode. For a while we took it for one. It is the absence of one: the earliest possible break gives every mode its easiest case. A grid built only that way cannot separate a safe design from a lucky one. The cost was a second full grid, with where-the-break-sits added as its own axis. That second pass found the deadlock in the waiting mode, a defect the easy shape had no way to show us.

The third: our first pass at a liveness verdict could not distinguish falling correctly from stuck. 1 cell had 4 entries frozen since 80 seconds in; a different cell had 2 entries already gone. A verdict that read the second as an instance of the first condemned the mode that turned out to be the right one. The fix cost a 2nd, finer verdict function, and a re-read of every cell the coarse one had already called.

Each has the same shape: the cheapest number stood in for the one that mattered, until we measured the harder one. Easy measurements crowd out costly ones by default — nothing forces the harder one to get taken. And with July's spend already made, costly meant costly in the plainest sense.

## §10. What this does not establish, and the objection we cannot answer

We do not know why the waiting mode deadlocks. The one explanation we had — the stricter grouping rule — was refuted in §7; nothing has replaced it.

The contention between merge-queue builds and ordinary pull-request builds sharing 4 runners was observed once, directly, on a real repository. It was not reproduced on the rig, and we have no rate for it — only that incident. Treat it as a reason to test your own repository, not as a claim about the rate at which it happens. The step that would lift §8's ceiling — identifying which pull request broke a group — is described, not built. Nothing here has been rolled out to a real repository.

The objection we would raise against ourselves: the cost claim is derived, not measured, and rests on a number we did not observe. That is true. Our answer: soundness is measured directly, cost is derived from a model with a stated bias, and the 2 do not carry equal weight. The ranking in §6 puts soundness first and cost third.

## §11. Run it yourself

5 configuration values, 1 file to copy, 1 command to run — none printed here. The values, the file, the command, and the 2 command-line tools it needs are written out in the rig at https://github.com/saharkit/windowsill/tree/main/docs/merge-queue. A stranger also needs 3 things: a command line authenticated against GitHub, 2 ordinary command-line tools, and push permission on the target repository.

1 term: a **ruleset** is the named rule set a repository attaches to a branch. It covers required checks, who may push, and whether a merge queue runs at all. The rig needs that ruleset's numeric id, which GitHub's settings page shows next to the ruleset's name.

In bold, because getting it wrong costs something real: the **skip** mode merges broken code. Run this against a repository you do not care about.

§7's minimum-entries and wait-timer values are pinned in the rig's configuration; skip's behavior depends on them. Change them and §7's 5-minute timer no longer applies. This section ships only because the driver runs. The driver was last verified at `ec709e33`, the commit that added it, and is unchanged since; the rig's setup instructions were completed after that, so the current rig is a later commit than the one the driver was run from.

## §12. The bill, again

In runs: today 4 full suites run per group; under the safe mode, about 0.69 per pull request landed. Against the 12.5 machine-hours actually spent on 2026-08-20, the model's 0.69-to-1.82 ratio puts the same day near 4.8 — a saving of about 7.8. Counted the other way, 0.69 runs times 34 landings is about 23 runs. At the measured 14-minute median that is 5.6 machine-hours, a saving of about 7. 2 routes agreeing to within a machine-hour — the most that should be claimed for either, and still an upper bound (§8).

1 framing we tried and withdrew: 4 runners times 24 hours as a 96 machine-hour ceiling. That is a ceiling on utilization at 100%, not a measured capacity limit, and nothing here shows the runners are actually the bottleneck. What we observed directly instead: at 14:43 UTC all 4 runners were busy, and 1 merge build sat queued for 8 minutes. Nothing merged on the repository for 2 hours 20 minutes. Merge builds draw from the same pool as ordinary pull-request builds — a fact about the shared pool, not machine-hours in the abstract.

Money, 1 line: in July we bought TRY 10,920.84 (about $234) of Cloud Build, and in August TRY 237.56 (about $5). The whole of that difference is how much of the service we chose to buy. The move in §1 that answered the July bill — the gating work going back onto machines we already had — carried the high-volume work off the meter. What we buy now is only what stayed: image builds, the artifact registry, the deploy. The bill moved. It did not vanish.

While we spent a month inside the queue, the hosted side built 3 images and kept them in its registry. It deployed to the cluster on every merge, without a single incident of ours. That silence made the month possible: we could take 1 thing apart because everything under it held.

The multiplier we were chasing was never in the price of a run.
