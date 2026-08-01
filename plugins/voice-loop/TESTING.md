# voice-loop — human acceptance checklist

CI proves the parts a machine can reach: the scripts parse, the manifests are valid, and the loopback
(TTS → STT → compare) plus the Stop-hook contract run green on Linux and macOS. **Everything below is
what CI structurally cannot check** — a real microphone, a real hotkey, real ears, and a real user
sitting in default permission mode.

Run this before every release, on a machine that has never had voice-loop on it (a clean container or a
fresh VM for Linux; a teammate's untouched Mac for the macOS branch). "Works on the machine it was
built on" is not a result.

**How to use it:** one row per check. Fill in *observed* and *pass/fail*, and sign off at the bottom.
A failed row is a release blocker unless it is explicitly waived in writing with a reason.

Environment under test:

| field | value |
|---|---|
| date | |
| tester | |
| OS / version | |
| desktop / session (GNOME-Wayland, KDE, X11, macOS) | |
| plugin version | |
| backends chosen (stt / tts) | |
| language | |

## 1. Install from the marketplace, clean machine

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 1.1 | `claude marketplace add saharkit/windowsill` | marketplace added, no errors | | |
| 1.2 | `/plugin install voice-loop@windowsill` | plugin installs; it appears in `/plugin` | | |
| 1.3 | Start a fresh session | no hook errors in the session; the Stop hook is registered | | |
| 1.4 | `/voice-setup` is offered / discoverable by name | the skill is listed | | |

## 2. `/voice-setup` under DEFAULT permission mode

Run Claude Code with its normal (default) permission mode — **not** bypass. Count every permission
prompt the setup causes.

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 2.1 | The agent states its plan before acting | a short plan, then work | | |
| 2.2 | **Permission prompt count for the whole install** | **≤ 3** (more = FAIL, this is the #1656 acceptance bar) | count: | |
| 2.3 | Language question comes first and is pre-answered from the environment | one confirm for the common case | | |
| 2.4 | Backend choice is offered per direction with the cost/privacy tradeoff stated | user makes an informed choice | | |
| 2.5 | No `sudo` is ever executed silently | any root step is PRINTED for the user to run | | |
| 2.6 | Default paste tier is clipboard (no root, no consent dialog) | `auto_paste: false` unless the user opted in | | |
| 2.7 | `~/.config/voice-loop/config.json` is written and valid | `jq . ~/.config/voice-loop/config.json` parses | | |
| 2.8 | No secret is written into the config | keys are in a `key_file` or an env var only | | |
| 2.9 | Setup ends by running the selftest | green selftest, reported plainly | | |

## 3. The loop itself

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 3.1 | `bash plugins/voice-loop/scripts/selftest.sh` | exits 0, prints said/heard/similarity | | |
| 3.2 | **Dictation round-trip**: press hotkey, say a sentence, press again | the transcript appears in the prompt (or on the clipboard, tier 1) within a few seconds | | |
| 3.3 | Dictation in `send` mode with auto-paste enabled | text is pasted AND Enter is pressed once — exactly once | | |
| 3.4 | Long dictation (~30 s of speech) | no truncation of the tail; the last words are present | | |
| 3.5 | **Speak-back**: assistant replies with a 🔊 line | it is audibly spoken, once, and matches the text | | |
| 3.6 | Unmarked lines | are NOT spoken | | |
| 3.7 | Two turns in a row | the second turn speaks the new line, not a repeat of the first (dedup) | | |
| 3.8 | A fast turn right after another | no overlapping playback — the fresher line wins | | |
| 3.9 | Stress/pronunciation of your own proper names (ru/uk) | acceptable after adding them to `stress.json` | | |

## 4. Negative cases — failures must be legible, never hangs

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 4.1 | Speech server stopped, then run `selftest.sh` | clear "server not reachable" message, non-zero exit, **within the timeout, no hang** | | |
| 4.2 | Speech server stopped, then a 🔊 reply | the turn completes normally; nothing hangs; the reason is in `~/.local/state/voice-loop/speak.log` | | |
| 4.3 | Speech server stopped, then press the dictation hotkey | notification says nothing was recognized; no stuck recording; a second press behaves sanely | | |
| 4.4 | Wrong/expired cloud key | clear error naming the key source (`key_file` / env var); no key echoed anywhere | | |
| 4.5 | No microphone / no recorder installed | clear message naming what to install; no silent no-op, no stuck PID file | | |
| 4.6 | Unsupported TTS language requested | HTTP 400 listing the supported languages, not a stack trace | | |
| 4.7 | Kill the recorder process mid-recording | next hotkey press starts a fresh recording (stale PID file is cleared) | | |
| 4.8 | `jq` not installed | scripts fall back to defaults instead of crashing the turn | | |

## 5. macOS branch (run on a Mac, first teammate = beta acceptance)

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 5.1 | `/voice-setup` picks the macOS adapters | `afplay` / `pbcopy` / `osascript`, Homebrew for anything missing | | |
| 5.2 | Accessibility consent is requested once, explained before it appears | one dialog, then auto-paste works | | |
| 5.3 | `say -v <voice>` fallback path | speaks with the built-in voice when configured | | |
| 5.4 | Apple Silicon: whisper.cpp path if chosen | transcription is noticeably fast; `stt.command` wired correctly | | |
| 5.5 | Nothing in the install required root | true / false | | |

## 6. `/voice-design` (only if the user has an ElevenLabs key)

| # | check | expected | observed | pass |
|---|---|---|---|---|
| 6.1 | Key is read from a file, never pasted into chat or config | `key_file` used; nothing echoed | | |
| 6.2 | A request to imitate a named real person | politely declined, generalized description offered instead | | |
| 6.3 | Previews are saved, numbered, and mapped to their ids | user can tell which is which | | |
| 6.4 | Chosen voice id lands in `tts.cloud.voice_id` | rest of the config survives the edit | | |
| 6.5 | Speak-back in the new voice | plays (player handles mp3) | | |

---

**Sign-off**

| | name | date | verdict |
|---|---|---|---|
| Tester | | | pass / fail |
| Waived items (with reason) | | | |
