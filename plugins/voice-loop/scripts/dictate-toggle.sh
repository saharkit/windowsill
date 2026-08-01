#!/usr/bin/env bash
# voice-loop — push-to-talk toggle. Bind it to a global hotkey (e.g. F9):
#   press once  -> recording starts
#   press again -> recording stops, audio goes to STT, the text lands in your clipboard
#                  (and, if auto_paste is enabled, is pasted into the focused window)
#
# Usage: dictate-toggle.sh [send|paste]
#   send  (default) paste the text AND press Enter — hands-free prompting
#   paste           paste only, you press Enter yourself
#
# Config: ~/.config/voice-loop/config.json (see README). Zero-root by default: the text is put on the
# clipboard and you press your own paste key. Auto-paste is the opt-in tier.
set -u

CFG="${VOICE_LOOP_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/voice-loop/config.json}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/voice-loop"
mkdir -p "$STATE" 2>/dev/null || true
LOG="$STATE/dictate.log"
PIDFILE="$STATE/dictate.pid"
WAV="$STATE/dictate.wav"

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
MODE="${1:-$(cfg '.dictate.mode' 'send')}"
PASTE_KEY=$(cfg '.dictate.paste_key' "$([ "$OS" = Darwin ] && echo cmd+v || echo ctrl+shift+v)")
AUTO_PASTE=$(cfg '.dictate.auto_paste' 'false')
RECORDER=$(cfg '.dictate.recorder' 'auto')
CLIPBOARD=$(cfg '.dictate.clipboard' 'auto')
START_SOUND=$(cfg '.dictate.start_sound' '')
STOP_SOUND=$(cfg '.dictate.stop_sound' '')
PLAYER=$(cfg '.speak.player' "$([ "$OS" = Darwin ] && echo afplay || echo 'aplay -q')")
BACKEND=$(cfg '.stt.backend' 'lan')
ENDPOINT=$(cfg '.stt.endpoint' 'http://127.0.0.1:8355')
# language: top-level "language" is the one you set; ".stt.language" is the advanced escape for
# people who dictate in one language and listen in another.
LANGUAGE=$(cfg '.stt.language' "$(cfg '.language' 'ru')")
STT_MODEL=$(cfg '.stt.model' 'whisper-1')
STT_COMMAND=$(cfg '.stt.command' '')
KEY_ENV=$(cfg '.stt.cloud.api_key_env' "$(cfg '.stt.api_key_env' 'VOICE_LOOP_STT_API_KEY')")
KEY_FILE=$(cfg '.stt.cloud.key_file' '')
TIMEOUT=$(cfg '.stt.timeout' '60')

read_key() { # key_file wins over the env var; the key itself is NEVER stored in config.json
  if [ -n "$KEY_FILE" ] && [ -f "${KEY_FILE/#\~/$HOME}" ]; then
    tr -d ' \t\r\n' <"${KEY_FILE/#\~/$HOME}"
  else
    printf '%s' "${!KEY_ENV:-}"
  fi
}

note() {
  case "$OS" in
    Darwin) osascript -e "display notification \"$1\" with title \"voice-loop\"" >/dev/null 2>&1 || true ;;
    *) command -v notify-send >/dev/null 2>&1 && notify-send -t 1500 "voice-loop" "$1" >/dev/null 2>&1 || true ;;
  esac
}

sound() { # sound <path>
  [ -n "$1" ] && [ -f "$1" ] || return 0
  # shellcheck disable=SC2086  # PLAYER is an intentional command+flags string from config
  ($PLAYER "$1" >/dev/null 2>&1 &) || true
}

