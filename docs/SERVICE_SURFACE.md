# Clawdbot Service Surface Policy

## Goal
Keep Home Assistant **Automation Actions** minimal and stable, while preserving full panel/runtime functionality via internal authenticated API routes.

## Runtime internals (NOT exposed as HA actions)
These are used by panel/chat runtime and should stay internal:

- Session list
  - API: `GET /api/clawdbot/sessions`
  - Old action (removed): `clawdbot.sessions_list`
- Session spawn
  - API: `POST /api/clawdbot/sessions_spawn`
  - Old action (removed): `clawdbot.sessions_spawn`
- Session status
  - API: `GET /api/clawdbot/session_status`
  - Old action (removed): `clawdbot.session_status_get`
- Chat send
  - API: `POST /api/clawdbot/sessions_send`
  - Old action (removed): `clawdbot.chat_send`
- Chat poll/history sync
  - API: `GET /api/clawdbot/sessions_history`
  - Old actions (removed): `clawdbot.chat_poll`, `clawdbot.chat_history_delta`
- Chat optimistic append
  - handled in panel client state (no HA action)
  - Old action (removed): `clawdbot.chat_append`

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
