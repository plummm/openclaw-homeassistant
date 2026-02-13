#!/usr/bin/env bash
set -euo pipefail

HA_BASE="${HA_BASE:-https://ha.etenal.me}"
SECRET_FILE="${SECRET_FILE:-/tmp/prod-ha-secret}"
OUT_DIR="${OUT_DIR:-/home/etenal/clawd/projects/ha-clawdbot/out}"

WIDTH="${WIDTH:-1366}"
HEIGHT="${HEIGHT:-768}"
DISPLAY_NUM="${DISPLAY_NUM:-102}"
DPI="${DPI:-96}"
DUR="${DUR:-35}"
DBG_PORT="${DBG_PORT:-9226}"

mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_PREFIX="${OUT_DIR}/prod-agent-sync-${TS}"
OUT_FILE="${OUT_DIR}/prod-agent-sync-${TS}.mp4"
TARGET_FILE="${OUT_DIR}/prod-agent-sync-${TS}.target"
STATE_BEFORE_JSON="${OUT_DIR}/prod-agent-sync-${TS}.state-before.json"

if [[ ! -f "$SECRET_FILE" ]]; then
  echo "Missing SECRET_FILE: $SECRET_FILE" >&2
  exit 2
fi

TOKEN="$(grep -o 'eyJ[^ ]*' "$SECRET_FILE" | tail -n1)"
HA_USER="$(awk -F': ' '/^username:/{print $2}' "$SECRET_FILE" | head -n1)"
HA_PASS="$(awk -F': ' '/^password:/{print $2}' "$SECRET_FILE" | head -n1)"

if [[ -z "$TOKEN" || -z "$HA_USER" || -z "$HA_PASS" ]]; then
  echo "Failed parsing TOKEN/HA_USER/HA_PASS from $SECRET_FILE" >&2
  exit 2
fi

# Save current profile state for restore
curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"service":"agent_state_get","data":{}}' \
  "$HA_BASE/api/clawdbot/panel_service" > "$STATE_BEFORE_JSON"

OLD_MOOD="$(python3 - <<'PY' "$STATE_BEFORE_JSON"
import json,sys
p=sys.argv[1]
j=json.load(open(p))
prof=((j.get('result') or {}).get('profile') or {})
print((prof.get('mood') or '').strip())
PY
)"
OLD_DESC="$(python3 - <<'PY' "$STATE_BEFORE_JSON"
import json,sys
p=sys.argv[1]
j=json.load(open(p))
prof=((j.get('result') or {}).get('profile') or {})
print((prof.get('description') or '').strip())
PY
)"
OLD_SOURCE="$(python3 - <<'PY' "$STATE_BEFORE_JSON"
import json,sys
p=sys.argv[1]
j=json.load(open(p))
prof=((j.get('result') or {}).get('profile') or {})
print((prof.get('source') or 'restore').strip())
PY
)"

TEST_MOOD="focused"
TEST_DESC="SYNC_TEST_${TS}_agent_card"
TEST_SOURCE="agent_sync_test"

restore_state() {
  if [[ -n "${OLD_MOOD}" || -n "${OLD_DESC}" ]]; then
    python3 - <<'PY' "$HA_BASE" "$TOKEN" "$OLD_MOOD" "$OLD_DESC" "$OLD_SOURCE"
import json,sys,urllib.request
base,token,mood,desc,source = sys.argv[1:6]
payload = {
  "service": "agent_state_set",
  "data": {
    "mood": mood if mood else None,
    "description": desc if desc else None,
    "source": source or "restore"
  }
}
req = urllib.request.Request(
  base + '/api/clawdbot/panel_service',
  data=json.dumps(payload).encode('utf-8'),
  headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
  method='POST'
)
with urllib.request.urlopen(req, timeout=20) as r:
  _ = r.read()
print('restore: agent_state_set ok')
PY
  else
    curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d '{"service":"agent_state_reset","data":{"clear_journal":false}}' \
      "$HA_BASE/api/clawdbot/panel_service" >/dev/null || true
    echo "restore: agent_state_reset ok"
  fi
}
trap restore_state EXIT

# Apply deterministic test update
python3 - <<'PY' "$HA_BASE" "$TOKEN" "$TEST_MOOD" "$TEST_DESC" "$TEST_SOURCE"
import json,sys,urllib.request
base,token,mood,desc,source = sys.argv[1:6]
payload = {
  "service": "agent_state_set",
  "data": {
    "mood": mood,
    "description": desc,
    "source": source
  }
}
req = urllib.request.Request(
  base + '/api/clawdbot/panel_service',
  data=json.dumps(payload).encode('utf-8'),
  headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
  method='POST'
)
with urllib.request.urlopen(req, timeout=20) as r:
  txt = r.read().decode('utf-8', errors='ignore')
print('set test state:', txt[:220])
PY

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
trap 'cleanup; restore_state' EXIT

Xvfb :$DISPLAY_NUM -screen 0 ${WIDTH}x${HEIGHT}x24 -dpi $DPI >"${LOG_PREFIX}.xvfb.log" 2>&1 &
XVFB_PID=$!
export DISPLAY=:$DISPLAY_NUM
sleep 1

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
node /home/etenal/clawd/projects/ha-clawdbot/scripts/cdp_login.mjs >"${LOG_PREFIX}.login.log" 2>&1

if [[ ! -s "$TARGET_FILE" ]]; then
  echo "CDP target id missing: $TARGET_FILE" >&2
  exit 3
fi
export CDP_TARGET_ID="$(cat "$TARGET_FILE")"

export CDP_NAV_URL="$HA_BASE/clawdbot" CDP_WAIT_INCLUDES="/clawdbot" CDP_WAIT_PATH_PREFIX="/clawdbot"
node /home/etenal/clawd/projects/ha-clawdbot/scripts/cdp_nav.mjs >"${LOG_PREFIX}.nav.log" 2>&1
sleep 2

ffmpeg -y -video_size ${WIDTH}x${HEIGHT} -framerate 30 -f x11grab -i :$DISPLAY_NUM \
  -t "$DUR" -pix_fmt yuv420p -c:v libx264 -preset veryfast -crf 23 \
  "$OUT_FILE" >"${LOG_PREFIX}.ffmpeg.log" 2>&1 &
FF_PID=$!

sleep 2
export AGENT_SYNC_SHOT_PREFIX="${LOG_PREFIX}" AGENT_SYNC_TARGET_MOOD="$TEST_MOOD" AGENT_SYNC_TARGET_DESC_TOKEN="SYNC_TEST_${TS}" AGENT_SYNC_WAIT_MS=22000
node /home/etenal/clawd/projects/ha-clawdbot/scripts/cdp_agent_card_verify_sync.mjs >"${LOG_PREFIX}.verify.log" 2>&1 || true

wait "$FF_PID" || true
FF_PID=""

echo "VIDEO=$OUT_FILE"
echo "LOG_PREFIX=$LOG_PREFIX"
echo "STATE_BEFORE=$STATE_BEFORE_JSON"
