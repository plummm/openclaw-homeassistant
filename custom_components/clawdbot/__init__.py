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

DOMAIN = "clawdbot"
_LOGGER = logging.getLogger(__name__)

DEFAULT_TITLE = "Clawdbot"
DEFAULT_ICON = "mdi:robot"
# NOTE: panel_url must be reachable from the *browser viewing Home Assistant*.
# Do NOT default to host.docker.internal (only works from inside Docker).
DEFAULT_URL = "http://127.0.0.1:7773/__clawdbot__/canvas/ha-panel/"
DEFAULT_SESSION_KEY = "main"

CONF_URL = "url"
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


async def async_setup(hass, config):
    conf = config.get(DOMAIN, {})
    panel_url = str(conf.get(CONF_URL, DEFAULT_URL)).rstrip("/")
    title = conf.get("title", DEFAULT_TITLE)
    icon = conf.get("icon", DEFAULT_ICON)

    token = conf.get(CONF_TOKEN)
    session_key = conf.get(CONF_SESSION_KEY, DEFAULT_SESSION_KEY)

    # Panel URL is for the browser iframe. Gateway URL is for HA->Clawdbot service calls.
    gateway_origin = str(conf.get(CONF_GATEWAY_URL, _derive_gateway_origin(panel_url))).rstrip("/")
    session = aiohttp.ClientSession()

    # Panel (iframe)
    try:
        from homeassistant.components.frontend import async_register_built_in_panel

        async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=title,
            sidebar_icon=icon,
            frontend_url_path=DOMAIN,
            config={"url": panel_url + "/"},
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
