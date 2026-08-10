---
name: voice-remove
description: Uninstall the voice-loop contour from this machine — stop and disable the local speech service, unbind the push-to-talk hotkey, delete the config, state and model caches the user confirms (keeping key files unless they say otherwise), take the spoken-summary convention line back out of CLAUDE.md, and print exactly what was intentionally left behind. Use when the user asks to remove, uninstall, disable, undo or clean up voice-loop, voice, dictation or speak-back.
argument-hint: "[all|service|hotkey|config|models|convention]"
allowed-tools: [Bash, Read, Edit, Glob, AskUserQuestion]
---

# voice-remove — take the contour back off

The mirror of `/voice-setup`. Setup put things in six places: a **user service** for the local
speech server, a **hotkey binding** in the desktop's own config, `~/.config/voice-loop/`,
`~/.local/state/voice-loop/`, **model caches** (some of them shared with other software), and one
**convention line** in a `CLAUDE.md`. Removing the plugin removes none of it. This skill does — in
reverse order, listing before deleting, and asking per group.

**Plus one thing setup never created:** the contour poller's schedule, which the README asks the
user to write by hand (a `systemd --user` timer or a cron line). It is the one piece that can
outlive the scripts it runs, so it is inventoried in Step 0 and stopped in Step 1b — a timer left
firing a deleted `contour-poll.sh` every five minutes is the failure this skill exists to prevent.

**Scope argument** (optional): `service`, `hotkey`, `config`, `models` or `convention` runs only
that step; `all`, or nothing, runs the whole flow. Step 0 always runs — you cannot offer to delete
what you have not looked at.

## Operating rules

1. **Inventory first, delete after.** Nothing is removed before it has been printed with its size.
   A path that Step 0 did not find is reported as "not present" and never touched again.
2. **Ask per group, and the default answer is *keep*.** One AskUserQuestion per group of paths, not
   one blanket "delete everything?". Deletion is the option the user has to choose.
3. **A key file needs its own yes.** `~/.config/voice-loop/*.key` holds a cloud credential the user
   cannot regenerate from this machine. "Delete the config directory" does **not** cover it — it
   takes a separate, explicit confirmation, and if the answer is no the directory stays with only
   the keys in it and you say so. Never print a key's contents, not even a prefix.
4. **Every `rm` names literal paths.** No `rm -rf "$VAR"` (an unset variable is `rm -rf /`), no glob
   wider than the entries Step 0 actually printed, and nothing outside those entries. Paths with
   spaces stay quoted.
5. **No root, ever.** Uninstalling needs none. The one piece setup installed as root (the `ydotoold`
   daemon, tier 3) is **printed for the user to run**, exactly as setup printed the install line.
6. **Batch.** Target **≤3 permission prompts**: one inventory batch, one removal batch, one verify
   batch. Chain with `;` (not `&&`) inside a batch so one missing path does not abort the rest.
7. **The plugin itself is not yours to remove.** `/plugin uninstall voice-loop@windowsill` does
   that, and it must come **after** this skill — uninstalling first takes this skill away with it.
   Say so at the end.

## Step 0 — inventory (one batch)

Read the config **before** anything is deleted: it names the marker Step 5 has to match and the
backends that say whether a local service should exist at all.

