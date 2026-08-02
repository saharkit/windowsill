#!/usr/bin/env bash
# voice-loop — hardware-free loopback selftest.
#
#   TTS renders a known phrase -> the resulting WAV is fed straight back into STT -> the transcript is
#   compared to the phrase (fuzzy: case, punctuation and stress marks stripped).
#
# No microphone, no speakers, no display: it runs in a bare container and in CI. This is the canonical
# "is my setup actually working" proof, and the last step of /voice-setup.
#
# Usage:
#   selftest.sh [--endpoint URL] [--tts-endpoint URL] [--stt-endpoint URL]
#               [--phrase TEXT] [--language ru] [--speaker baya] [--threshold 0.75] [--keep] [--strict]
# Exit codes: 0 ok · 1 mismatch/failure · 2 nothing configured (prints how to get a backend)
set -u

CFG="${VOICE_LOOP_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop/config.json}"

cfg() { # cfg <jq-path> <default>
  local v=""
  if [ -f "$CFG" ] && command -v jq >/dev/null 2>&1; then
    v=$(jq -r "$1 // empty" "$CFG" 2>/dev/null || true)
  fi
  if [ -n "$v" ] && [ "$v" != "null" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi
}

if [ -f "$CFG" ] && ! command -v jq >/dev/null 2>&1; then
  echo "note: jq not found -- config at $CFG is ignored" >&2
fi

TTS_EP=""; STT_EP=""; PHRASE=""; LANG_=""; SPEAKER=""; THRESHOLD="0.75"; KEEP="no"; STRICT="no"
while [ $# -gt 0 ]; do
  case "$1" in
    --endpoint) TTS_EP="$2"; STT_EP="$2"; shift 2 ;;
    --tts-endpoint) TTS_EP="$2"; shift 2 ;;
    --stt-endpoint) STT_EP="$2"; shift 2 ;;
    --phrase) PHRASE="$2"; shift 2 ;;
    --language) LANG_="$2"; shift 2 ;;
    --speaker) SPEAKER="$2"; shift 2 ;;
    --threshold) THRESHOLD="$2"; shift 2 ;;
    --keep) KEEP="yes"; shift ;;
    --strict) STRICT="yes"; shift ;;
    --local)
      cat <<'TXT'
voice-loop selftest --local
  There is no configured backend yet. To get one on this machine:

    1. python3 -m venv ~/.local/share/voice-loop/venv
    2. ~/.local/share/voice-loop/venv/bin/pip install -r server/requirements.txt
    3. VOICE_LOOP_STT_MODEL=small ~/.local/share/voice-loop/venv/bin/python server/voice_server.py
    4. scripts/selftest.sh --endpoint http://127.0.0.1:8355

  Or let the agent do it: /voice-setup
TXT
      exit 0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

[ -n "$TTS_EP" ] || TTS_EP=$(cfg '.tts.endpoint' '')
[ -n "$STT_EP" ] || STT_EP=$(cfg '.stt.endpoint' '')
[ -n "$PHRASE" ] || PHRASE=$(cfg '.selftest.phrase' '')
[ -n "$LANG_" ] || LANG_=$(cfg '.stt.language' "$(cfg '.language' 'ru')")
[ -n "$SPEAKER" ] || SPEAKER=$(cfg '.tts.speaker' '')

if [ -z "$PHRASE" ]; then
  case "$LANG_" in
    ru) PHRASE="Проверка связи. Голосовой контур работает нормально." ;;
    uk) PHRASE="Перевірка зв'язку. Голосовий контур працює нормально." ;;
    en) PHRASE="This is a connection check. The voice loop is working normally." ;;
    de) PHRASE="Verbindungstest. Der Sprachkreis funktioniert einwandfrei." ;;
    es) PHRASE="Prueba de conexión. El circuito de voz funciona correctamente." ;;
    fr) PHRASE="Test de liaison. La boucle vocale fonctionne correctement." ;;
    *) PHRASE="This is a voice loop check. The speech contour is working." ;;
  esac
fi

command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required" >&2; exit 1; }

