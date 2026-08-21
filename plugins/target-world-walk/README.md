# target-world-walk

A **portfolio-completeness instrument**, shipped as a single skill. It is not a review of a change
and not an audit of your tickets. It answers one question:

> What must exist for one real unit of work to cross the system you intend to have, and who owns
> each part?

## The problem it addresses

The ordinary way to check a plan is a **backward carrier audit**: walk the items you wrote down and
confirm each has a ticket and an owner. That is worth doing, and it is **blind by construction** to
the thing that actually sinks plans. Its input is the plan, so anything the plan never named is
invisible to it. A board can be fully carried and still be missing the step that makes the work
reach anybody.

The walk runs the other direction. You start from a goal stated without reference to any tracker
item, pick one concrete unit of work, and replay it through the target system. At every transition
you ask what must exist and who owns it. What you find that nobody named is the answer.

## What ships here

| file | what it is |
|---|---|
| [`skills/target-world-walk/SKILL.md`](skills/target-world-walk/SKILL.md) | the executable form — the refusal gate, the walk itself, the four classifications, the ordering pass, and what the walk deliberately cannot certify |
| [`target-world-walk.md`](target-world-walk.md) | the method and its limits, for a reader who wants the reasoning rather than the procedure |

Each file is written to stand alone. Neither links to the other or to anything outside itself, which
is deliberate: the method travels, and a reader who receives one of them has everything that file
claims to give. This README is the only place the two are named together.

## Installing

```
/plugin marketplace add saharkit/windowsill
/plugin install target-world-walk@windowsill
```

Then invoke it as `/target-world-walk`, or simply describe the situation — the skill's description
covers the three moments it is for.

Nothing to configure, nothing to install beyond the plugin, no runtime, no hardware, no network.

## When to reach for it

- Before committing to a batch of work.
- When one large item is fanned out into slices, and you want to know whether the slices actually
  cover the path.
- Whenever someone asks whether the plan is sufficient — which is a question the plan itself cannot
  answer.

## What it refuses

The walk **refuses a goal that names a tracker item.** That refusal is the method, not a nicety: a
goal phrased as "finish the work under item 123" imports the plan's own blind spot as its starting
premise, and the walk would then re-derive exactly what was already written. If the goal cannot be
stated without pointing at the board, that is itself the finding.

## What a completed walk does not give you

One trace covers one path. The report is a reader's claim, not an instrument reading. **A completed
walk cannot certify that a plan is complete** — it can only name what this one path required and
nobody owned. The skill states these limits as part of the method rather than as a disclaimer,
because a walk mistaken for a certificate is worse than no walk.

## Licence

MIT.
