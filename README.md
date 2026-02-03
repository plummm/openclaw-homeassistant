# OpenClaw Home Assistant Integration (clawdbot)

Custom Home Assistant integration that connects HA to an OpenClaw Gateway.

## What it does
- Adds a sidebar panel (iframe) pointing at an OpenClaw Canvas/Control UI URL.
- Adds HA services:
  - `clawdbot.send_chat`: send a message to a configured Discord channel via OpenClaw `message.send`.
  - `clawdbot.tools_invoke`: invoke an OpenClaw tool via Gateway `/tools/invoke`.
  - `clawdbot.ha_get_states` / `clawdbot.ha_call_service`: local HA inspection/control helpers.

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
