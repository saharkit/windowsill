# Achievements — what the shelf and the school can prove

A ledger of milestones. It has two rules, and they are the whole point of the page.

- **Proof, or it is not an entry.** Every row carries a **date** and a **proof link** — a merged
  pull request, a catalog listing, a published counter, a workflow run. What the public record
  cannot prove, this page does not claim. There are no participation trophies here, and "we
  basically did it" is not a row.
- **Never before the thing it claims.** A row lands in the *same* pull request as the thing that
  proves it, or in a later one — never in advance. A row that anticipates is a lie with a date
  on it.

Two consequences worth stating, because they are what makes the ledger cheap to trust:

- **Proof links are public.** This repository's issues, pull requests and Actions runs; the
  official catalog; a public counter. Nothing here points at an internal tracker or a private
  session (see the public-repo hygiene rules in [CLAUDE.md](CLAUDE.md)). A milestone whose only
  evidence is private is not claimable here and stays unclaimed — that is the rule working, not
  the rule failing.
- **A "first" is a claim about everyone else**, so it carries its method: the row's proof link
  must show *how* the field was checked and *when*, not just that the thing happened.

Each rung below has a stable id (`shelf-install-1`, `school-qa-loop`, …). The pull request that
earns a rung names it by id and adds the row in the same breath. Ids are never reused and never
repointed at a different rung.

---

## Shelf ladder

External, and every rung is measurable by someone who does not work here.

| date | achievement | proof link |
|---|---|---|
| — | *no entry yet* | — |

**Rungs not yet earned**

- **`shelf-install-1`**, **`shelf-install-10`**, **`shelf-install-100`**, **`shelf-install-1000`** —
  external installs. *Proof:* the install counter on the official catalog listing, which is
  server-side and therefore not ours to inflate. The counter only exists once the shelf is listed,
  so these rungs are gated behind `shelf-catalog`. An install by anyone in the school does not
  count toward the first rung.
- **`shelf-catalog`** — accepted into the official Anthropic catalog, through a real policy scan
  rather than a self-listing. *Vehicle:* issue #20. *Proof:* the merged submission plus the live
  listing.
- **`shelf-support-contour`** — first plugin in that catalog shipping a full support contour.
  *Proof:* the merged support wave (#48, #55, #57, #58), and a re-run of the survey from #59 that
  measured the vacuum in the first place — 69 trees, doctor in 2 of them, consent-gated bug
  reporting in 0. The claim is grep-checkable against the catalog, so the row links the dated
  re-run, not the original survey.
- **`shelf-outside-bug`** — first bug report from outside the school arriving through
  `/report-bug` (#55), i.e. the reporting path proving itself on a stranger. *Proof:* the filed
  issue, opened by an account with no school affiliation.
- **`shelf-outside-pr`** — first outside pull request merged, through the passport's own two-lens
  bar with no exemption. *Proof:* the merged PR with both review lenses recorded on it.
- **`shelf-outside-tale`** — first tale accepted from outside the school, through the
  tale-contribution path (#63). Canon authorship is curated, not merged by CI: the lore-keeper
  continuity lens (registry resolution + non-contradiction) advises, and the teller-editor's cold
  read + signature accepts. *Proof:* the merged tale's pull request together with the
  teller-editor's signature on it.

## School ladder

Named students, public progress, per the school contract. Once a student keeps a ledger of their
own, the achievement cell links it; until then the row names the student and the proof link
carries the evidence.

| date | achievement | proof link |
|---|---|---|
| — | *no entry yet* | — |

**Rungs not yet earned**

- **`school-conformance-1`** — first end-to-end conformance run. *Proof:* the harness from #56 plus
  the tester's dated report of running it.
- **`school-qa-loop`** — found → fixed → verified: the first QA-filed bug whose fix lands **and** is
  re-verified by the person who found it. Six candidates are already open (#48–#53). *Proof:* three
  links in one row — the issue, the pull request that fixed it, and the finder's re-verification on
  it. A fix merged without the finder's re-check does not earn this rung.
- **`school-merge-marshal`** — first ceremony queue driven end to end by a student. *Proof:* the
  queue's own run and the pull requests it carried.
- **`school-own-voice`** — a student's cloned voice serving in the contour (the #47 path). *Proof:*
  the pull request that wired it in, and an artifact it actually voiced.
- **`school-incident-runbook`** — first incident closed by runbook, unassisted, by an SRE student.
  *Proof:* the incident record and the runbook revision it exercised.

## Factory milestones

Retroactive entries from the chronicle — the work that built what is on the shelf. They are
admitted under exactly the same two rules as everything above, plus the public-proof constraint:
an entry lands only when a *public* link can carry it, which for factory work usually means a
merged pull request in this repository or a workflow run against it. Chronicle prose is a pointer
for finding that link; it is not itself the proof, and an entry whose evidence stays internal is
left out rather than paraphrased.

| date | achievement | proof link |
|---|---|---|
| — | *no entry yet* | — |

---

Kept by **Sahar**. If you can find a row here that its link does not prove, that is a bug — open
an issue and it comes out.
