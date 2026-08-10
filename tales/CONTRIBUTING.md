# Contributing a tale

A tale arrives on this windowsill as a **pull request into `tales/`**. This page is the whole path:
the rules a tale is held to, the review it passes, and the gate that finally accepts it.

The tales are written in **Russian**, in a small fixed vocabulary, and they never explain the
machinery they came from. Before you write one, read the [universe model](canon/UNIVERSE.md) (the
laws) and the [entity registry](canon/REGISTRY.md) (the cast) — a tale is told *inside* that world,
and it must not break it. An existing tale is the best map: [«Своя песня»](2026-08-01-svoya-pesnya.md)
is a short one you can read in a minute.

## The register rules

A tale earns its place by holding to these:

- **Common, everyday Russian** — roughly CEFR **B2 and below**. A tale the reader understands line
  by line, with no word that needs a dictionary. Rarity and archaism read as showing off, not as
  depth.
- **Warm humor, not cleverness.** A small true tale beats a grand hollow one. Cruelty and
  cleverness-for-its-own-sake do not belong here.
- **Rod, not fish.** A tale hands the reader a fishing rod, not a fish: it **never exposes the
  machinery it came from**. The wisdom travels; the mechanism does not. If a line only makes sense
  once you know the real-world thing behind it, it is a fish, and it fails the rule.
- **Every image legible cold.** No metaphor that needs its own decoding. Hold each image by
  *conviction* (you can say why it is true), not *convenience* (it was the first thing you found).
- **No gods, no mythology name-dropping.** Ground the tale in craft, people, land, folk.
- **A number must be earned in the tale.** If a tale is "the fourth night," the body shows nights
  one through three first.
- **The dawn-figure's contract is constant.** She may be given any honest craft the parable needs,
  but she holds through the night, does not sleep the watch, does not unravel what she finished, and
  reaches the dawn.

These are the [universe model's](canon/UNIVERSE.md) laws applied to the act of writing; read that
page for the why behind each one.

## Resolution and non-contradiction

Two checks a tale must pass before it can be accepted:

1. **Resolution.** Every character or place the tale names must either **resolve to an id in the
   [registry](canon/REGISTRY.md)**, or be **introduced by the tale in registry form** — a stable
   `@id`, a one-line contract, and its relations — in the same pull request. A tale with a stranger
   in it that no one vouched for does not pass.
2. **Non-contradiction.** The tale must not contradict canon: not the laws, and not a registered
   contract. You may shade a registered face with a new deed; you may not make them do what their
   contract says they never do. You may add to the world; you may not revise a tale that already
   stands (a told tale is never rewritten — see the chronicle law).

## The review path

A tale is read by the **lore-keeper continuity lens** — a careful pass over the *whole* canon, not
just the diff, that asks two things:

- does every name resolve, and is every new entry self-consistent with what already stands?
- does the tale contradict a law or a contract?

The lore-keeper lens **advises**. It catches resolution gaps and contradictions and names them, so
the teller can hold the thread. It does not accept the tale.

## The final gate

> **The teller-editor's cold read and signature is the acceptance.** A tale is accepted when it has
> been read cold, against the whole canon, and signed.

Canon authorship is **curated, not merged by CI.** The continuity lens advises; the teller-editor
signs. No automated check can accept a tale, and none is meant to — the gate is a person who has read
the canon and stands behind the tale, exactly the way every tale already on the shelf was accepted.
A green build does not merge a tale; a signature does.

## Format

A tale lands as one file (plus optional companions) under `tales/`:

- `tales/YYYY-MM-DD-slug.md` — the tale, in Russian, dated for the day it is told. The date in the
  filename is when the tale is told, and it does not move.
- `tales/YYYY-MM-DD-slug.voice.mp3` — an optional voiced edition.
- `tales/YYYY-MM-DD-slug.art.png` — an optional picture.

A tale that introduces a new entity carries its registry additions in the pull request (an entry in
`canon/REGISTRY.md`, or the registry-form block in the tale), so the resolution law holds the moment
the tale lands. Sign the tale as told — the existing ones are signed **Sahar**.

## What a tale earns

A tale accepted from outside the school is the `shelf-outside-tale` rung in
[ACHIEVEMENTS.md](../ACHIEVEMENTS.md) — its proof is the merged tale's pull request together with
the teller-editor's signature on it. That row lands *with* the accepted tale, never before.
