# agent-handbook

Material for **judging your own work** — architecture decisions, planning, the general rules that
hold whatever you happen to be building. Not a framework, not a linter, not code that runs in your
project.

It holds two kinds of thing, and the difference matters. An **instrument** is something you reach
for at a moment, and it waits until you do. A **standing rule** is something you want in force at
all times, so it is not invoked at all — it applies from the moment the plugin is enabled.

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

### The flagellant rule — a standing rule, not an instrument

Named for the sixteenth-century penitents who walked town to town whipping themselves in public.
The whipping was real and nobody was lying about the pain. It simply fixed nothing, and the crowd
went home moved rather than informed. Every failure this rule describes has that shape: a true
thing done in place of a useful one.

It carries this much standing instruction:

- **Report a mistake as two sentences** — what is wrong, and what was done about it. No third
  sentence about the person who made it. Self-blame reads as honesty and works as performance,
  because the apology buries the fact.
- **Stop when the stopping condition is met.** *Be thorough* is unfalsifiable: nobody can ever
  demonstrate having complied, so the only safe reading is *do more*, and re-reading a thing does
  not change it. The whip is in the instructions, not in the character — which is the good news,
  because instructions can be rewritten.
- **Spend care where a mistake cannot be taken back.** Caution is a budget, not a virtue. Sorting
  work by reversibility once, in writing, replaces every vague instruction to be careful.
- **A completion claim needs a test it could fail.** *Done* is a stopping condition, and an
  unfalsifiable one stops early exactly as *be thorough* runs long. Before reporting completion,
  state what would still be outstanding if you were wrong — if that sentence is easy to write,
  the report was the work.

| file | what it is |
|---|---|
| [`hooks/hooks.json`](hooks/hooks.json) + [`hooks/emit-standing-rules.js`](hooks/emit-standing-rules.js) | how it reaches you by default — a `SessionStart` hook that puts the rule into context, and re-puts it there on compact and resume |
| [`output-styles/flagellant-rule.md`](output-styles/flagellant-rule.md) | the rule's text, and an output style you may select if you want it in the system prompt instead |
| [`flagellant-rule.md`](flagellant-rule.md) | why each part is there, the exception that keeps facts from being stripped as apology, and what the rule deliberately cannot do |

**How it reaches you, and the one trade worth knowing.** By default it arrives through a
`SessionStart` hook: the plugin prints the rule and Claude Code puts that text into the session's
context. The same event is documented for a resume and a compact, so it should return in a long
session — that is the documented contract; what was measured here is the startup case. It takes nothing from you and it composes — every other
plugin's standing rules arrive alongside it.

The obvious alternative is an *output style*, which Claude Code appends to its own system prompt.
That is strictly the stronger position: a system prompt cannot be crowded out the way context can.
It is not the default for one measured reason. **Forced output styles do not compose.** Install two
plugins that both force one and only the first loaded takes effect — the second's rule is simply
absent, with no warning at install time and none at run time. Both plugins report themselves
healthy. For a handbook that promises more methods on the same terms, a mechanism whose failure
mode is the silent removal of somebody else's rule is the wrong default.

So the style ships **unforced**. Select it deliberately if you want the rule in the system prompt
and are content to give up whatever output style you had chosen; the hook already delivers the same
text, from the same file, without asking for anything.

**One precondition, and it is why the style is worth keeping rather than a duplicate.** The hook runs
`node`, so it needs `node` on your PATH. That is the case if you installed Claude Code with npm; it
is **not** guaranteed with the native installer, which bundles its own runtime. If `node` is missing
the hook simply does not deliver, and — because a standing rule must never break a session start —
it does so quietly. Confirm with `claude --debug hooks`, which prints `Hook SessionStart:startup
(SessionStart) success:` followed by the rule text when it worked. The output style needs no runtime
at all, so it is the path that still works when the hook cannot run.

Two limits belong here rather than in your surprise. Neither path reaches separately spawned
subagents — a fork of the main conversation inherits, a spawned agent does not. And a skill was
considered and rejected: only a skill's *description* is preloaded, its body arrives on invocation,
and automatic activation is a per-turn judgement rather than a guarantee. That is right for an
instrument you reach for and wrong for a rule that must not be missed.

### Why the two are shipped together

The walk exists to find what is **missing**. If finding something missing means blame, nobody walks
honestly — they bring a tidier plan instead, which is precisely the failure the walk was built to
catch, arriving through the front door. And without a method that produces a real standard, *stop
apologising* is only permission to do less. One supplies the standard; the other makes it safe to
meet.

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
/plugin install agent-handbook@windowsill
```

Then invoke the instrument by name — `/target-world-walk` — or simply describe the situation, since
its description covers the moments it is for. The flagellant rule needs no invocation; it is in force
from the moment the plugin is enabled.

Nothing to configure, no runtime, no hardware, no network.

## Why the plugin is named for the domain and not for the skill

A plugin's name and a skill's name are different things; `voice-loop` on this same shelf carries six
skills and none of them is called `voice-loop`. This one was briefly named for its single member,
which reads well until the second instrument arrives and has nowhere to go. The domain name is the
room. Each skill keeps its own name and its own invocation.

## Licence

MIT.