```sh
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop"; STATE="${XDG_STATE_HOME:-$HOME/.local/state}/voice-loop"; \
echo "== config ($CFG)"; cat "$CFG/config.json" 2>/dev/null || echo "no config"; \
echo "== files"; ls -1 "$CFG" 2>/dev/null; \
echo "== sizes"; du -sh "$CFG" "$STATE" "$HOME/.local/share/voice-loop" "$HOME/.local/share/tts" 2>/dev/null; \
du -sh "${TORCH_HOME:-$HOME/.cache/torch}"/hub/*silero* 2>/dev/null; \
du -sh "${HF_HOME:-$HOME/.cache/huggingface}"/hub/models--*faster-whisper* \
       "${HF_HOME:-$HOME/.cache/huggingface}"/hub/models--*ccentuator* 2>/dev/null; \
echo "== service"; ls -1 "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/voice-loop.service" 2>/dev/null; \
systemctl --user is-enabled voice-loop.service 2>/dev/null; systemctl --user is-active voice-loop.service 2>/dev/null; \
echo "== contour schedule"; \
ls -1 "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/voice-loop-contour.timer" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/voice-loop-contour.service" 2>/dev/null; \
systemctl --user is-enabled voice-loop-contour.timer 2>/dev/null; \
crontab -l 2>/dev/null | grep -n 'contour-poll'; \
echo "== hotkey"; gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null; \
gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-loop/ command 2>/dev/null; \
grep -nE 'dictate-toggle|voice-loop-dictate' "$HOME/.config/skhd/skhdrc" 2>/dev/null; \
echo "== stable launcher"; ls -l "$HOME/.local/bin/voice-loop-dictate" 2>/dev/null; \
echo "== convention"; grep -n 'spoken summary' "$HOME/.claude/CLAUDE.md" ./CLAUDE.md 2>/dev/null; \
echo "== pipewire aec"; ls -l ~/.config/pipewire/pipewire.conf.d/voice-loop-echo-cancel.conf 2>/dev/null; \
echo "== root-installed (left alone, reported only)"; systemctl is-active ydotoold 2>/dev/null
```

Report it as a short table — path, what it is, size, present/absent. Note the **marker** from
`speak.marker` (default `🔊`) and both `backend` values; you need them in Steps 1 and 5.

If nothing is present, say exactly that and stop: there is nothing to uninstall, and the only thing
left is `/plugin uninstall voice-loop@windowsill`. Do not invent work.

## Step 1 — the local speech service (stop it first)

Only exists if a backend was `local` on Linux. Stop before deleting, so nothing is holding a model
or a port while the caches go.

```sh
systemctl --user disable --now voice-loop.service; \
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/voice-loop.service"; \
systemctl --user daemon-reload
```

`disable --now` is stop **and** disable — a stop alone comes back on the next login. If the unit is
running but Step 0 found no unit file, it was not written by setup: report it and leave it.

On macOS setup writes no launch agent — the server, if any, was started by hand. Say so rather than
inventing a unit to remove.

### Step 1b — the contour poller's schedule (stop it BEFORE the scripts go)

Setup never created this one: the README tells the user to write it themselves, which means it can
outlive everything else here. **A timer left firing a deleted `contour-poll.sh` is this skill's
worst outcome** — a unit that fails every five minutes forever, with the failure attributed to a
plugin that is no longer installed. Remove it in the same step as the service, before anything is
deleted, and only what Step 0 actually found.

```sh
systemctl --user disable --now voice-loop-contour.timer; \
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/voice-loop-contour.timer" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/voice-loop-contour.service"; \
systemctl --user daemon-reload
```

**The cron line is the user's file, so it is theirs to edit.** Never rewrite a crontab: it holds
entries this skill knows nothing about, and `crontab -` replaces the whole thing. Print the line
Step 0 found with its number and the one command that opens the file:

```sh
crontab -e   # delete the line above
```

