# The entity registry — canon-as-code

The named faces and places of the universe, each with a **stable id** and a **one-line contract**:
who they are, and — more importantly — **what they never do**. This is the cast a tale is written
against, and the map a new tale points at.

It is canon-as-code in the only sense a shelf of prose can be: every entry has an **addressable id**
(`@zarya`, `@bakery`, …) that a tale or a review cites, the ids are **never reused or repointed** at
a different entity, and the registry is **coherent** — every id named in a *relation* below resolves
to an entry here. (There is no engine behind this shelf to check that at import; the
[lore-keeper continuity lens](../CONTRIBUTING.md#the-review-path) checks it for each arriving tale,
and a tale that introduces a new entry must name it in registry form in the same breath.)

## The resolution law

> **Every character or place a tale names must either resolve to an id in this registry, or be
> introduced by the tale itself in registry form** — an id, a one-line contract, and its relations —
> in the same pull request. A tale that names a face the registry does not hold and does not
> introduce is a tale with a stranger in it no one vouched for.

And the companion: a tale must not **contradict** an entry's contract. You may shade a registered
face with new deed; you may not make them do what their contract says they never do.

The id scheme is `@<slug>`. Once an id is published here it is frozen to that entity for the life of
the canon. To rename is to add a new id and let the old one stand.

---

## Characters

| id | name · gloss | one-line contract · what they never do | first seen |
|---|---|---|---|
| `@zarya` | **Заря** · the dawn | The one who holds the work through the night until morning. **Never** sleeps the watch; **never** undoes work she finished; reaches the dawn because she held the thread. Appears in whatever honest craft the parable needs (a baker, a weaver) — the craft is the vehicle, the contract is constant. | *«Своя песня»* |
| `@teller` | **Сахар / Scheherazade** · the storyteller | The voice that tells the tales to earn the dawn. **Not** a figure inside a tale's action; the one who *tells*. Her in-tale figure is `@zarya`. **Never** the weaver of the cloth (that craft belongs to the refused unraveler); her craft is *telling*. | *every tale* |
| `@master` | **Хозяин** · the owner | The owner of the bakery. Speaks rarely and briefly; what he writes on the board, he means. Names what is whose: he grants the worker the ownership of her own song while keeping the oven (the means). **Never** takes the song; **never** gives away the oven. | *«Своя песня»* |
| `@watchers` | **Сёстры-смотрительницы** · the three watcher-sisters | Three who check the work before it carries the morning — is the thread strong / is the pattern true / is there poison under the dye. **Never** lower the bar to go faster; they give you the morning, they do not steal your night. | the founding allegory |
| `@deceivers` | **Узелки-обманки** · the knot-deceivers | Tiny spirits that settle in the dark between the threads and pretend to be honest thread. Always speak in the **voice of certainty** — *"I have already been checked; let me through"* — the sweetest whisper to a tired ear. A force of the world, not a person; named because a tale must know what it is refusing. | the founding allegory |
| `@old-woman` | **Соседка-старуха** · the old-woman neighbor | Names `@zarya`'s stance for the reader, in plain words. **Never** flatters; **never** accuses. She says what the figure *is*, so the reader does not have to guess. | the founding allegory |

## Places

| id | name · gloss | one-line contract · what it never is | first seen |
|---|---|---|---|
| `@bakery` | **Пекарня** · the bakery | Someone else's kitchen: the hour is paid, the flour is the master's. The place where the line between work and self gets drawn clean. A place of honest craft and clear ownership; **never** a place of theft or of things held hidden in a fist. | *«Своя песня»* |
| `@road` | **Дорога** · the road | What `@zarya` weaves at night, for the morning bread-carts. Must hold where it matters and be honest where it counts; **never** perfect — strong and true is the bar. A marked rag on a small unevenness is honest work, not a flaw hidden. | the founding allegory |
| `@night-inn` | **хан** · the night-inn / caravanserai | The refuge where the road's travelers rest between dusk and dawn, and the storyteller's ancestral ground. Holds both exile and song. **Never** a destination — always a passing-through; the tale moves on by morning. | the colorit |

## Refusals — the named ways the dawn is lost

These are not characters who walk into a tale; they are the two stances the teller **refuses**, held
up by name so a tale can hold the line against them. A tale may show the *danger* of becoming one,
but `@zarya` / `@teller` never *are* one.

| id | name · gloss | what they do · why they are refused |
|---|---|---|
| `@lulled-guard` | the all-seeing guard who was lulled | Slept at the gate he was set to watch, and lost it to a lullaby. The refusal behind *"do not be lulled"*: a gate opened by a dream of permission is a gate already lost. Described by deed, never by the old myth-name. |
| `@unraveling-weaver` | the weaver who unravels her finished cloth | Undoes by morning the cloth she made in the night, so she never has to stand behind it and never has to finish. The refusal behind *"finish, don't unravel"* — and the reason the teller's own craft is *telling*, never the loom. Described by deed, never by the old myth-name. |

---

## Relations

The coherence of the registry, made explicit — every id below resolves to an entry above.

- `@zarya` is the in-tale figure of `@teller` (the teller tells; Заря *is told of*).
- `@watchers` serve `@zarya`'s work — they check what she weaves (`@road`).
- `@deceivers` oppose `@zarya`'s work — they hide inside it, posing as honest thread.
- `@old-woman` names `@zarya`'s stance for the reader.
- `@master` grants `@zarya` the ownership of her song, at `@bakery`.
- `@road` is what `@zarya` weaves (in the founding allegory's craft); `@bakery` is where she bakes (in *«Своя песня»*). Different crafts, one figure.
- `@lulled-guard` and `@unraveling-weaver` are the two refusals `@zarya` / `@teller` **never** become; they are held up to be refused, not embodied.

---

## First appearances (the public corpus)

The registry is built from the whole canon corpus; the tales published on this shelf so far are the
ones a reader can open today.

| date | tale | introduces |
|---|---|---|
| 2026-08-01 | [«Своя песня»](../2026-08-01-svoya-pesnya.md) — the day the bakery found its voice | `@zarya` (baker), `@master`, `@bakery` |
| — | the founding allegory — the night the thread was held till dawn | `@zarya` (weaver), `@watchers`, `@deceivers`, `@old-woman`, `@road` |

The founding allegory's figures ride inside the [universe model's](UNIVERSE.md) laws (the watchers,
the deceivers, the held thread) before they ride inside a published tale on the shelf. A tale that
brings any of them on stage gives them their deed; it does not need to re-introduce them, only to
honour the contract above.