start_recording() {
  case "$RECORDER" in
    auto)
      if [ "$OS" = "Darwin" ]; then
        if command -v rec >/dev/null 2>&1; then RECORDER="sox"
        elif command -v ffmpeg >/dev/null 2>&1; then RECORDER="ffmpeg"
        fi
      else
        if command -v pw-record >/dev/null 2>&1; then RECORDER="pw-record"
        elif command -v arecord >/dev/null 2>&1; then RECORDER="arecord"
        elif command -v ffmpeg >/dev/null 2>&1; then RECORDER="ffmpeg"
        fi
      fi
      ;;
  esac
  case "$RECORDER" in
    pw-record) pw-record --rate 16000 --channels 1 "$WAV" >/dev/null 2>>"$LOG" & ;;
    arecord) arecord -q -f S16_LE -r 16000 -c 1 "$WAV" >/dev/null 2>>"$LOG" & ;;
    sox) rec -q -r 16000 -c 1 -b 16 "$WAV" >/dev/null 2>>"$LOG" & ;;
    ffmpeg)
      if [ "$OS" = "Darwin" ]; then
        ffmpeg -hide_banner -loglevel error -f avfoundation -i ":default" -ar 16000 -ac 1 -y "$WAV" >/dev/null 2>>"$LOG" &
      else
        ffmpeg -hide_banner -loglevel error -f alsa -i default -ar 16000 -ac 1 -y "$WAV" >/dev/null 2>>"$LOG" &
      fi
      ;;
    *)
      note "no recorder found — install pw-record/arecord (Linux) or sox/ffmpeg (macOS)"
      log "no recorder available"
      exit 1
      ;;
  esac
  echo $! >"$PIDFILE"
  log "recording via $RECORDER pid=$(cat "$PIDFILE")"
  sound "$START_SOUND"
  note "recording…"
}

transcribe() { # prints the transcript on stdout
  if [ -n "$STT_COMMAND" ]; then
    # local engine without a server: the command gets the wav path as its last argument
    # and prints the transcript (e.g. whisper.cpp: "whisper-cli -m model.bin -nt -f")
    sh -c "$STT_COMMAND \"$WAV\"" 2>>"$LOG"
    return
  fi
  local raw=""
  if [ "$BACKEND" = "cloud" ]; then
    local key
    key=$(read_key)
    if [ -z "$key" ]; then log "cloud stt: no key (key_file unset/unreadable and \$$KEY_ENV empty)"; return; fi
    raw=$(curl -s --noproxy '*' -m "$TIMEOUT" -X POST "$ENDPOINT/v1/audio/transcriptions" \
      -H "Authorization: Bearer $key" -F "file=@$WAV" -F "model=$STT_MODEL" -F "language=$LANGUAGE" 2>>"$LOG")
  else
    raw=$(curl -s --noproxy '*' -m "$TIMEOUT" -F "audio=@$WAV" "$ENDPOINT/stt?language=$LANGUAGE" 2>>"$LOG")
  fi
  printf '%s' "$raw" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("text","").strip())
except Exception: print("")' 2>>"$LOG"
}

to_clipboard() { # to_clipboard <text>
  case "$CLIPBOARD" in
    auto)
      if [ "$OS" = "Darwin" ]; then CLIPBOARD="pbcopy"
      elif [ -n "${WAYLAND_DISPLAY:-}" ] && command -v wl-copy >/dev/null 2>&1; then CLIPBOARD="wl-copy"
      elif command -v xclip >/dev/null 2>&1; then CLIPBOARD="xclip"
      elif command -v wl-copy >/dev/null 2>&1; then CLIPBOARD="wl-copy"
      fi
      ;;
  esac
  case "$CLIPBOARD" in
    pbcopy) printf '%s' "$1" | pbcopy 2>>"$LOG" ;;
    wl-copy)
      printf '%s' "$1" | wl-copy 2>>"$LOG"
      printf '%s' "$1" | wl-copy --primary 2>>"$LOG"
      ;;
    xclip)
      printf '%s' "$1" | xclip -selection clipboard 2>>"$LOG"
      printf '%s' "$1" | xclip -selection primary 2>>"$LOG"
      ;;
    *) log "no clipboard tool (install wl-clipboard or xclip)"; return 1 ;;
  esac
}

