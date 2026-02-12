# Clawdbot Service Surface Policy

## Goal
Keep Home Assistant **Automation Actions** minimal and stable, while preserving full panel/runtime functionality via internal authenticated API routes.

## Runtime internals (NOT exposed as HA actions)
These are used by panel/runtime and stay internal via authenticated API:

- **Panel service bridge**
  - API: `POST /api/clawdbot/panel_service`
  - Purpose: invoke internal runtime handlers (chat/theme/setup/agent/avatar/derived) without registering HA actions.
- Session list/status/history/send/spawn helper APIs
  - `GET /api/clawdbot/sessions`
  - `GET /api/clawdbot/session_status`
  - `GET /api/clawdbot/sessions_history`
  - `POST /api/clawdbot/sessions_send`
  - `POST /api/clawdbot/sessions_spawn`

Old automation-facing actions for these internals remain removed.

## Automation-visible services (kept)
Only keep services intended for user automations / explicit ops:

- `clawdbot.notify_event`
- `clawdbot.agent_prompt`
- `clawdbot.journal_append`
- `clawdbot.journal_list`
- `clawdbot.set_connection_overrides`
- `clawdbot.reset_connection_overrides`
- `clawdbot.set_mapping`
- `clawdbot.ha_get_states`
- `clawdbot.ha_call_service`
- `clawdbot.tools_invoke` (advanced/admin)
- `clawdbot.tts_vibevoice_health`
- `clawdbot.tts_vibevoice`
- `clawdbot.build_info`
- `clawdbot.agent_pulse`

## Regression guardrail
Before releasing:
1. Panel smoke (Setup/Chat/Automations/Agent tab switching)
2. Chat smoke (session dropdown population + send + poll)
3. Console smoke: no `Action clawdbot.* not found`
4. `build_info.services` does not reintroduce internal-only chat/session actions
