"""Clawdbot custom integration.

MVP scope:
- Sidebar iframe panel (points at a Clawdbot-served mini page).
- HA services that let Home Assistant:
  - send a message to Xiaochen via Clawdbot gateway (Telegram) (clawdbot.send_chat)
  - call Clawdbot gateway /tools/invoke for arbitrary tools (clawdbot.tools_invoke)
  - control Home Assistant itself via Clawdbot's Home Assistant MCP server (clawdbot.ha_get_states, clawdbot.ha_call_service)

Config (configuration.yaml):

clawdbot:
  url: "http://host.docker.internal:7773/__clawdbot__/canvas/ha-panel/"   # panel URL
  token: "<gateway-token>"                                              # gateway.auth.token
  session_key: "main"

Notes:
- `sessions_send` via gateway /tools/invoke hangs in current Clawdbot build; use message.send for MVP.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.components.http import HomeAssistantView

DOMAIN = "clawdbot"
_LOGGER = logging.getLogger(__name__)

DEFAULT_TITLE = "Clawdbot"
DEFAULT_ICON = "mdi:robot"

# HA-embedded panel is served by Home Assistant itself.
# This avoids OpenClaw Control UI "device identity" requirements and mixed-content issues.
PANEL_PATH = "/api/clawdbot/panel"

DEFAULT_URL = PANEL_PATH  # legacy/override name; now defaults to HA-local panel
DEFAULT_SESSION_KEY = "main"

CONF_URL = "url"  # legacy
CONF_PANEL_URL = "panel_url"  # preferred
CONF_TOKEN = "token"
CONF_SESSION_KEY = "session_key"
CONF_GATEWAY_URL = "gateway_url"

SERVICE_SEND_CHAT = "send_chat"
SERVICE_TOOLS_INVOKE = "tools_invoke"
SERVICE_HA_GET_STATES = "ha_get_states"
SERVICE_HA_CALL_SERVICE = "ha_call_service"


async def _gw_post(session: aiohttp.ClientSession, url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
        txt = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"Gateway HTTP {r.status}: {txt}")
        try:
            return await r.json()
        except Exception:
            return {"raw": txt}


def _derive_gateway_origin(panel_url: str) -> str:
    try:
        from urllib.parse import urlparse

        u = urlparse(panel_url)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return panel_url


PANEL_HTML = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\"/>
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
  <title>Clawdbot (HA Panel)</title>
  <style>
    body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;padding:16px;}
    input,button,textarea{font:inherit;}
    code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;}
    .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
    .card{border:1px solid #ddd;border-radius:10px;padding:12px;margin:12px 0;}
    .muted{color:#666;font-size:13px;}
    .ok{color:#0a7a2f;}
    .bad{color:#a00000;}
    .entities{max-height:420px;overflow:auto;border:1px solid #eee;border-radius:8px;padding:8px;}
    .ent{display:flex;gap:10px;align-items:center;justify-content:space-between;border-bottom:1px solid #f0f0f0;padding:6px 0;}
    .ent:last-child{border-bottom:none;}
    .ent-id{font-weight:600;}
    .ent-state{color:#444;}
    .btn{padding:6px 10px;border:1px solid #ccc;border-radius:8px;background:#fff;cursor:pointer;}
    .btn:hover{background:#fafafa;}
    .btn.primary{border-color:#3b82f6;}
  </style>
</head>
<body>
  <h1>Clawdbot (Home Assistant Panel)</h1>
  <div class=\"muted\">Served by Home Assistant. Uses HA frontend auth to call HA services (which relay to OpenClaw gateway).</div>

  <div class=\"card\" id=\"statusCard\">
    <div class=\"row\">
      <div><b>Status:</b> <span id=\"status\">checking…</span></div>
      <button class=\"btn\" id=\"refreshBtn\">Refresh entities</button>
    </div>
    <div class=\"muted\" id=\"statusDetail\"></div>
  </div>

  <div class=\"card\">
    <h2>Chat (MVP)</h2>
    <div class=\"muted\">Calls HA service <code>clawdbot.send_chat</code>.</div>
    <div class=\"row\" style=\"margin-top:8px\">
      <input id=\"chatInput\" style=\"flex:1;min-width:240px\" placeholder=\"Type message…\"/>
      <button class=\"btn primary\" id=\"chatSend\">Send</button>
    </div>
    <div class=\"muted\" id=\"chatResult\" style=\"margin-top:8px\"></div>
  </div>

  <div class=\"card\">
    <h2>Entities (local HA)</h2>
    <div class=\"muted\">Lists entities from HA frontend state; controls call <code>clawdbot.ha_call_service</code>.</div>
    <div class=\"row\" style=\"margin-top:8px\">
      <input id=\"filter\" style=\"flex:1;min-width:240px\" placeholder=\"Filter (e.g. input_boolean, switch., light.kitchen)…\"/>
      <button class=\"btn\" id=\"clearFilter\">Clear</button>
    </div>
    <div class=\"entities\" id=\"entities\" style=\"margin-top:10px\"></div>
  </div>

<script>
(function(){
  function qs(sel){ return document.querySelector(sel); }

  async function getHass(){
    const parent = window.parent;
    if (!parent) throw new Error('No parent window');
    if (parent.hassConnection && parent.hassConnection.then) {
      const conn = await parent.hassConnection;
      if (conn && conn.conn) return { conn: conn.conn, hass: conn.hass };
    }
    if (parent.hass && parent.hass.connection) return { conn: parent.hass.connection, hass: parent.hass };
    throw new Error('Unable to access Home Assistant frontend connection from iframe');
  }

  function setStatus(ok, text, detail){
    const el = qs('#status');
    el.textContent = text;
    el.className = ok ? 'ok' : 'bad';
    qs('#statusDetail').textContent = detail || '';
  }

  async function callService(domain, service, data){
    const { conn } = await getHass();
    return conn.callService(domain, service, data || {});
  }

  let _allIds = [];

  function renderEntities(hass, filter){
    const states = hass && hass.states ? hass.states : {};
    const root = qs('#entities');
    root.innerHTML = '';

    const f = (filter || '').trim().toLowerCase();
    const ids = (f ? _allIds.filter(id => id.toLowerCase().includes(f)) : _allIds);

    for (const id of ids){
      const st = states[id];
      const row = document.createElement('div');
      row.className = 'ent';

      const left = document.createElement('div');
      left.style.minWidth = '280px';
      left.innerHTML = `<div class="ent-id">${id}</div><div class="ent-state">${st.state}</div>`;

      const right = document.createElement('div');
      right.className = 'row';

      const domain = id.split('.')[0];
      if (['switch','light','input_boolean'].includes(domain)){
        const onBtn = document.createElement('button');
        onBtn.className = 'btn';
        onBtn.textContent = 'On';
        onBtn.onclick = async () => { await callService('clawdbot','ha_call_service',{domain, service:'turn_on', entity_id:id, service_data:{}}); };

        const offBtn = document.createElement('button');
        offBtn.className = 'btn';
        offBtn.textContent = 'Off';
        offBtn.onclick = async () => { await callService('clawdbot','ha_call_service',{domain, service:'turn_off', entity_id:id, service_data:{}}); };

        right.appendChild(onBtn);
        right.appendChild(offBtn);
      } else {
        const noop = document.createElement('span');
        noop.className = 'muted';
        noop.textContent = 'no controls';
        right.appendChild(noop);
      }

      row.appendChild(left);
      row.appendChild(right);
      root.appendChild(row);
    }

    setStatus(true, 'connected', `Loaded ${ids.length} entities (filter: ${f || 'none'})`);
  }

  async function refreshEntities(){
    const { hass } = await getHass();
    const states = hass && hass.states ? hass.states : {};
    _allIds = Object.keys(states).sort();
    renderEntities(hass, qs('#filter').value);
  }

  async function init(){
    try{
      await getHass();
      setStatus(true,'connected','');
      await refreshEntities();
    } catch(e){
      setStatus(false,'error', String(e));
    }
  }

  qs('#refreshBtn').onclick = refreshEntities;
  qs('#clearFilter').onclick = () => { qs('#filter').value=''; getHass().then(({hass})=>renderEntities(hass,'')); };
  qs('#filter').oninput = async () => { try{ const { hass } = await getHass(); renderEntities(hass, qs('#filter').value); } catch(e){} };

  qs('#chatSend').onclick = async () => {
    const msg = qs('#chatInput').value.trim();
    if (!msg) return;
    qs('#chatResult').textContent = 'sending…';
    try{
      await callService('clawdbot','send_chat',{message: msg});
      qs('#chatResult').textContent = 'sent';
      qs('#chatInput').value = '';
    } catch(e){
      qs('#chatResult').textContent = 'error: ' + String(e);
    }
  };

  init();
})();
</script>
</body>
</html>
"""


