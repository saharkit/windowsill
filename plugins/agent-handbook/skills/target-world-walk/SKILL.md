---
name: target-world-walk
description: Derive what a plan needs by replaying one concrete unit of work through the system you intend to have, instead of auditing the tickets already written. Refuses a goal that names a tracker item, classifies every need as covered, partial, missing or oral, and runs an ordering pass before anything is filed. Use before committing to a batch of work, when a large item is fanned out into slices, or whenever someone asks whether the plan is sufficient.
---

# target-world-walk

A **portfolio-completeness instrument**. It is not a review of a change and not an audit of your
tickets. It answers one question: *what must exist for one real unit of work to cross the target
system, and who owns each part?*

It is the forward companion to a backward carrier audit — the ordinary exercise of walking your plan
items and checking each has a ticket and an owner. That audit asks whether **named** work is
carried. This walk asks what the target path requires that **nobody named**. A backward audit is
structurally blind to anything absent from the plan, because the plan is its input.

This file is self-contained. Everything needed to run the method is here.

## Inputs

Take exactly two:

1. **Goal statement** — the desired target world, in plain words. It describes the outcome and the
   target architecture, not the work already filed.
2. **Concrete trace** — one unit of work replayed through the target system, in order. Name the
   actor, boundary, state and hand-off at every transition. Do not summarise the system as "the
   pipeline works."

The goal is deliberately **tracker-blind**. The trace may name the work unit needed to make the
replay concrete, but it must describe the TARGET world rather than enumerate today's implementation.

## First action: refuse a tracker-seeded goal

Before reading the trace or deriving any plan item, inspect the **goal statement only**. Refuse it if
it contains a tracker reference: a hash-number such as `#123`, a phrase such as `issue 123` or
`ticket-123`, an issue URL, or an equivalent identifier from whatever tracker is in use.

Use this deterministic gate, case-insensitive:

```text
refuse if the goal matches: #[0-9]+ | issue\s*#?[0-9]+ | ticket[-\s]*[0-9]+ |
  (https?://[^\s/]+/)*[^\s/]+/(issues|issue)/[0-9]+
```

**Do not silently strip the reference and continue.** Stripping preserves the item's framing while
removing the evidence that it was there — the derivation is still seeded, and now invisibly.

The gate is intentionally conservative: a *possible* tracker reference is a refusal, not an
invitation to guess.

Why this matters more than it looks: a goal carrying an item reference imports that item's scope and
blind spots. What follows can then only rediscover the plan you already have, while wearing the
costume of an independent check. Refusing costs one message; a report you cannot trust costs the
whole exercise.

References appearing in the **trace**, or in evidence gathered after the gate, do not retroactively
invalidate a tracker-blind goal. Classify their ownership honestly.

### Demonstrate the gate before showing a passing walk

A run is not demonstrated by a successful report alone. Show the refusal first:

```text
Input goal: "Deliver #123 through the target-world system."
Result: REFUSED — the goal names a tracker item; rewrite it as an outcome.

Input goal: "A change can be carried from proposal to a durable, reviewed landing while the
scheduler, workers, database, checks and integration step all run in the target world."
Result: ACCEPTED — the goal is tracker-blind; begin the concrete trace.
```

The accepted example demonstrates the input gate. It is not a claim that the target world is already
implemented.

## Assemble the report

For each transition in the trace, ask both questions before moving on:

- **What must exist?** Name the service, boundary, state, credential or identity, data shape,
  scheduler, deployment, observability, or human action that makes this transition possible in the
  target world. Pay attention to the boring transitions: those are the ones currently hidden behind a
  person or a long-lived machine, and they vanish silently when the system moves.
- **Who owns it?** Ownership means **an acceptance criterion that would fail if the need were
  absent**. A title is not ownership. A mention is not ownership. A relationship to a large parent
  item is not ownership.

Record one row per need, in exactly one of four bins:

- **COVERED** — one item owns the need, and its acceptance criterion requires the thing to work.
  State the item *and the criterion that makes the coverage real*.
