#!/usr/bin/env bash
# voice-loop — Stop hook: speak the assistant's marker-tagged lines.
#
# Convention: only lines whose first non-space character is the marker (default 🔊) are voiced;
# everything else stays text. The model decides what is worth hearing.
#
# Reads ~/.config/voice-loop/config.json (jq). Never fails a turn: every path exits 0.
#
# Two behaviours below were found by live debugging and are deliberate — do not "simplify" them away:
#   * flush race  — Stop can fire BEFORE the final assistant message is written to the transcript,
#                   so we retry until we see a line DIFFERENT from the previously spoken one;
#   * dedup       — a same-as-last read IS the stale previous turn, so it is dropped, not spoken twice.
set -u

CFG="${VOICE_LOOP_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop/config.json}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/voice-loop"
mkdir -p "$STATE" 2>/dev/null || true
LOG="$STATE/speak.log"

log() {
  [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 1000000 ] && : >"$LOG"
  printf '%s %s\n' "$(date +%FT%T)" "$1" >>"$LOG" 2>/dev/null || true
}

cfg() { # cfg <jq-path> <default>
  local v=""
  if [ -f "$CFG" ] && command -v jq >/dev/null 2>&1; then
    v=$(jq -r "$1 // empty" "$CFG" 2>/dev/null || true)
  fi
  if [ -n "$v" ] && [ "$v" != "null" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi
}

OS=$(uname -s 2>/dev/null || echo unknown)
case "$OS" in
  Darwin) DEF_PLAYER="afplay" ;;
  *) DEF_PLAYER="aplay -q" ;;
esac

ENABLED=$(cfg '.speak.enabled' 'true')
[ "$ENABLED" = "false" ] && exit 0

MARKER=$(cfg '.speak.marker' '🔊')
PLAYER=$(cfg '.speak.player' "$DEF_PLAYER")
MAXCHARS=$(cfg '.speak.max_chars' '600')
TIMEOUT=$(cfg '.speak.timeout' '60')
BACKEND=$(cfg '.tts.backend' 'lan')
ENDPOINT=$(cfg '.tts.endpoint' 'http://127.0.0.1:8355')
SPEAKER=$(cfg '.tts.speaker' '')
# language: top-level "language" is the one you set; ".tts.language" is the advanced escape for
# people who dictate in one language and listen in another.
LANGUAGE=$(cfg '.tts.language' "$(cfg '.language' 'ru')")
COMMAND=$(cfg '.tts.command' '')
PROVIDER=$(cfg '.tts.cloud.provider' 'openai')
VOICE_ID=$(cfg '.tts.cloud.voice_id' "$SPEAKER")
CLOUD_MODEL=$(cfg '.tts.cloud.model' "$([ "$PROVIDER" = elevenlabs ] && echo eleven_multilingual_v2 || echo tts-1)")
OUTPUT_FORMAT=$(cfg '.tts.cloud.output_format' 'mp3_44100_128')
KEY_ENV=$(cfg '.tts.cloud.api_key_env' "$(cfg '.tts.api_key_env' 'VOICE_LOOP_TTS_API_KEY')")
KEY_FILE=$(cfg '.tts.cloud.key_file' '')

cfg_json() { # cfg_json <jq-path> — a config SUBTREE as compact JSON, or empty
  local v=""
  if [ -f "$CFG" ] && command -v jq >/dev/null 2>&1; then
    v=$(jq -c "$1 // empty" "$CFG" 2>/dev/null || true)
  fi
  printf '%s' "$v"
}

# provider-specific synthesis knobs, passed through verbatim (ElevenLabs: stability,
# similarity_boost, style, use_speaker_boost — see the anti-robovoice notes in skills/voice-design)
VOICE_SETTINGS=$(cfg_json '.tts.cloud.voice_settings')

read_key() { # key_file wins over the env var; the key itself is NEVER stored in config.json
  if [ -n "$KEY_FILE" ] && [ -f "${KEY_FILE/#\~/$HOME}" ]; then
    tr -d ' \t\r\n' <"${KEY_FILE/#\~/$HOME}"
  else
    printf '%s' "${!KEY_ENV:-}"
  fi
}

command -v python3 >/dev/null 2>&1 || { log "no python3 — skipping"; exit 0; }

payload=$(cat)
tp=$(printf '%s' "$payload" | (command -v jq >/dev/null 2>&1 && jq -r '.transcript_path // empty' 2>/dev/null || true))
if [ -z "$tp" ]; then
  tp=$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("transcript_path") or "")