class ClawdbotPanelView(HomeAssistantView):
    url = PANEL_PATH
    name = "api:clawdbot:panel"
    # This route serves static HTML only (no secrets). It must be embeddable in HA's iframe panel.
    # HA frontend auth is not a cookie header, so iframe navigation would 401 if requires_auth=True.
    requires_auth = False

    async def get(self, request):
        from aiohttp import web

        return web.Response(text=PANEL_HTML, content_type="text/html")


async def async_setup(hass, config):
    conf = config.get(DOMAIN, {})
    # For MVP: always serve panel content from HA itself.
    # This avoids OpenClaw Control UI auth/device-identity and makes the iframe same-origin.
    panel_url = PANEL_PATH

    # (Future) If we re-enable external panel overrides, validate here.
    title = conf.get("title", DEFAULT_TITLE)
    icon = conf.get("icon", DEFAULT_ICON)

    token = conf.get(CONF_TOKEN)
    session_key = conf.get(CONF_SESSION_KEY, DEFAULT_SESSION_KEY)

    # Panel URL is for the browser iframe. Gateway URL is for HA->Clawdbot service calls.
    gateway_origin = str(conf.get(CONF_GATEWAY_URL, _derive_gateway_origin(panel_url))).rstrip("/")
    session = aiohttp.ClientSession()

    # HTTP view (served by HA)
    try:
        hass.http.register_view(ClawdbotPanelView)
        _LOGGER.info("Registered Clawdbot panel view → %s", PANEL_PATH)
    except Exception:
        _LOGGER.exception("Failed to register Clawdbot panel view")

    # Panel (iframe)
    try:
        from homeassistant.components.frontend import async_register_built_in_panel

        async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=title,
            sidebar_icon=icon,
            frontend_url_path=DOMAIN,
            config={"url": panel_url},
            require_admin=True,
        )
        _LOGGER.info("Registered Clawdbot iframe panel → %s", panel_url)
    except Exception:
        _LOGGER.exception("Failed to register Clawdbot panel")

    # Ensure we close the aiohttp session
    async def _close_session(_evt):
        await session.close()

    hass.bus.async_listen_once("homeassistant_stop", _close_session)

    async def _notify(title: str, message: str) -> None:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": title, "message": message[:4000]},
            blocking=False,
        )

    # Services
    async def handle_send_chat(call):
        if not token:
            raise RuntimeError("clawdbot.token is required to use services")
        message = call.data.get("message")
        if not message:
            raise RuntimeError("message is required")

        # STRATEGY SHIFT: Use native 'message' tool directly.
        # This bypasses session routing and sends directly to the configured channel.
        # session_key is repurposed as the target channel ID for this mode.
        target_channel = session_key 

        payload = {
            "tool": "message",
            "args": {
                "action": "send",
                "channel": "discord",
                "channelId": target_channel, 
                "message": f"[Home Assistant] {message}",
            },
        }
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        await _notify("Clawdbot: send_chat", str(res))

    async def handle_tools_invoke(call):
        if not token:
            raise RuntimeError("clawdbot.token is required to use services")
        tool = call.data.get("tool")
        args = call.data.get("args", {})
        if not tool:
            raise RuntimeError("tool is required")
        if not isinstance(args, dict):
            raise RuntimeError("args must be an object")

        payload = {"tool": str(tool), "args": args}
        res = await _gw_post(session, gateway_origin + "/tools/invoke", token, payload)
        await _notify(f"Clawdbot: {tool}", str(res))

    async def handle_ha_get_states(call):
        """Return current HA entity states.

        Note: This runs locally inside HA (no Clawdbot gateway calls) because the
        gateway /tools/invoke endpoint does not expose a generic exec tool.
        """
        items = []
        for st in hass.states.async_all():
            items.append({
                "entity_id": st.entity_id,
                "state": st.state,
                "attributes": dict(st.attributes),
                "last_changed": st.last_changed.isoformat() if st.last_changed else None,
                "last_updated": st.last_updated.isoformat() if st.last_updated else None,
            })
        await _notify("Clawdbot: ha_get_states", __import__("json").dumps(items, indent=2))

    async def handle_ha_call_service(call):
        """Call a HA service locally."""
        domain = call.data.get("domain")
        service_name = call.data.get("service")
        entity_id = call.data.get("entity_id")
        service_data = call.data.get("service_data", {}) or {}
        if not domain or not service_name:
            raise RuntimeError("domain and service are required")
        if service_data and not isinstance(service_data, dict):
            raise RuntimeError("service_data must be an object")

        target = None
        if entity_id:
            target = {"entity_id": entity_id}

        await hass.services.async_call(
            str(domain),
            str(service_name),
            service_data,
            target=target,
            blocking=True,
        )
        await _notify("Clawdbot: ha_call_service", f"Called {domain}.{service_name} target={target} data={service_data}")

    hass.services.async_register(DOMAIN, SERVICE_SEND_CHAT, handle_send_chat)
    hass.services.async_register(DOMAIN, SERVICE_TOOLS_INVOKE, handle_tools_invoke)
    hass.services.async_register(DOMAIN, SERVICE_HA_GET_STATES, handle_ha_get_states)
    hass.services.async_register(DOMAIN, SERVICE_HA_CALL_SERVICE, handle_ha_call_service)

    _LOGGER.info(
        "Clawdbot services registered (%s.%s, %s.%s, %s.%s, %s.%s)",
        DOMAIN,
        SERVICE_SEND_CHAT,
        DOMAIN,
        SERVICE_TOOLS_INVOKE,
        DOMAIN,
        SERVICE_HA_GET_STATES,
        DOMAIN,
        SERVICE_HA_CALL_SERVICE,
    )

    return True