auto_paste() {
  # Returns 0 when the keystroke was actually sent, 1 when we could not — the caller then falls back
  # to "it is in your clipboard, press <paste_key>". That fallback is the DEFAULT tier, not a failure.
  local enter="${1:-no}"
  if [ "$OS" = "Darwin" ]; then
    command -v osascript >/dev/null 2>&1 || return 1
    local using
    case "$PASTE_KEY" in
      ctrl+shift+v) using="{control down, shift down}" ;;
      shift+insert) using="{command down}" ;; # no Insert key on mac keyboards — use the native paste
      *) using="{command down}" ;;
    esac
    osascript -e "tell application \"System Events\" to keystroke \"v\" using $using" >/dev/null 2>>"$LOG" || return 1
    if [ "$enter" = "yes" ]; then
      sleep 0.25
      osascript -e 'tell application "System Events" to key code 36' >/dev/null 2>>"$LOG" || true
    fi
    return 0
  fi

  # Wayland with a running ydotoold: named combos only (older ydotool cannot type non-ASCII),
  # which is exactly why we paste a clipboard instead of typing the text.
  local sock="${YDOTOOL_SOCKET:-/tmp/.ydotool_socket}"
  if command -v ydotool >/dev/null 2>&1 && [ -S "$sock" ]; then
    sleep 0.15
    YDOTOOL_SOCKET="$sock" ydotool key "$PASTE_KEY" >/dev/null 2>>"$LOG" || return 1
    if [ "$enter" = "yes" ]; then sleep 0.25; YDOTOOL_SOCKET="$sock" ydotool key enter >/dev/null 2>>"$LOG" || true; fi
    return 0
  fi
  # wlroots/KDE: wtype is pure userland (no daemon, no root). GNOME/Mutter exposes no
  # virtual-keyboard protocol, so wtype is not available there — the clipboard path covers it.
  if command -v wtype >/dev/null 2>&1; then
    case "$PASTE_KEY" in
      ctrl+shift+v) wtype -M ctrl -M shift -k v -m shift -m ctrl >/dev/null 2>>"$LOG" || return 1 ;;
      shift+insert) wtype -M shift -k Insert -m shift >/dev/null 2>>"$LOG" || return 1 ;;
      *) wtype -M ctrl -k v -m ctrl >/dev/null 2>>"$LOG" || return 1 ;;
    esac
    if [ "$enter" = "yes" ]; then sleep 0.25; wtype -k Return >/dev/null 2>>"$LOG" || true; fi
    return 0
  fi
  # X11
  if [ -n "${DISPLAY:-}" ] && command -v xdotool >/dev/null 2>&1; then
    case "$PASTE_KEY" in
      shift+insert) xdotool key shift+Insert >/dev/null 2>>"$LOG" || return 1 ;;
      *) xdotool key "$PASTE_KEY" >/dev/null 2>>"$LOG" || return 1 ;;
    esac
    if [ "$enter" = "yes" ]; then sleep 0.25; xdotool key Return >/dev/null 2>>"$LOG" || true; fi
    return 0
  fi
  return 1
}

stop_and_transcribe() {
  local rpid
  rpid=$(cat "$PIDFILE" 2>/dev/null || echo "")
  rm -f "$PIDFILE"
  # SIGINT lets sox/ffmpeg finalize the WAV header; TERM is the fallback for pw-record/arecord.
  kill -INT "$rpid" 2>/dev/null || true
  for _ in $(seq 1 30); do kill -0 "$rpid" 2>/dev/null || break; sleep 0.1; done
  kill -0 "$rpid" 2>/dev/null && kill "$rpid" 2>/dev/null
  for _ in $(seq 1 30); do kill -0 "$rpid" 2>/dev/null || break; sleep 0.1; done
  # the recorder flushes the tail of the file AFTER it stops accepting samples — this short
  # settle is the difference between a complete phrase and a truncated one
  sleep 0.2
  sound "$STOP_SOUND"
  note "transcribing…"

  local text
  text=$(transcribe)
  log "transcript: ${text:0:120}"
  mv -f "$WAV" "$STATE/dictate-last.wav" 2>/dev/null || true

  if [ -z "$text" ]; then
    note "nothing recognized"
    log "empty transcription"
    exit 0
  fi

  to_clipboard "$text" || { note "no clipboard tool available"; exit 1; }

  if [ "$AUTO_PASTE" = "true" ]; then
    if auto_paste "$([ "$MODE" = send ] && echo yes || echo no)"; then
      log "auto-pasted (mode=$MODE key=$PASTE_KEY)"
      exit 0
    fi
    log "auto-paste unavailable — clipboard fallback"
  fi
  note "copied — press $PASTE_KEY to paste"
  exit 0
}

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null || echo -1)" 2>/dev/null; then
  stop_and_transcribe
else
  rm -f "$PIDFILE"
  start_recording
fi
