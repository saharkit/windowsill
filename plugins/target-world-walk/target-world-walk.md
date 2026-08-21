# Target-world walk

The **target-world walk** is a portfolio-completeness method: you derive what a plan needs by
replaying one concrete unit of work through the system you *intend to have*, instead of auditing the
tickets you already wrote.

> **Method rule.** Start from the goal, not from the board. Replay one concrete unit of work through
> the target world. At every transition ask what must exist and who owns it.

It is the forward companion to a **backward carrier audit** — the ordinary exercise of walking your
existing plan items and checking that each has a ticket, an owner and a deadline. The audit asks
whether *named* work is carried. This walk asks what the target-world path requires that **nobody
named**. Both are worth running; they find different classes of gap, and neither substitutes for the
other.

## Why a backward audit is not enough

A backward audit is honest work, and it is structurally blind in one direction: it can only
rediscover what someone already wrote down. Anything absent from the plan is invisible to a method
whose input is the plan.

Two failure shapes motivated this method, and both are worth recognising because neither shows up as
a missing ticket:

**An epic with several slices under one ticket number.** A large piece of work is broken into, say,
eight slices, but only the epic has an identifier. The first slice's change carries a close-directive
naming the epic. The epic therefore closes when slice one merges, with seven slices unbuilt — and
everything waiting on "the epic is done" is released onto a foundation that never moved. Every slice
was known. Nothing was missing from anyone's head. The plan still could not carry it, because a
ticket number is not a carrier: one work item is one deliverable change, and an epic with eight
slices under one number is *zero* carriers, not eight.

**An acceptance criterion that passes while the work is undone.** Two related items each had a
ticket, an owner, and a merged change. The acceptance level still allowed undelivered work to be
reported as delivered, because closure was keyed to *a* merge rather than to *the completion of that
specific item*. "It has a ticket" and "its acceptance catches non-delivery" are different
properties, and only the second one protects you.

Both are ownership failures rather than knowledge failures. That is exactly what the four-bin
classification below is built to expose.

## Ancestry, and what is specific here

The deeper family names are useful navigation, not claims of invention:

- **Walking skeleton** — Alistair Cockburn's thin, end-to-end implementation that proves the major
  joints of a system before any of them is filled in.
- **Tracer bullet** — Hunt and Thomas's small working path through every architectural layer, fired
  early to discover the real shape rather than to deliver the feature.
- **Value-stream mapping** — the lean practice of following one item through the whole flow and
  recording what happens to it at each station, including the waiting.
- **FMEA** — failure-mode and effects analysis: asking, at each step, how this can fail and what
  contains the failure.
- **Critical-path forward/backward pass** — ordering the work and locating the furthest-upstream
  dependency that actually controls delivery.

What is specific to this method is the **four-bin ownership classification** and the **ticket-blind
refusal gate**. Both exist because of the two measured failure shapes above, not because four
categories are a pleasing number.

## The walk

### 1. State the goal without tickets

Write the target system in plain words: what changes in the world if this plan succeeds. Name the
desired outcome and the target-world conditions — not the issue number, not the ticket title, not
the plan already on the board.

**This is enforced mechanically, and the enforcement is the point.** If the goal contains a tracker
reference, the walk refuses before reading anything else. Rewrite the goal without the identifier.

The reason is not tidiness. A goal seeded with a ticket reference imports that ticket's framing,
scope and blind spots. The derivation that follows can then only rediscover the tracker — you get a
confirmation of the plan you already have, wearing the costume of an independent check. Refusing is
cheaper than reading a report you cannot trust.

Do not silently strip the reference and continue. Stripping preserves the framing while removing the
evidence that it was there.

### 2. Choose one concrete trace

Pick **one** unit of work and replay it through the **target** world, in order. Not "the system
works" — an actual sequence.

A generic example, which you should replace with your own:

> A change is proposed; the scheduler picks it up; the work runs on a worker; a review runs; the
> change opens for integration; automated checks run; a human or gate approves; the change lands; the
> work item closes — with the scheduler running as a managed service, the workers as scheduled
> containers, the state in a shared database, and no manual step on the critical path.

At every transition record four things: **actor, boundary, state, hand-off**. Then ask two questions
and answer both before moving on:

1. **What must exist for this transition to happen?** Services, data shapes, identities and
   credentials, scheduling, deployment, observability, and human actions. Pay particular attention to
   the boring transitions — those are the ones currently hidden behind a person or a long-lived
   machine, and they are the ones that vanish silently when the system moves.
2. **Does anything own it?** Ownership means **an acceptance criterion that would fail if the need
   were absent**. A ticket title is not ownership. A mention is not ownership. A broad relationship
   to a large parent item is not ownership.

### 3. Classify every need into exactly one bin