- **PARTIAL** — an item mentions the need, but its acceptance criterion could pass while the need
  remains broken. State the missing proof. Do not upgrade it.
- **MISSING** — nothing owns the need. This is the primary output, and the class a backward audit
  cannot find.
- **ORAL** — the need exists only in a conversation or in a comment on some other item, never in an
  item that owns it. Cite the source and the unowned need; a comment is not delivery.

Two rules carry most of the value: **do not promote PARTIAL to COVERED** because the item sounds
related, and **do not promote ORAL to COVERED** because the remark was authoritative when made. The
bins describe evidence of ownership, not importance of work.

Then run the ordering pass:

1. Draw dependencies between needs and **flag cycles**. A cycle is itself a finding: two items each
   blocked by the other never become startable, and prioritising harder does not help.
2. Mark every step needing a human hand and **name what schedules it**. A manual step with no
   scheduler is a plan gap even when its implementation item exists — "someone will remember" is the
   commonest silent dependency in any plan.
3. Find the **critical path** through the trace and its furthest-upstream item. That item, not the
   loudest one, controls delivery.
4. **Verify every MISSING row before filing anything.** The walk produces claims; it does not
   authorise creating work and cannot declare a plan complete.

## Emit this fixed shape

```markdown
# Target-world walk: <shortened tracker-blind goal>

Trace: <one concrete unit of work and its target-world path>

## COVERED
- [transition / need] — owner: <item>; acceptance proof: <criterion>

## PARTIAL
- [transition / need] — mentioned by: <item>; missing proof: <what can still be broken>

## MISSING
- [transition / need] — no owner; evidence to verify: <observation>

## ORAL
- [transition / need] — oral source: <where it was said>; filing gap: <what must become owned>

## Ordering pass
- Cycles: <none, or the explicit cycle>
- Human steps with no scheduler: <none, or the explicit steps>
- Critical path: <ordered needs>
- Furthest-upstream item: <item, or "none — MISSING must be verified first">

## Verification before filing
- <who checked each MISSING claim, what they checked, and the result>
```

**State an empty bin; do not omit it.** "None identified" is evidence the bin was considered.
Silence is not, and a reader cannot tell a skipped bin from an empty one.

Keep the report grounded in the trace. Do not backfill unobserved transitions from a list of
existing work, and do not call a row COVERED because a neighbouring item sounds related.

## Two failure shapes this method was built to catch

Recognising them is most of the skill. Neither appears as a missing ticket.

**An epic with several slices under one identifier.** Work is split into eight slices but only the
parent has a number. The first slice's change carries a close-directive naming the parent, so the
parent closes when slice one lands — seven slices unbuilt, and everything waiting on "the parent is
done" released onto a foundation that never moved. Every slice was known; nothing was missing from
anyone's head. **A number is not a carrier:** one work item is one deliverable change, so an epic
with eight slices under one number is *zero* carriers, not eight.

**An acceptance criterion that passes while the work is undone.** Two related items each had an
owner and a landed change, yet closure was keyed to *a* landing rather than to the completion of
*that specific item*. "It has a ticket" and "its acceptance catches non-delivery" are different
properties, and only the second protects you.

Both are ownership failures, not knowledge failures — which is precisely what the four bins expose.

## Triggers and boundaries

Run this skill:

- **before committing to a batch of work**;
- **whenever a large item is fanned out into slices**; and
- **whenever someone asks whether the plan is sufficient.**

**Limits, which are part of the method rather than a disclaimer.** One trace covers one path only:
an incident trace or a rollback trace derives different needs, and a different class of work exposes
different gaps — running the walk once does not exhaust it. The report is one reader's claim, not an
instrument reading; every MISSING row is verified before anything is filed. A completed walk cannot
certify that a plan is complete — it reports only what this trace required and what nobody owned.
And it is not a per-item review gate: whether a single change is good is a different question, asked
by different means.
