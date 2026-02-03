# Installing OpenClaw (clawdbot) via HACS

> Note: Repository is currently **private**. HACS can add it as a *custom repository* only if your Home Assistant environment can access it (GitHub auth) or once we make it public.

## Option A — HACS (custom repository)
1. In Home Assistant, go to **HACS → Integrations**.
2. Open the **⋮ menu → Custom repositories**.
3. Add repository:
   - **Repository:** `https://github.com/plummm/openclaw-homeassistant`
   - **Category:** `Integration`
4. Find **OpenClaw** in HACS and install.
5. Restart Home Assistant.

## Option B — Manual install
1. Download the repo (or release zip).
2. Copy folder `custom_components/clawdbot/` into your HA config:
   - `<HA_CONFIG>/custom_components/clawdbot/`
3. Restart Home Assistant.

## Configuration (configuration.yaml)
```yaml
clawdbot:
  # Browser access to OpenClaw panel
  url: "http://<OPENCLAW_HOST>:7773/__clawdbot__/canvas/ha-panel/"

  # HA backend access to OpenClaw Gateway
  gateway_url: "http://<OPENCLAW_HOST>:7773"

  # OpenClaw Gateway token (gateway.auth.token)
  token: "<OPENCLAW_GATEWAY_TOKEN>"

  # MVP: Discord channel id to post into via OpenClaw `message.send`
  # (will likely be renamed to `discord_channel_id` in a later release)
  session_key: "<DISCORD_CHANNEL_ID>"

  title: "OpenClaw"
  icon: "mdi:robot"
```

## What you should see
- A sidebar item **OpenClaw** (robot icon) which loads an iframe panel.
- HA services available under `clawdbot.*`.
