# OpenClaw Home Assistant Integration (clawdbot)

Custom Home Assistant integration that connects HA to an OpenClaw Gateway.

## What it does
- Adds a sidebar panel (iframe) served by HA.
- Adds HA services:
  - `clawdbot.send_chat`: send a message to a configured Discord channel via OpenClaw `message.send`.
  - `clawdbot.notify_event`: send a **structured event** into OpenClaw (for automations).
  - `clawdbot.tools_invoke`: invoke an OpenClaw tool via Gateway `/tools/invoke`.
  - `clawdbot.ha_get_states` / `clawdbot.ha_call_service`: local HA inspection/control helpers.

## Architecture (Chat)
Home Assistant iframe panels have tricky auth boundaries; direct iframe `fetch('/api/...')` calls can 401/ban.
This integration uses HA **services** as the stable bridge:

- **Send (panel → gateway)**
  - Panel calls: `clawdbot.chat_send`
  - Backend calls gateway tool: `sessions_send`

- **Poll (panel → backend → gateway)**
  - Panel calls: `clawdbot.chat_poll` (fire-and-forget)
  - Backend calls gateway tool: `sessions_history` and appends new agent messages into HA Store

- **Delta fetch (panel → backend store)**
  - Panel calls: `clawdbot.chat_history_delta` via HA websocket `call_service` with `return_response: true`
  - Backend returns items from HA Store (optionally since `after_ts` / or `before_id` paging)

This keeps the iframe UI responsive and avoids relying on `window.parent.hass` internals.

See: `docs/AUTOMATION_EXAMPLES.md` for ready-to-paste HA automation YAML using `clawdbot.notify_event`.

## Install (manual)
1. Copy `custom_components/clawdbot/` into your HA `config/custom_components/`.
2. Add config:

```yaml
clawdbot:
  # Browser-facing iframe URL (preferred key)
  # MUST be reachable from your browser (the machine viewing HA)
  panel_url: "http://<OPENCLAW_PUBLIC_HOST>:7773/__clawdbot__/canvas/ha-panel/"

  # Legacy alias (supported for backward compatibility)
  # url: "http://<OPENCLAW_PUBLIC_HOST>:7773/__clawdbot__/canvas/ha-panel/"

  # HA-backend-facing OpenClaw Gateway URL
  # Docker-on-Linux tip: use the host bridge gateway (often 172.17.0.1)
  # (Alternatively configure extra_hosts host.docker.internal:host-gateway)
  gateway_url: "http://172.17.0.1:7773"

  token: "<OPENCLAW_GATEWAY_TOKEN>"

  # For current MVP: Discord channel id to post into
  session_key: "<DISCORD_CHANNEL_ID>"
```

3. Restart Home Assistant.

## Dev
See `docs/SETUP.md`.
