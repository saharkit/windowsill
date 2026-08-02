# FAQ

**What does it cost to run locally?**
Whisper `small` needs roughly 2 GB of RAM and transcribes a short phrase in a couple of seconds on a
modern CPU; `base`/`tiny` are faster and less accurate; `large-v3-turbo` wants a GPU. Silero
synthesis is CPU-friendly and near real time. First run downloads ~0.5–1.5 GB of models. Idle cost is
just the resident process; keep it as a user service and forget it.

**Does my audio leave the machine?**
Depends on the backend, per direction:

- `local` — no. Nothing leaves.
- `lan` — it goes to the host you chose, over your network or an ssh tunnel. Nowhere else.
- `cloud` — yes: your microphone audio (STT) and the spoken text (TTS) go to that provider. It is off
  by default, and you can mix — e.g. local recognition with cloud synthesis, so your voice never
  leaves but the assistant's replies do.

**What is sent to the provider in cloud mode, exactly?**
For STT, the recorded WAV. For TTS, the text of the lines marked with the speak marker — not your
whole session, not your code. Keys are read from a file or a named environment variable, never stored
in `config.json`.

**Which languages work?**
Recognition is multilingual (whisper). Local synthesis ships English (`v3_en`), Russian (`v4_ru`),
Ukrainian (`v4_ua`), German (`v3_de`), Spanish (`v3_es`), French (`v3_fr`). Russian and Ukrainian
additionally get automatic stress marking, which is what keeps them from sounding drunk. Any other
language: recognition still works, and for synthesis use a cloud voice or the macOS built-in `say`.

**Can I have my own voice?**
`/voice-design` casts one: you describe the timbre you want, it auditions candidates, and the one you
pick is written into your config. It will **not** imitate a real, identifiable person — describe a
voice, not a human. Running that designed voice locally is live too: mint a reference recording,
serve it with the server's XTTS-v2 engine (`VOICE_LOOP_TTS_ENGINE=xtts`) on your own GPU, and drop
the cloud key — see [server/README.md — XTTS engine](../server/README.md#xtts-engine-voice-cloning).

**Why only lines with a marker?**
Because hearing an entire answer read aloud is exhausting and slow. The model marks the one line
worth hearing; you read the rest. The marker is configurable (`speak.marker`) and the convention is
whatever you write in your `CLAUDE.md`.

**Does it need root?**
No. The default path — clipboard plus a notification — needs no root and no consent dialogs anywhere.
The only root step in the whole system is the optional `ydotool` daemon needed for auto-paste on
GNOME/Wayland specifically, and `/voice-setup` prints that command for you to run rather than running
it. macOS needs root nowhere (one Accessibility consent if you want auto-paste).

**Does it work in every permission mode?**
Hooks execute without permission prompts, so speak-back works unchanged under any mode. `/voice-setup`
is written for the default mode: it announces its plan and batches its work into a couple of coarse
actions instead of twenty prompts.

**Will it slow down my session?**
The Stop hook is registered `async`, so the turn is not blocked by synthesis. Dictation runs in its
own process off a hotkey and never touches the session at all.

**Can I use it without Claude Code?**
The dictation script is a standalone toggle — bind it to a hotkey and it fills your clipboard in any
app. The speak hook is the only Claude-Code-specific part.
