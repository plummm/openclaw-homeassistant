#!/usr/bin/env bash
set -euo pipefail

# v11: CDP login + hard gates on SAME target; CDP navigation to /clawdbot#chat.
# xdotool only used for in-panel clicks once the correct page is loaded.

HA_BASE="${HA_BASE:-http://100.96.0.2:8123}"
HA_USER="${HA_USER:-test}"
HA_PASS="${HA_PASS:-12345}"
OUT_DIR="${OUT_DIR:-/home/etenal/clawd/projects/ha-clawdbot/out}"
OUT_FILE="${OUT_FILE:-$OUT_DIR/openclaw-voice-e2e-v11.mp4}"
FRAME_OV="${FRAME_OV:-$OUT_DIR/openclaw-voice-e2e-v11-overview.png}"
FRAME5="${FRAME5:-$OUT_DIR/openclaw-voice-e2e-v11-check-5s.png}"

WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
DPI="${DPI:-96}"
DUR="${DUR:-55}"
DBG_PORT="${DBG_PORT:-9222}"

mkdir -p "$OUT_DIR"

URL_PANEL="$HA_BASE/clawdbot#chat"

TMP_PROFILE="$(mktemp -d)"
cleanup() { rm -rf "$TMP_PROFILE" 2>/dev/null || true; }
trap cleanup EXIT

Xvfb :$DISPLAY_NUM -screen 0 ${WIDTH}x${HEIGHT}x24 -dpi $DPI >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
export DISPLAY=:$DISPLAY_NUM
sleep 0.8

chromium --no-sandbox --user-data-dir="$TMP_PROFILE" --window-size=${WIDTH},${HEIGHT} \
  --remote-debugging-port=$DBG_PORT \
  --autoplay-policy=no-user-gesture-required \
  --disable-dev-shm-usage \
  --disable-features=PasswordManager,PasswordManagerOnboarding,AutofillServerCommunication,AutofillAddressProfileSavePrompt,AutofillCreditCardSavePrompt \
  --disable-save-password-bubble \
  --password-store=basic \
  --no-first-run --disable-sync --disable-default-apps \
  "$HA_BASE/" >/tmp/chromium-e2e.log 2>&1 &
CH_PID=$!

get_wid() { xdotool search --onlyvisible --class chromium | tail -n1; }
click() { local WID="$1"; local X="$2"; local Y="$3"; xdotool mousemove --window "$WID" --sync "$X" "$Y" click 1; }
keyw() { local WID="$1"; shift; xdotool key --window "$WID" --clearmodifiers "$@"; }
shot() { ffmpeg -y -video_size ${WIDTH}x${HEIGHT} -f x11grab -i :$DISPLAY_NUM -frames:v 1 "$1" >/dev/null 2>&1 || true; }

dismiss_bubble() {
  local WID="$1"
  keyw "$WID" Escape || true
  sleep 0.2
  click "$WID" 1040 510 || true
  sleep 0.2
  click "$WID" 1235 135 || true
  sleep 0.2
  keyw "$WID" Escape || true
}

# ---- CDP LOGIN (creates & records target id) ----
sleep 2.0
export HA_BASE HA_USER HA_PASS CDP_PORT="$DBG_PORT" CDP_TIMEOUT_MS=60000
TARGET_FILE="$OUT_DIR/openclaw-voice-e2e-v11.target"
rm -f "$TARGET_FILE" || true
export CDP_TARGET_FILE="$TARGET_FILE"
node scripts/cdp_login.mjs

if [[ ! -s "$TARGET_FILE" ]]; then
  echo "GATE_FAIL: missing CDP target id" >&2
  kill $CH_PID >/dev/null 2>&1 || true
  kill $XVFB_PID >/dev/null 2>&1 || true
  exit 10
fi
export CDP_TARGET_ID="$(cat "$TARGET_FILE")"

# Gate 1: same-tab /lovelace/0 (ensure CDP login actually stuck)
export CDP_NAV_URL="$HA_BASE/lovelace/0"
export CDP_WAIT_INCLUDES="/lovelace/0"
export CDP_WAIT_PATH_PREFIX="/lovelace"
if ! node scripts/cdp_nav.mjs; then
  echo "CDP_NAV_FAIL: login did not persist. Capturing fail state." >&2
  # Try to grab screenshot of the failure
  WID="$(get_wid)"
  if [[ -n "$WID" ]]; then
    dismiss_bubble "$WID"
    shot "${OUT_DIR}/openclaw-voice-e2e-v11-fail.png"
  fi
  kill $CH_PID >/dev/null 2>&1 || true
  kill $XVFB_PID >/dev/null 2>&1 || true
  exit 10
fi

WID="$(get_wid)"
if [[ -z "$WID" ]]; then
  echo "ERROR: chromium window not found" >&2
  exit 2
fi

dismiss_bubble "$WID"
shot "$FRAME_OV"

# Gate 2: same-tab /clawdbot
export CDP_NAV_URL="$URL_PANEL"
export CDP_WAIT_INCLUDES="/clawdbot"
export CDP_WAIT_PATH_PREFIX="/clawdbot"
export CDP_TIMEOUT_MS=90000
node scripts/cdp_nav.mjs
sleep 2.0

dismiss_bubble "$WID"

# Start recording
ffmpeg -y -video_size ${WIDTH}x${HEIGHT} -framerate 30 -f x11grab -i :$DISPLAY_NUM \
  -t "$DUR" -pix_fmt yuv420p -c:v libx264 -preset veryfast -crf 23 \
  "$OUT_FILE" >/tmp/ffmpeg-e2e.log 2>&1 &
FF_PID=$!

# In-panel clicks
click "$WID" 520 200
sleep 1.0
click "$WID" 860 305
sleep 2.0
click "$WID" 900 430

wait $FF_PID || true
ffmpeg -y -ss 00:00:05 -i "$OUT_FILE" -frames:v 1 "$FRAME5" >/dev/null 2>&1 || true

kill $CH_PID >/dev/null 2>&1 || true
kill $XVFB_PID >/dev/null 2>&1 || true

echo "Wrote: $OUT_FILE"
echo "Frames: $FRAME_OV $FRAME5"