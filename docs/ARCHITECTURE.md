# Home Assistant ↔ Clawdbot integration (design draft)

## Goal
- A Home Assistant (HA) **custom integration** that connects to a remote Clawdbot server.
- A Clawdbot-side **MCP server/tooling** that can connect to a Home Assistant instance and:
  - list all exposed entities
  - fetch state + attributes
  - call services (turn on/off switches, etc.)
- HA dashboard experience:
  - chatbox with Clawdbot
  - entity list + basic controls
  - optional: event stream, recent ops log, approvals, etc.

## Proposed components

### 1) HA custom integration: `clawdbot`
Location in HA config:
- `custom_components/clawdbot/`

Responsibilities:
- Store Clawdbot connection settings (URL + auth token).
- Provide a HA-side service like `clawdbot.send_chat` that forwards chat messages to Clawdbot.
- Provide a Panel / Dashboard entry (sidebar panel) that renders a UI.

Auth options:
- Token auth header to Clawdbot gateway (matches clawdbot gateway token mode).

UI options:
- **MVP**: HA Panel that embeds an iframe pointing at a Clawdbot-hosted web UI (served by Clawdbot gateway).
- **Better**: true HA custom panel (frontend bundle) that uses HA websocket for entity control and Clawdbot HTTP for chat.

### 2) Clawdbot MCP server for Home Assistant
Implemented as a local MCP stdio server script.

Responsibilities:
- Connect to Home Assistant REST/WebSocket API.
- Expose MCP tools:
  - `ha.get_states`
  - `ha.get_state`
  - `ha.get_services`
  - `ha.call_service`
  - (later) subscribe to state changes via websocket

Security:
- HA Long-Lived Access Token in env/secret.
- Bind any local HA test instances to 127.0.0.1.

## Current status
- Docker test HA: blocked by docker socket permissions (needs sudo approval).
- MCP server script drafted: `/home/etenal/clawd/scripts/mcp_homeassistant.py`
