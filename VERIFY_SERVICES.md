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

## 5) Created entities (v1 whitelist: pv_next_day_prediction)
1. Open **Developer Tools → Actions**
2. Select action: `clawdbot.created_entity_install`
3. Fill `spec` (object) with a real energy sensor that has long-term statistics (state_class measurement/total/total_increasing), e.g.:
   ```yaml
   id: demo
   title: "PV next-day prediction (7d mean)"
   kind: pv_next_day_prediction
   inputs:
     source_entity_id: sensor.pv_energy_today
     method: mean_last_n_days
     window_days: 7
     unit: kWh
   ```
4. Click **Run**
5. Proof: screenshot showing `{ ok: true }` and returned `spec.entity_id`.
6. In **Developer Tools → States**, search for the returned `spec.entity_id` (e.g. `sensor.clawdbot_pv_next_day_prediction_*`).
   - Proof: screenshot showing the entity exists (state may be `unknown` if recorder stats are not available yet).
7. Select action: `clawdbot.created_entity_list` and click **Run**.
   - Proof: screenshot showing the created entity in `items`.
8. Cleanup: call `clawdbot.created_entity_remove` with `entity_id` set to the created entity.
   - Proof: screenshot showing `{ ok: true }`.

## Notes
- If any action fails, screenshot the error/toast and include returned payload text (redact secrets if present).
- `clawdbot.ha_call_service` is guardrailed via an allowlist; `persistent_notification.create` is expected to be allowed.
