# Codex Routine (Deterministic)

When a patch is ready to ship:

1) **Preflight**
- `python3 -m py_compile custom_components/clawdbot/__init__.py`
- `node -c ha-config/www/clawdbot-panel.js`

2) **Commit + push (deterministic)**
- Use:
  - `scripts/release_ha_patch.sh -m "<message>"`

3) **Deploy to local test HA (optional, requires sudo + docker)**
- Use:
  - `scripts/release_ha_patch.sh -m "<message>" --deploy`

Notes:
- Never paste secrets (AimlAPI key, tokens) into commits or logs.
- If you bump `PANEL_BUILD_ID`, include it in the commit message.
