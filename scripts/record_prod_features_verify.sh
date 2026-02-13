#!/usr/bin/env bash
set -euo pipefail

# Record a deterministic prod HA panel feature verification:
# - login
# - verify session list, listen button, voice mode, theme options, picker

HA_BASE="${HA_BASE:-https://ha.etenal.me}"
SECRET_FILE="${SECRET_FILE:-/tmp/prod-ha-secret}"
OUT_DIR="${OUT_DIR:-/home/etenal/clawd/projects/ha-clawdbot/out}"

WIDTH="${WIDTH:-1366}"
HEIGHT="${HEIGHT:-768}"
DISPLAY_NUM="${DISPLAY_NUM:-101}" # Use different display to avoid conflict
DPI="${DPI:-96}"
DUR="${DUR:-45}"
DBG_PORT="${DBG_PORT:-9225}"

mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="${OUT_DIR}/prod-feat-verify-${TS}.mp4"
LOG_PREFIX="${OUT_DIR}/prod-feat-verify-${TS}"
TARGET_FILE="${OUT_DIR}/prod-feat-verify-${TS}.target"

if [[ ! -f "$SECRET_FILE" ]]; then
  echo "Missing SECRET_FILE: $SECRET_FILE" >&2
  exit 2
fi

HA_USER="$(awk -F': ' '/^username:/{print $2}' "$SECRET_FILE" | head -n1)"
HA_PASS="$(awk -F': ' '/^password:/{print $2}' "$SECRET_FILE" | head -n1)"
if [[ -z "${HA_USER}" || -z "${HA_PASS}" ]]; then
  echo "Could not parse username/password from $SECRET_FILE" >&2
  exit 2
fi

TMP_PROFILE="$(mktemp -d)"
XVFB_PID=""
CH_PID=""
FF_PID=""
cleanup() {
  [[ -n "$FF_PID" ]] && kill "$FF_PID" >/dev/null 2>&1 || true
  [[ -n "$CH_PID" ]] && kill "$CH_PID" >/dev/null 2>&1 || true
  [[ -n "$XVFB_PID" ]] && kill "$XVFB_PID" >/dev/null 2>&1 || true
  rm -rf "$TMP_PROFILE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Starting Xvfb..."
Xvfb :$DISPLAY_NUM -screen 0 ${WIDTH}x${HEIGHT}x24 -dpi $DPI >"${LOG_PREFIX}.xvfb.log" 2>&1 &
XVFB_PID=$!
export DISPLAY=:$DISPLAY_NUM
sleep 1

echo "Starting Chromium..."
chromium \
  --no-sandbox \
  --user-data-dir="$TMP_PROFILE" \
  --window-size=${WIDTH},${HEIGHT} \
  --remote-debugging-port=$DBG_PORT \
  --autoplay-policy=no-user-gesture-required \
  --disable-dev-shm-usage \
  --disable-features=PasswordManager,PasswordManagerOnboarding,AutofillServerCommunication,AutofillAddressProfileSavePrompt,AutofillCreditCardSavePrompt \
  --disable-save-password-bubble \
  --password-store=basic \
  --no-first-run --disable-sync --disable-default-apps \
  "$HA_BASE/" >"${LOG_PREFIX}.chromium.log" 2>&1 &
CH_PID=$!

# Wait for Chrome DevTools endpoint to be reachable
for i in {1..40}; do
  if curl -fsS "http://127.0.0.1:${DBG_PORT}/json/version" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
  if [[ "$i" -eq 40 ]]; then
    echo "DevTools endpoint not ready on :${DBG_PORT}" >&2
    exit 4
  fi
done

export HA_BASE HA_USER HA_PASS CDP_PORT="$DBG_PORT" CDP_TIMEOUT_MS=90000 CDP_TARGET_FILE="$TARGET_FILE"
echo "Login..."
node /home/etenal/clawd/projects/ha-clawdbot/scripts/cdp_login.mjs >"${LOG_PREFIX}.login.log" 2>&1

if [[ ! -s "$TARGET_FILE" ]]; then
  echo "CDP target id missing: $TARGET_FILE" >&2
  exit 3
fi
export CDP_TARGET_ID="$(cat "$TARGET_FILE")"

# Navigate to panel
export CDP_NAV_URL="$HA_BASE/clawdbot" CDP_WAIT_INCLUDES="/clawdbot" CDP_WAIT_PATH_PREFIX="/clawdbot"
echo "Navigating to panel..."
node /home/etenal/clawd/projects/ha-clawdbot/scripts/cdp_nav.mjs >"${LOG_PREFIX}.nav.log" 2>&1
sleep 2

# Start recording
echo "Recording..."
ffmpeg -y -video_size ${WIDTH}x${HEIGHT} -framerate 30 -f x11grab -i :$DISPLAY_NUM \
  -t "$DUR" -pix_fmt yuv420p -c:v libx264 -preset veryfast -crf 23 \
  "$OUT_FILE" >"${LOG_PREFIX}.ffmpeg.log" 2>&1 &
FF_PID=$!

sleep 2
export CDP_TIMEOUT_MS=60000 FEATURE_VERIFY_SHOT_PREFIX="${LOG_PREFIX}"
echo "Running verification..."
node /home/etenal/clawd/projects/ha-clawdbot/scripts/cdp_panel_verify_features_cdp.mjs >"${LOG_PREFIX}.verify.log" 2>&1 || true

wait "$FF_PID" || true
FF_PID=""

echo "VIDEO=$OUT_FILE"
echo "LOG_PREFIX=$LOG_PREFIX"
echo "Done."
