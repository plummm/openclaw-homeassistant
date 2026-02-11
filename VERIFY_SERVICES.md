# Manual verification (trimmed `clawdbot.*` services)

This repo intentionally trims the Home Assistant action/service surface.

If UI automation is flaky (Xvfb / no window manager), use this 2-minute manual verification via Home Assistant UI.

## Preconditions
- Home Assistant running with this integration installed.
- Service UI schema fix deployed (so fields render in UI): commit `5df4f0a`.
- Open **Developer Tools → Actions**:
  - URL: `/developer-tools/service`

## 4 critical checks (minimum)

### 1) `clawdbot.tts_vibevoice_health`
1. Select action: `clawdbot.tts_vibevoice_health`
2. Click **Run**
3. Proof: screenshot showing the returned result/toast (ok/http_status/etc).

### 2) `clawdbot.tts_vibevoice` (generate-only)
1. Select action: `clawdbot.tts_vibevoice`
2. Fill:
   - `text`: `test one two`
   - `format`: `mp3`
   - leave `media_player_entity_id` empty
3. Click **Run**
4. Proof: screenshot showing returned `audio_url` (should be same-origin `/api/clawdbot/...`).

### 3) `clawdbot.ha_call_service` → `persistent_notification.create`
1. Select action: `clawdbot.ha_call_service`
2. Fill:
   - `domain`: `persistent_notification`
   - `service`: `create`
   - `service_data.title`: `OpenClaw test`
   - `service_data.message`: `ha_call_service ok`
3. Click **Run**
4. Proof: open **Notifications** panel (bell) and screenshot the new notification.

### 4) `clawdbot.journal_append` + side-effect
1. Select action: `clawdbot.journal_append`
2. Fill:
   - `text`: `journal_append ok`
3. Click **Run**
4. Proof (side-effect): Developer Tools → **States** → search `sensor.openclaw_agent_journal_updated` and screenshot its updated timestamp/state.

## Notes
- If any action fails, screenshot the error/toast and include returned payload text (redact secrets if present).
- `clawdbot.ha_call_service` is guardrailed via an allowlist; `persistent_notification.create` is expected to be allowed.