if [ -z "$TTS_EP" ] || [ -z "$STT_EP" ]; then
  if [ "$STRICT" = "yes" ]; then
    echo "FAIL: no HTTP endpoints configured (config: $CFG); pass --endpoint URL" >&2
    exit 1
  fi
  echo "No speech endpoint configured (looked in $CFG)."
  echo "Run 'scripts/selftest.sh --local' for how to start one, or '/voice-setup' to let the agent do it."
  exit 2
fi

if [ -n "$(cfg '.tts.command' '')" ] || [ -n "$(cfg '.stt.command' '')" ]; then
  if [ "$STRICT" = "yes" ]; then
    echo "FAIL: direct-command backends cannot be loopback-tested; point --endpoint at an HTTP server" >&2
    exit 1
  fi
  echo "Note: a direct-command backend is configured (tts.command/stt.command)."
  echo "      The loopback below still runs against the HTTP endpoints; the command path is ear-verified."
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/voice-loop-selftest-XXXXXX") || exit 1
if [ "$KEEP" = "no" ]; then trap 'rm -rf "$WORK"' EXIT; else echo "artifacts: $WORK"; fi
WAV="$WORK/loopback.wav"

echo "1/3 synthesizing via $TTS_EP  (language=$LANG_ speaker=${SPEAKER:-<server default>})"
body=$(python3 -c 'import json,sys; print(json.dumps({k: v for k, v in (("text", sys.argv[1]), ("speaker", sys.argv[2]), ("language", sys.argv[3])) if v}))' \
  "$PHRASE" "$SPEAKER" "$LANG_")
http=$(curl -s --noproxy '*' -m 180 -o "$WAV" -w '%{http_code}' -X POST "$TTS_EP/tts" \
  -H 'Content-Type: application/json' --data "$body" || echo 000)
if [ "$http" != "200" ] || [ ! -s "$WAV" ]; then
  echo "FAIL: TTS request to $TTS_EP/tts returned http=$http, $(wc -c <"$WAV" 2>/dev/null || echo 0) bytes." >&2
  echo "      Is the server running?  curl $TTS_EP/health" >&2
  exit 1
fi
if [ "$(head -c 4 "$WAV" 2>/dev/null)" != "RIFF" ]; then
  echo "FAIL: TTS returned $(wc -c <"$WAV") bytes that are not a WAV file. First bytes:" >&2
  head -c 200 "$WAV" >&2; echo >&2
  exit 1
fi
echo "    -> $(wc -c <"$WAV") bytes of WAV"

echo "2/3 transcribing via $STT_EP  (language=$LANG_)"
resp=$(curl -s --noproxy '*' -m 180 -F "audio=@$WAV" "$STT_EP/stt?language=$LANG_" || true)
heard=$(printf '%s' "$resp" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("text","").strip())
except Exception: print("")')
if [ -z "$heard" ]; then
  echo "FAIL: STT returned no text. Raw response:" >&2
  printf '%s\n' "${resp:0:400}" >&2
  exit 1
fi

echo "3/3 comparing"
python3 - "$PHRASE" "$heard" "$THRESHOLD" <<'PY'
import difflib, re, sys, unicodedata

def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))  # drop combining acute stress marks
    s = s.replace("+", "")                                        # drop '+'-notation stress marks
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)               # punctuation is not a difference
    return re.sub(r"\s+", " ", s).strip()

said, heard, threshold = sys.argv[1], sys.argv[2], float(sys.argv[3])
a, b = norm(said), norm(heard)
ratio = difflib.SequenceMatcher(None, a, b).ratio()
print(f"    said : {said}")
print(f"    heard: {heard}")
print(f"    similarity: {ratio:.2f} (threshold {threshold:.2f})")
if ratio < threshold:
    print(
        "FAIL: the loopback transcript does not match the phrase.\n"
        "      Likely causes: STT model too small for this language, wrong 'stt.language',\n"
        "      or the TTS voice/speaker does not speak that language.",
        file=sys.stderr,
    )
    raise SystemExit(1)
print("OK: voice-loop round trip works.")
PY
