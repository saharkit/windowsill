---
name: voice-design
description: Cast a custom synthetic voice for voice-loop using ElevenLabs text-to-voice — turn the user's own description of a timbre into an English voice prompt, generate and present preview samples, iterate on feedback, then save the chosen voice_id into ~/.config/voice-loop/config.json. Use when the user wants to design, choose, audition or change the voice that speaks their Claude Code replies.
argument-hint: "[a few words about the voice you want]"
allowed-tools: [Read, Write, Edit, AskUserQuestion, mcp__plugin_voice-loop_voice-loop__design_previews, mcp__plugin_voice-loop_voice-loop__design_save]
---

# voice-design — cast the voice that will speak

A generic TTS speaker is a placeholder. This flow designs a voice **from a description** and writes
the result into the user's config, so the Stop hook starts speaking in it.

## Hard ethics line — read before anything else

**Never design a voice to imitate a real, identifiable person.** Not a celebrity, not a colleague, not
the user's friend, not a character voiced by a known actor. If the user asks for "a voice like <named
person>", decline that framing and offer the generalized version instead: *"warm low female voice,
mid-30s, calm and unhurried"* — describe the **timbre**, never the identity. This is both the
ElevenLabs terms of service and the right thing; state it once, plainly, without a lecture, and move
on to the description that does work.

Also state the privacy tradeoff once, before the first request: **voice design is a cloud call** — the
description and the sample text you generate are sent to ElevenLabs. Nothing from the user's sessions
goes there unless they choose that text. Recognition and everyday synthesis can still run locally; the
cloud is used here for **design** (and afterwards only if they set `tts.backend: cloud`).

## Step 0 — key and prerequisites

The ElevenLabs key lives in the **`tts_api_key` userConfig option** — open `/config`, find
*voice-loop*, set *Cloud TTS API key*, and Claude Code delivers it to the voice-loop MCP server
through its env. The skill calls the two MCP tools below; the server reads the key, never the skill,
never argv, and never on disk. If `/voice-design` reports that the voice-loop MCP tools are
unavailable (stock Windows has no `python3` on PATH, so the MCP server does not start), the user
must run voice-design from a session where the MCP server is live, or use the voice-loop
`/voice-setup` flow to install Python and `python3` first.

You also need the language (from `language` in the config) and a way to play audio
(`speak.player` — `afplay` on macOS, `mpg123 -q` or `ffplay -autoexit -nodisp -loglevel quiet` on
Linux, since previews come back as mp3).

## Step 1 — get the description in the user's own words

Ask for the voice they want, in their language, however they want to say it: warm/cold, high/low, age,
pace, breathiness, accent shade, "like a late-night radio host", "calm and unhurried". Then ask
about the specifics that measurably change the result if they have not covered them:

- **accent shade** (native / slight foreign accent / neutral broadcast),
- **breathiness** (airy vs. clean),
- **pace** (unhurried vs. brisk),
- **register** (low chest voice vs. bright).

**Translate their description into an English `voice_description`** — the model is prompted in English
and the quality difference is large. Show the user the English prompt you produced (one line) so they
can see what is actually being asked for. Aim for 20–100 words: timbre, age range, pace, emotional
color, recording character ("clean studio recording, no reverb").

## Step 2 — generate previews

Author a sample text **in the user's language**, roughly 300–500 characters (the API wants at least
~100 and this length is what makes a timbre judgeable). Make it neutral and relevant — a few sentences
of the kind of thing the assistant actually says, not a poem.

Call the voice-loop MCP tool `mcp__plugin_voice-loop_voice-loop__design_previews(voice_description, text)`.
The MCP server posts to `https://api.elevenlabs.io/v1/text-to-voice/create-previews`, writes each
preview under `~/.local/share/voice-loop/previews/preview-N.mp3`, and returns
`[{generated_voice_id, path}, ...]`. Present them to the user: list the file paths, play them in order
if a player is available, and say which numbered preview maps to which id. Never leave the user to
guess which file was which.

*If the tool reports an `elevenlabs http` error or the endpoint returns 404:* ElevenLabs has been
renaming this API surface (`/v1/text-to-voice/design` + `/v1/text-to-voice` in newer revisions). Say
so, check their current docs, and keep the flow identical — the shape (describe → previews → pick →
create) is what matters.

## Step 3 — iterate

Take the user's reaction and turn it into **2–3 varied descriptions per round**, not one — auditioning
is faster in batches and each round costs a call anyway. Typical axes to vary:

- "lighter accent" / "fully native"
- "breathier, more intimate" / "cleaner, more neutral"
- "slower, more deliberate" / "brisker"
- age up or down five years
- "warmer, lower" / "brighter"

Change **one or two axes at a time** and tell the user what changed between candidates. Keep the same
sample text across rounds so comparison is honest. Stop when they say "this one".

