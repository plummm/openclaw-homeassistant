# HA test setup (local, docker)

## Start

```bash
cd /home/etenal/clawd/projects/ha-clawdbot
sudo docker compose up -d
```

HA UI (bound to localhost only):
- http://127.0.0.1:8123

If you are on your laptop, tunnel:

```bash
ssh -L 8123:127.0.0.1:8123 <server>
# then open http://127.0.0.1:8123 locally
```

## Create token
After onboarding:
- Profile → Long-Lived Access Tokens → Create

Store token for MCP server tests:

```bash
export HA_BASE_URL=http://127.0.0.1:8123
export HA_TOKEN='...'

/home/etenal/clawd/scripts/mcp_homeassistant.py
```

## Quick MCP smoke test (stdio)

```bash
export HA_TOKEN='...'
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | /home/etenal/clawd/scripts/mcp_homeassistant.py
```
