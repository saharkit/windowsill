# engineering-method

Instruments for **judging your own work** — architecture decisions, planning, the general rules that
hold whatever you happen to be building. Not a framework, not a linter, not code that runs in your
project: each one is a skill you invoke when you want a second way of looking at something you have
already decided.

Nothing here is tied to a language, a stack or a build system. Nothing installs a runtime, opens a
network connection or needs hardware.

## What ships here

### `/target-world-walk` — a portfolio-completeness instrument

It answers one question:

> What must exist for one real unit of work to cross the system you intend to have, and who owns
> each part?

The ordinary way to check a plan is a **backward carrier audit**: walk the items you wrote down and
confirm each has a ticket and an owner. That is worth doing, and it is **blind by construction** to
the thing that actually sinks plans. Its input is the plan, so anything the plan never named is
invisible to it. A board can be fully carried and still be missing the step that makes the work
reach anybody.

The walk runs the other direction. You start from a goal stated without reference to any tracker
item, pick one concrete unit of work, and replay it through the target system. At every transition
you ask what must exist and who owns it. What you find that nobody named is the answer.

| file | what it is |
|---|---|
| [`skills/target-world-walk/SKILL.md`](skills/target-world-walk/SKILL.md) | the executable form — the refusal gate, the walk itself, the four classifications, the ordering pass, and what the walk deliberately cannot certify |
| [`target-world-walk.md`](target-world-walk.md) | the method and its limits, for a reader who wants the reasoning rather than the procedure |

Each file is written to stand alone. Neither links to the other or to anything outside itself, which
is deliberate: the method travels, and a reader who receives one of them has everything that file
claims to give. This README is the only place the two are named together.

**Reach for it** before committing to a batch of work; when one large item is fanned out into slices
and you want to know whether the slices cover the path; or whenever someone asks whether the plan is
sufficient — which is a question the plan itself cannot answer.

**It refuses a goal that names a tracker item.** That refusal is the method, not a nicety: a goal
phrased as "finish the work under item 123" imports the plan's own blind spot as its starting
premise, and the walk would then re-derive exactly what was already written. If the goal cannot be
stated without pointing at the board, that is itself the finding.

**A completed walk is not a certificate.** One trace covers one path, and the report is a reader's
claim rather than an instrument reading. It can only name what this one path required and nobody
owned. The skill states these limits as part of the method rather than as a disclaimer, because a
walk mistaken for a certificate is worse than no walk.

## Installing

```
/plugin marketplace add saharkit/windowsill
/plugin install engineering-method@windowsill
```

Then invoke a skill by name — `/target-world-walk` — or simply describe the situation, since each
skill's description covers the moments it is for.

Nothing to configure, no runtime, no hardware, no network.

## Why the plugin is named for the domain and not for the skill

A plugin's name and a skill's name are different things; `voice-loop` on this same shelf carries six
skills and none of them is called `voice-loop`. This one was briefly named for its single member,
which reads well until the second instrument arrives and has nowhere to go. The domain name is the
room. Each skill keeps its own name and its own invocation.

## Licence

MIT.