If Step 0 found a schedule pointing at `contour-poll.sh` in **neither** of those two shapes (a
launchd agent, a Kubernetes CronJob, someone's own supervisor), say exactly what you found and
leave it: a scheduler you did not write is not yours to disable.

## Step 2 — the hotkey binding

A binding left pointing at a deleted script is a key that silently does nothing. Remove it the same
way setup added it — **read, modify, write back**; a plain `gsettings set` of the list would clobber
the user's other custom keybindings.

### GNOME

```sh
KEY=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-loop/
CUR=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)
NEW=$(python3 -c "
import ast, sys
cur, key = sys.argv[1], sys.argv[2]
lst = [] if cur.strip() in ('@as []', '[]') else ast.literal_eval(cur)
print([p for p in lst if p != key])" "$CUR" "$KEY")
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$NEW"
gsettings reset-recursively org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY
```

The `reset-recursively` clears the orphaned name/command/binding keys the list no longer points at.

If the command read during inventory contains an absolute versioned `dictate-toggle.sh` path, this
is also an existing-install migration opportunity: before removing the binding, offer to replace its
command with `$HOME/.local/bin/voice-loop-dictate send` and leave the binding in place. The stable
launcher is the file setup installs for future updates. When the user is removing the hotkey, remove
that launcher too (only after showing its path and size); it is owned by this skill, not by the plugin
cache. If the user keeps the hotkey, keep the launcher and say so explicitly.

For a GNOME hotkey the stable launcher is a separate file, so after the binding is removed offer:

```sh
rm -f "$HOME/.local/bin/voice-loop-dictate"
```

Run it only after the user accepts that launcher deletion; `rm -f` is limited to this literal path.

### macOS (skhd)

Delete only the line that names `dictate-toggle.sh` or `voice-loop-dictate` — the file is the user's,
and other bindings live in it. Read the file, `Edit` out that one line, then `skhd --restart-service`.
If the stable launcher is no longer referenced by any retained binding, offer to remove
`$HOME/.local/bin/voice-loop-dictate` after showing its size. If the file is left empty and skhd was
installed for voice-loop alone, print — do not run — the two lines that finish the job:

```sh
skhd --stop-service && brew uninstall skhd
```

### KDE, Sway/Hyprland, anything else

Print where the binding lives (KDE: System Settings → Shortcuts → Custom Shortcuts; Sway/Hyprland:
the `bindsym`/`bind` line in the compositor config) and let the user delete it. Do not edit a
compositor config you did not write.

## Step 3 — config and state

Two questions, because they carry different risk.

**State** (`~/.local/state/voice-loop/` — `speak.log`, `dictate.log`, `spoken.ledger`,
`speaking.lock`, the recorder PID, the last WAV, the contour poller's `contour.json` and its
`contour-announced` ledger): pure runtime residue, nothing in it is
configuration. Offer deletion plainly; mention the logs are the only record of past runs.

**Config** (`~/.config/voice-loop/`): `config.json` and `stress.json` are re-creatable by re-running
`/voice-setup` — but the stress dictionary may hold hand-tuned proper names the user typed
themselves, so name it separately rather than folding it into "the config".

Then, and only then, the key files, as their own question:

> `~/.config/voice-loop/elevenlabs.key` is a cloud API key. Delete it too? (Keeping it is the
> default; it is the one thing here you cannot recreate from this machine. Deleting it does **not**
> revoke the key — do that in the provider's dashboard.)

Delete the named files, then `rmdir` the directory (not `rm -rf`) so it disappears only when it is
genuinely empty — a kept key leaves the directory standing, which is the honest outcome:

```sh
rm -f "$CFG/config.json" "$CFG/stress.json"; rmdir "$CFG" 2>/dev/null
```

### Step 3b — PipeWire echo-cancel config (Linux only, no root)

If Step 0's probe found `~/.config/pipewire/pipewire.conf.d/voice-loop-echo-cancel.conf` (or if
`speak.sink` / `dictate.source` are set in config pointing at the echo-cancel nodes), this
step removes the PipeWire module config that setup wrote.

```sh
rm -f ~/.config/pipewire/pipewire.conf.d/voice-loop-echo-cancel.conf
# The directory is shared with other PipeWire config — only rmdir if empty:
rmdir ~/.config/pipewire/pipewire.conf.d 2>/dev/null || true
```

Tell the user the module is still loaded in the running PipeWire session — it stays active until the
next restart (`systemctl --user restart pipewire`), and no running dictation is affected. The config
is gone; the next PipeWire restart is a clean default-mic setup.

## Step 4 — model caches (list sizes, ask per cache)

This is where the gigabytes are, and where "delete it all" would be wrong: two of these directories
belong to shared tools, not to voice-loop. Present the Step 0 sizes and ask per line.

| cache | who owns it | safe to delete |
|---|---|---|
| `~/.local/share/voice-loop/` | **voice-loop only** — the venv, the copied server, whisper.cpp `.bin` files, voice previews | yes; setup recreates it |
| `~/.cache/torch/hub/*silero*` (or `$TORCH_HOME`) | torch.hub, **shared** — but the silero entries are ours | yes, entry by entry — never the whole `hub/` |
| `~/.cache/huggingface/hub/models--*faster-whisper*`, `models--*ccentuator*` (or `$HF_HOME`) | HuggingFace, **shared with every HF tool on this machine** | yes, those entries only — never the whole `hub/` |
| `~/.local/share/tts/` | coqui's own cache (XTTS-v2 weights) | only if the user does not use coqui-tts for anything else |

Say the cost before deleting: re-installing later re-downloads roughly **0.5–1.5 GB** (more with
XTTS). A user who might come back is better off keeping the caches — that is a legitimate answer,
not a failure to finish.

Delete only the literal paths Step 0 printed, one `rm -rf` per entry, each path quoted.

## Step 5 — the CLAUDE.md convention line

Setup appended, verbatim as its own paragraph:

> End each reply with a one-sentence spoken summary on its own line, starting with 🔊.

Use the **same dup-safe matching setup used**, and against the marker from Step 0's config (if the
user changed `speak.marker`, the line in their file carries *their* marker, not `🔊`): search both
`~/.claude/CLAUDE.md` and the current project's `./CLAUDE.md` loosely — a line containing the marker
**and** "spoken summary". Setup could only ever have written one such paragraph per file, but a user
may have written their own variant; matching loosely finds it, and showing it before touching it is
what keeps that safe.