## Step 4 — mint the voice and wire it in

Call the voice-loop MCP tool
`mcp__plugin_voice-loop_voice-loop__design_save(generated_voice_id, voice_name, voice_description)`.
The MCP server posts to `https://api.elevenlabs.io/v1/text-to-voice/create-voice-from-preview` and
returns `{voice_id}`. Take the printed `voice_id` and write it into the config:

```jsonc
"tts": {
  "backend": "cloud",
  "cloud": {
    "provider": "elevenlabs",
    "voice_id": "<voice_id>",
    "model": "eleven_multilingual_v2",
    "output_format": "mp3_44100_128",
    "voice_settings": { "stability": 0.7, "similarity_boost": 0.8, "style": 0.1, "use_speaker_boost": true }
  }
}
```

Edit the existing config rather than rewriting it — the user's language, dictation and hotkey settings
must survive. Remind them that `speak.player` now has to handle mp3 (`afplay` does; on Linux use
`mpg123 -q` or `ffplay`), and that with `backend: cloud` every spoken line becomes a billed API call —
they can keep `local`/`lan` for daily use and switch when they want the designed voice.

## Anti-robovoice — the settings that actually matter (field-tested)

A designed voice can sound wonderful in the preview and metallic in daily use. The cause is almost
never "the model is bad"; it is settings, text length, or — for Russian and Ukrainian — **stress**.

### Cloud (ElevenLabs `voice_settings`)

Counter-intuitively, **breathy/airy designed voices produce metallic artifacts at LOW stability** —
the airiness is exactly what the sampler destabilizes. The working recipe:

```jsonc
// fragment of ~/.config/voice-loop/config.json, under tts.cloud
"voice_settings": {
  "stability": 0.7,
  "similarity_boost": 0.8,
  "style": 0.1,
  "use_speaker_boost": true
}
```

- `stability` **0.6–0.75** — this is the first knob to RAISE when robotic artifacts appear;
- `similarity_boost` **0.75–0.85**;
- `style` **≤ 0.15** — the second knob to lower; high style is the other common artifact source;
- `use_speaker_boost` **true**.

Put this under `tts.cloud.voice_settings` in the config — `speak.sh` passes it through verbatim.

**Long texts drift.** Keep each request moderate and chunk on sentence boundaries; a single long block
wanders in tone and picks up artifacts near the end. (The local server already chunks; for cloud,
prefer short spoken summaries — which is what the marker convention gives you anyway.)

**If artifacts persist across settings, regenerate — do not keep fighting the knobs.** Previews vary:
generate a fresh batch from the same (or a slightly varied) description and pick a different candidate.
Different previews from the same description beat an hour of parameter tuning.

### Local (Silero, ru/uk)

For Russian and Ukrainian, most of what people call "robotic" is **wrong word stress**, not synthesis
quality. The levers, in order:

1. the automatic accentuation pipeline — RUAccent (ru) / ukrainian-word-stress (uk) — must actually be
   installed and enabled (`/health` reports `accentuated_languages`);
2. `~/.config/voice-loop/stress.json` for proper names and homographs the dictionaries get wrong —
   your rules are applied before the accentuator and are never overridden by it;
3. writing a name with a **combining acute** in the text itself (`Ка́тя`) — the server converts it to
   Silero's `+` notation for you.

Fix the stress before concluding the voice is bad. It usually is the whole problem.

## Step 5 — a real test render

Offer a full-length render of something longer than the preview (a paragraph they pick), play it, and
ask the honest question: *"is this the voice you want to hear fifty times a day?"* A voice that is
charming for ten seconds can be tiring at length — better to find out now than after it is wired in.

Finish by ending your next reply with a marker line so the Stop hook speaks in the new voice, and
confirm with the user that it sounded right.

## Design in the cloud, then drop the key — hand off to the XTTS engine

The local clone path **exists**: the bundled server ships an XTTS-v2 engine
(`VOICE_LOOP_TTS_ENGINE=xtts`) that clones from a short reference wav — setup, the licensing
caveats (the XTTS-v2 weights are non-commercial), and the reference contract are in
the plugin's own `server/README.md` → *XTTS engine (voice cloning)*
(<https://github.com/saharkit/windowsill/blob/main/plugins/voice-loop/server/README.md#xtts-engine-voice-cloning>).

This skill designs **cloud** voices; it does not set up XTTS. If the user wants the full arc —
design the voice here, mint a reference recording from the chosen voice, run XTTS-v2 on their own
GPU with that reference, then point `tts` at it (`backend: lan`) and **clear the `tts_api_key`
plugin option** —
finish the cloud design, then hand them to that server doc for the local half. The ethics rule
travels with it unchanged: a reference must be **a voice they minted themselves or have explicit
rights to**, never a third party's.