| Bin | Meaning | Why it is a separate bin |
|---|---|---|
| **COVERED** | A work item owns the need, and its acceptance criterion requires the thing to actually work. Name the criterion, not merely the item. | Without this, a board can look complete while the target path is not. The criterion is the evidence; the item is only the address. |
| **PARTIAL** | An item mentions the need, but its acceptance criterion could pass while the need remains broken. Record the missing proof. | This is the "has a ticket, does not catch non-delivery" shape. Promoting it to COVERED is how undelivered work gets reported as delivered. |
| **MISSING** | Nothing owns the need. | This is the primary output — the class a backward audit structurally cannot find. |
| **ORAL** | The need exists only in a conversation, a chat ruling, or a comment on some other item — never in an item that owns it. | A decision everyone remembers is not a decision the system enforces. Comments are authoritative in conversation and invisible to execution. |

Two rules that carry most of the value:

- **Do not promote PARTIAL to COVERED** because the item sounds related.
- **Do not promote ORAL to COVERED** because the comment was authoritative when it was written.

The four bins describe **evidence of ownership**, not the importance of the work. An important need
with no owner is MISSING, not COVERED-by-obviousness.

### 4. Run the ordering pass

A flat list of gaps is not yet a plan. After classification:

1. **Draw dependencies between needs, and flag cycles.** A cycle is itself a finding — two items each
   blocked by the other will never become chargeable, and no amount of prioritisation fixes it.
2. **Mark every step that needs a human hand, and name what schedules it.** A manual step with no
   scheduler is a plan gap *even when its implementation item exists*. "Someone will remember" is the
   most common silent dependency in any plan.
3. **Find the critical path** through the concrete trace, and identify its furthest-upstream item.
   That item, not the loudest one, is what controls delivery.
4. **Verify every MISSING row before filing anything.** The walk produces a reader's claims. It does
   not authorise creating work and it cannot certify that a plan is complete.

A missing owner belongs **upstream** of everything that depends on it.

## Report shape

One run emits one report, in this fixed shape:

```markdown
# Target-world walk: <shortened ticket-blind goal>

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

**An empty bin must be stated, not omitted.** "None identified" is evidence that the bin was
considered. Silence is not — and a reader cannot tell the difference between a bin that was empty and
a bin that was skipped.

## Worked example A — the epic that closes early

*This example is written backwards, as an explanation of what the walk would have exposed. A real
walk never starts from the item.*

**Ticket-blind goal:** a change can be carried from proposal through every planned slice to a
foundation that reflects all of them, with the parent work closing only after those slices are
actually delivered.

**Concrete trace:** propose the parent work; the scheduler picks up a slice; the slice is
implemented and reviewed; it opens for integration; checks run; it lands; repeat for every slice;
then close the parent and release the work waiting on it.

| Trace need | Bin | Why |
|---|---|---|
| One acceptance gate proves every planned slice landed before the parent closes | **PARTIAL** | The parent and the first slice both mention the work, but a close-directive naming the parent can fire after slice one and proves nothing about the remaining seven. |
| A durable slice inventory, and a completion join the integration step actually checks | **MISSING** | Nothing forced the integration step to compare delivered slices against the plan. |
| Dependent releases wait for the complete fan-out | **PARTIAL** | The dependent items were named, but the observed closure path could release them early. |

**Ordering:** the slice inventory and completion join come before parent close; parent close comes
before dependent release. The MISSING row is verified against the real system before anything is
filed.

## Worked example B — closure keyed to the wrong thing

**Ticket-blind goal:** a landed change closes exactly the work item whose acceptance it satisfies,
and related work stays open until its own target-world path is complete.

**Concrete trace:** propose the work; implement it; review it; open it for integration; run checks;
land it; let the integration step close the intended item; then inspect the related item and its own
completion state.

| Trace need | Bin | Why |
|---|---|---|
| Closure is keyed to the exact completed work item | **PARTIAL** | Both items had a ticket and a merged change, but an auto-close pair let closure stand in for proving the right work was delivered. |
| A neighbouring item's landing cannot declare this item complete | **MISSING** | Nothing answered: what prevents one landing from satisfying another item's delivery claim? |
| The integration step records its closure decision durably | **COVERED only if** the owning criterion asserts the durable record | The mention alone is insufficient. The criterion must require the recorded exact-item relationship. |

**Ordering:** exact-item closure comes before any downstream release or status report.

## Triggers and limits

Run the walk at three explicit moments:

1. **Before a batch of work is committed to.**
2. **Whenever a large item is fanned out into slices.**
3. **Whenever someone asks whether the plan is sufficient.**

The third is not theoretical — that question is what produced this method.

**The limits are part of the method, and stating them is not modesty:**

- **One trace covers one path only.** An incident trace and a rollback trace derive different needs;
  a different class of work exposes different ownership gaps. Running the walk once does not exhaust
  it.
- **The output is one reader's claim, not an instrument reading.** Every MISSING row is verified
  before anything is filed.
- **A completed walk cannot certify that a plan is complete.** It can only report what this trace
  required and what nobody owned.
- **It is not a per-item review gate.** This is a portfolio-level instrument; whether a single change
  is good is a different question, asked by different means.