**Show the exact line(s) found, with their file, and ask before editing.** Then remove the line and
the blank line it brought with it, leaving everything else byte-identical — use `Read` + `Edit` on
the exact string, never `sed -i` over a file the user maintains by hand. If the file is now empty,
leave the empty file: it may be tracked by the user's own repository.

If no such line is found, say so — setup may have been declined at that step, or the user may keep
the convention somewhere else (a `CLAUDE.local.md`, an `AGENTS.md`). Point at that possibility
instead of grepping the whole home directory.

## Step 6 — verify, then print what was left

One verify batch, re-running the Step 0 shape so the report is measured rather than assumed:

```sh
systemctl --user is-active voice-loop.service 2>/dev/null; \
systemctl --user is-enabled voice-loop-contour.timer 2>/dev/null; \
crontab -l 2>/dev/null | grep -c 'contour-poll'; \
ls -d "${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop" "${XDG_STATE_HOME:-$HOME/.local/state}/voice-loop" \
      "$HOME/.local/share/voice-loop" 2>/dev/null; \
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null; \
grep -c 'spoken summary' "$HOME/.claude/CLAUDE.md" 2>/dev/null
```

Then print **what is intentionally still on the machine** — this list is the deliverable, not a
footnote. Cover every item that applies:

- **The plugin.** `/plugin uninstall voice-loop@windowsill` — run it last, after this skill.
- **Anything the user declined**: key files, shared caches, the stress dictionary. Name the paths.
- **Packages, because they are shared.** `jq`, `curl`, `sox`, `wl-clipboard`, `alsa-utils`,
  `ffmpeg`, `whisper-cpp`, `skhd` — installed by setup, used by other things. Print the removal
  line for the user's decision (`brew uninstall …`, `sudo apt remove …`); never run it.
- **The `ydotoold` daemon**, if tier 3 was taken — root-installed, so root removes it. Print:
  ```sh
  sudo systemctl disable --now ydotoold && sudo rm /etc/systemd/system/ydotoold.service
  ```
- **macOS Accessibility consents** for the terminal app and/or `skhd` — System Settings → Privacy &
  Security → Accessibility, revoked by the user by hand.
- **Nothing off this machine.** Voices designed by `/voice-design` live in the user's ElevenLabs
  account, and a deleted key file is still a live key: both are the provider's dashboard, not ours.

Close with a two-or-three line summary: what was stopped, what was deleted, what was kept and why,
and the one command left to run. If any step failed, say which and what to run by hand — a partial
uninstall reported honestly is a result; a partial uninstall reported as done is a bug in this skill.