except Exception: print("")' 2>/dev/null || true)
fi
[ -n "$tp" ] && [ -f "$tp" ] || exit 0

extract() {
  python3 - "$tp" "$MARKER" "$MAXCHARS" <<'PY' 2>/dev/null || true
import json, sys

path, marker, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
last = None
with open(path, encoding="utf-8") as fh:
    for line in fh:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        parts = [c.get("text", "") for c in (msg.get("content") or []) if c.get("type") == "text"]
        if any(parts):
            last = "\n".join(parts)
if last:
    out = [ln.lstrip()[len(marker):].strip() for ln in last.splitlines() if ln.lstrip().startswith(marker)]
    print(" ".join(x for x in out if x)[:limit])
PY
}

LASTF="$STATE/last-spoken"
prev=$(cat "$LASTF" 2>/dev/null || true)
text=""
for _ in 1 2 3 4 5 6; do
  text=$(extract)
  [ -n "$text" ] && [ "$text" != "$prev" ] && break
  sleep 0.7
done
[ -n "$text" ] || exit 0
[ "$text" = "$prev" ] && exit 0
printf '%s' "$text" >"$LASTF"
log "text: ${text:0:80}"

# a fresher line supersedes a still-playing older one
pkill -u "$(id -u)" -f "voice-loop-speak-" 2>/dev/null || true

# tts.command: speak locally without any server (e.g. "say -v Milena" on macOS, or a piper pipeline).
# The command receives the text on stdin and is expected to produce the sound itself.
if [ -n "$COMMAND" ]; then
  printf '%s' "$text" | sh -c "$COMMAND" >/dev/null 2>&1
  log "local command rc=$?"
  exit 0
fi

wav=$(mktemp "${TMPDIR:-/tmp}/voice-loop-speak-XXXXXX") || exit 0
trap 'rm -f "$wav"' EXIT

if [ "$BACKEND" = "cloud" ]; then
  key=$(read_key)
  if [ -z "$key" ]; then log "cloud tts: no key (key_file unset/unreadable and \$$KEY_ENV empty)"; exit 0; fi
  case "$PROVIDER" in
    elevenlabs)
      # the response container follows output_format (mp3 by default) — your speak.player must
      # be able to play it (macOS afplay does; on Linux use mpg123 or ffplay)
      ep=${ENDPOINT:-https://api.elevenlabs.io}
      body=$(python3 -c 'import json,sys
payload = {"text": sys.argv[1], "model_id": sys.argv[2]}
if sys.argv[3]:
    try: payload["voice_settings"] = json.loads(sys.argv[3])
    except ValueError: pass
print(json.dumps(payload))' "$text" "$CLOUD_MODEL" "$VOICE_SETTINGS")
      curl -s --noproxy '*' -m "$TIMEOUT" -X POST "$ep/v1/text-to-speech/$VOICE_ID?output_format=$OUTPUT_FORMAT" \
        -H 'Content-Type: application/json' -H "xi-api-key: $key" --data "$body" -o "$wav" || exit 0
      ;;
    *)
      # OpenAI-compatible speech API
      body=$(python3 -c 'import json,sys; print(json.dumps({"model": sys.argv[1], "voice": sys.argv[2], "input": sys.argv[3], "response_format": "wav"}))' \
        "$CLOUD_MODEL" "${VOICE_ID:-alloy}" "$text")
      curl -s --noproxy '*' -m "$TIMEOUT" -X POST "$ENDPOINT/v1/audio/speech" \
        -H 'Content-Type: application/json' -H "Authorization: Bearer $key" --data "$body" -o "$wav" || exit 0
      ;;
  esac
else
  body=$(python3 -c 'import json,sys; print(json.dumps({k: v for k, v in (("text", sys.argv[1]), ("speaker", sys.argv[2]), ("language", sys.argv[3])) if v}))' \
    "$text" "$SPEAKER" "$LANGUAGE")
  curl -s --noproxy '*' -m "$TIMEOUT" -X POST "$ENDPOINT/tts" \
    -H 'Content-Type: application/json' --data "$body" -o "$wav" || exit 0
fi

if [ ! -s "$wav" ]; then log "empty synthesis from $ENDPOINT"; exit 0; fi
case "$(head -c 1 "$wav" 2>/dev/null)" in
  '{'|'[') log "synthesis returned an error document: $(head -c 200 "$wav")"; exit 0 ;;
esac
# shellcheck disable=SC2086  # PLAYER is an intentional command+flags string from config
$PLAYER "$wav" >/dev/null 2>&1
log "played rc=$? bytes=$(wc -c <"$wav" 2>/dev/null || echo 0)"
exit 0
