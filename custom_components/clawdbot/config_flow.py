from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries

# Reuse constants defined in __init__.py to minimize refactor.
from . import (
    DOMAIN,
    CONF_GATEWAY_URL,
    CONF_TOKEN,
    CONF_SESSION_KEY,
    CONF_PANEL_URL,
    DEFAULT_SESSION_KEY,
)


class ClawdbotConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for OpenClaw (clawdbot)."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            gw = (user_input.get(CONF_GATEWAY_URL) or "").strip().rstrip("/")
            tok = (user_input.get(CONF_TOKEN) or "").strip()
            sk = (user_input.get(CONF_SESSION_KEY) or DEFAULT_SESSION_KEY).strip() or DEFAULT_SESSION_KEY
            panel_url = (user_input.get(CONF_PANEL_URL) or "").strip()

            # This integration uses HTTP(S) REST calls to the OpenClaw Gateway.
            # ws:// is a common mistake (that's for websocket endpoints).
            if gw.startswith("ws://") or gw.startswith("wss://"):
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_GATEWAY_URL, default=gw): str,
                            vol.Required(CONF_TOKEN, default=tok): str,
                            vol.Optional(CONF_SESSION_KEY, default=sk): str,
                            vol.Optional(CONF_PANEL_URL, default=panel_url): str,
                        }
                    ),
                    errors={CONF_GATEWAY_URL: "invalid_url"},
                    description_placeholders={
                        "hint": "Use http(s)://HOST:7773 (not ws://).",
                    },
                )

            data = {
                CONF_GATEWAY_URL: gw,
                CONF_TOKEN: tok,
                CONF_SESSION_KEY: sk,
            }
            if panel_url:
                data[CONF_PANEL_URL] = panel_url

            return self.async_create_entry(title="OpenClaw", data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_GATEWAY_URL): str,
                vol.Required(CONF_TOKEN): str,
                vol.Optional(CONF_SESSION_KEY, default=DEFAULT_SESSION_KEY): str,
                vol.Optional(CONF_PANEL_URL): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
