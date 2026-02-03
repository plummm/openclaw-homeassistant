"""Home Assistant Agent integration setup."""

from __future__ import annotations

from pathlib import Path
import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components import panel_custom
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .api import HAAgentApi
from .conversation import (
    HAAgentConversationAgent,
    async_register_agent,
    async_set_default_agent,
    async_unregister_agent,
)
from .const import (
    CONF_BASE_URL,
    CONF_SET_DEFAULT_AGENT,
    DEFAULT_BASE_URL,
    DEFAULT_INSTRUCTION,
    DOMAIN,
    PANEL_COMPONENT_NAME,
    PANEL_FRONTEND_URL,
    PANEL_ICON,
    PANEL_MODULE_URL,
    PANEL_TITLE,
)
from .storage import HAAgentStorage

_LOGGER = logging.getLogger(__name__)

PANEL_FILE_PATH = Path(__file__).parent / "panel" / "home-assistant-agent-panel.js"
PANEL_STATIC_URL = "/home_assistant_agent_panel/home-assistant-agent-panel.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(
        DOMAIN,
        {
            "entries": {},
            "panel_registered": False,
            "views_registered": False,
            "storage": HAAgentStorage(hass),
        },
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.setdefault(
        DOMAIN,
        {
            "entries": {},
            "panel_registered": False,
            "views_registered": False,
            "storage": HAAgentStorage(hass),
        },
    )

    session = aiohttp_client.async_get_clientsession(hass)
    storage: HAAgentStorage = domain_data["storage"]
    if not await storage.async_entry_exists(entry.entry_id):
        seed: dict[str, Any] = {}
        base_url = entry.data.get(CONF_BASE_URL)
        if base_url and base_url != DEFAULT_BASE_URL:
            seed["base_url"] = base_url
        await storage.async_set_entry(entry.entry_id, seed)
    settings = await storage.async_get_entry(entry.entry_id)
    client = HAAgentApi(settings.get("base_url", DEFAULT_BASE_URL), session)
    agent = HAAgentConversationAgent(hass, entry.entry_id)
    domain_data["entries"][entry.entry_id] = {
        "client": client,
        "entry": entry,
        "agent": agent,
        "settings": settings,
        "addon_config": None,
        "addon_config_ts": 0.0,
    }
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await async_register_agent(hass, entry, agent)

    if entry.options.get(CONF_SET_DEFAULT_AGENT):
        await async_set_default_agent(hass, agent)

    if not domain_data["panel_registered"]:
        await _async_register_panel(hass)
        domain_data["panel_registered"] = True

    if not domain_data["views_registered"]:
        _register_views(hass)
        domain_data["views_registered"] = True

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.get(DOMAIN, {})
    entry_data = domain_data.get("entries", {}).pop(entry.entry_id, None)
    if entry_data and entry_data.get("agent"):
        await async_unregister_agent(hass, entry, entry_data["agent"])

    if not hass.config_entries.async_entries(DOMAIN):
        if domain_data.get("panel_registered"):
            await _async_unregister_panel(hass)
            domain_data["panel_registered"] = False
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    domain_data = hass.data.get(DOMAIN, {})
    entry_data = domain_data.get("entries", {}).get(entry.entry_id)
    if not entry_data:
        return
    storage: HAAgentStorage = domain_data.get("storage")
    if storage:
        settings = await storage.async_get_entry(entry.entry_id)
        entry_data["settings"] = settings
        entry_data["client"].set_base_url(settings.get("base_url", DEFAULT_BASE_URL))
    if entry.options.get(CONF_SET_DEFAULT_AGENT):
        await async_set_default_agent(hass, entry_data["agent"])


async def _async_register_panel(hass: HomeAssistant) -> None:
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_STATIC_URL, str(PANEL_FILE_PATH), False)]
    )
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_COMPONENT_NAME,
        frontend_url_path=PANEL_FRONTEND_URL,
        module_url=PANEL_MODULE_URL,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        config={},
        require_admin=False,
    )


async def _async_unregister_panel(hass: HomeAssistant) -> None:
    remove_fn = getattr(panel_custom, "async_remove_panel", None)
    if remove_fn is None:
        remove_fn = getattr(panel_custom, "async_unregister_panel", None)
    if remove_fn is not None:
        await remove_fn(hass, PANEL_FRONTEND_URL)


def _register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(HAAgentEntitiesView())
    hass.http.register_view(HAAgentLLMKeyView())
    hass.http.register_view(HAAgentSettingsView())
    hass.http.register_view(HAAgentSuggestView())
    hass.http.register_view(HAAgentHealthView())


def _get_entry_and_client(
    hass: HomeAssistant, entry_id: str | None
) -> tuple[ConfigEntry | None, HAAgentApi | None]:
    entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
    else:
        entry = entries[0] if entries else None
    if not entry:
        return None, None
    entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry.entry_id)
    if not entry_data:
        return entry, None
    return entry, entry_data["client"]


async def _update_settings(
    hass: HomeAssistant, entry: ConfigEntry, updates: dict[str, Any]
) -> dict[str, Any]:
    domain_data = hass.data.get(DOMAIN, {})
    storage: HAAgentStorage = domain_data.get("storage")
    if not storage:
        return {}
    filtered = {}
    if "base_url" in updates:
        filtered["base_url"] = updates.get("base_url")
    if not filtered:
        return await storage.async_get_entry(entry.entry_id)
    settings = await storage.async_set_entry(entry.entry_id, filtered)
    entry_data = domain_data.get("entries", {}).get(entry.entry_id)
    if entry_data:
        entry_data["settings"] = settings
        if "base_url" in updates:
            entry_data["client"].set_base_url(settings.get("base_url", DEFAULT_BASE_URL))
    return settings


@dataclass
class AddonConfig:
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    enable_web_search: bool | None = None
    model_reasoning: str | None = None
    model_fast: str | None = None
    tts_model: str | None = None
    stt_model: str | None = None
    instruction: str | None = None
    api_keys_present: dict[str, bool] | None = None
    db_path: str | None = None


async def _fetch_addon_config(
    hass: HomeAssistant, entry: ConfigEntry, *, force: bool = False
) -> AddonConfig | None:
    entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry.entry_id)
    if not entry_data:
        return None
    now = asyncio.get_running_loop().time()
    cached = entry_data.get("addon_config")
    cached_ts = float(entry_data.get("addon_config_ts") or 0.0)
    if cached and not force and (now - cached_ts) < 15:
        return cached

    base_url = entry_data.get("settings", {}).get("base_url", DEFAULT_BASE_URL)
    session = aiohttp_client.async_get_clientsession(hass)
    url = f"{base_url.rstrip('/')}/config"
    try:
        async with session.get(url, timeout=15) as resp:
            payload = await resp.json()
    except Exception as exc:  # noqa: BLE001
        entry_data["addon_config_ts"] = now
        entry_data["addon_config"] = None
        _LOGGER.warning("Failed to fetch add-on config: %s", exc)
        return None

    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        entry_data["addon_config_ts"] = now
        entry_data["addon_config"] = None
        return None

    api_keys = config.get("api_keys") if isinstance(config.get("api_keys"), dict) else {}
    parsed = AddonConfig(
        model=config.get("model"),
        temperature=config.get("temperature"),
        max_output_tokens=config.get("max_output_tokens"),
        enable_web_search=config.get("enable_web_search"),
        model_reasoning=config.get("model_reasoning"),
        model_fast=config.get("model_fast"),
        tts_model=config.get("tts_model"),
        stt_model=config.get("stt_model"),
        instruction=config.get("instruction"),
        api_keys_present={
            "openai_api_key": bool(api_keys.get("openai_api_key")),
            "anthropic_api_key": bool(api_keys.get("anthropic_api_key")),
            "google_api_key": bool(api_keys.get("google_api_key")),
        },
        db_path=config.get("db_path"),
    )
    entry_data["addon_config"] = parsed
    entry_data["addon_config_ts"] = now
    return parsed


def _build_entity_payload(hass: HomeAssistant) -> list[dict[str, Any]]:
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    entities: list[dict[str, Any]] = []

    for entry in entity_reg.entities.values():
        device = device_reg.devices.get(entry.device_id) if entry.device_id else None
        area_id = entry.area_id or (device.area_id if device else None)
        area = area_reg.areas.get(area_id) if area_id else None
        state = hass.states.get(entry.entity_id)
        name = (
            entry.name
            or entry.original_name
            or (state.attributes.get("friendly_name") if state else None)
            or entry.entity_id
        )
        device_class = getattr(entry, "device_class", None) or (
            state.attributes.get("device_class") if state else None
        )
        unit = getattr(entry, "unit_of_measurement", None) or (
            state.attributes.get("unit_of_measurement") if state else None
        )

        entities.append(
            {
                "entity_id": entry.entity_id,
                "name": name,
                "device_class": device_class,
                "unit": unit,
                "area": area.name if area else None,
                "device": device.name_by_user or device.name if device else None,
            }
        )

    return entities


class HAAgentEntitiesView(HomeAssistantView):
    """Return registry data shaped for /entity/suggest."""

    url = "/api/home_assistant_agent/entities"
    name = "api:home_assistant_agent:entities"
    requires_auth = True

    async def get(self, request):
        hass: HomeAssistant = request.app["hass"]
        entities = _build_entity_payload(hass)
        return self.json({"entities": entities})


class HAAgentLLMKeyView(HomeAssistantView):
    """Store an LLM API key in HA storage."""

    url = "/api/home_assistant_agent/llm_key"
    name = "api:home_assistant_agent:llm_key"
    requires_auth = True

    async def post(self, request):
        hass: HomeAssistant = request.app["hass"]
        payload = await request.json()
        llm_key = payload.get("llm_key", "")
        entry_id = payload.get("entry_id")
        entry, _client = _get_entry_and_client(hass, entry_id)
        if not entry:
            return self.json({"error": "No config entry found"}, status_code=400)
        entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry.entry_id, {})
        base_url = entry_data.get("settings", {}).get("base_url", DEFAULT_BASE_URL)
        session = aiohttp_client.async_get_clientsession(hass)
        url = f"{base_url.rstrip('/')}/config"
        body = {"openai_api_key": llm_key}
        try:
            async with session.put(url, json=body, timeout=15) as resp:
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            return self.json({"error": f"Config update failed: {exc}"}, status_code=500)
        return self.json(
            {"status": "ok", "openai_key_present": bool((data or {}).get("config", {}).get("api_keys", {}).get("openai_api_key"))}
        )


class HAAgentSettingsView(HomeAssistantView):
    """Get or update stored settings without reloading the entry."""

    url = "/api/home_assistant_agent/settings"
    name = "api:home_assistant_agent:settings"
    requires_auth = True

    async def get(self, request):
        hass: HomeAssistant = request.app["hass"]
        entry_id = request.query.get("entry_id")
        entry, _client = _get_entry_and_client(hass, entry_id)
        if not entry:
            return self.json({"error": "No config entry found"}, status_code=400)
        entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry.entry_id, {})
        settings = entry_data.get("settings", {})
        addon_cfg = await _fetch_addon_config(hass, entry)
        return self.json(
            {
                "base_url": settings.get("base_url", DEFAULT_BASE_URL),
                "openai_key_present": bool((addon_cfg.api_keys_present or {}).get("openai_api_key")) if addon_cfg else False,
                "anthropic_key_present": bool((addon_cfg.api_keys_present or {}).get("anthropic_api_key")) if addon_cfg else False,
                "gemini_key_present": bool((addon_cfg.api_keys_present or {}).get("google_api_key")) if addon_cfg else False,
                "model_reasoning": addon_cfg.model_reasoning if addon_cfg and addon_cfg.model_reasoning else "",
                "model_fast": addon_cfg.model_fast if addon_cfg and addon_cfg.model_fast else "",
                "tts_model": addon_cfg.tts_model if addon_cfg and addon_cfg.tts_model else "",
                "stt_model": addon_cfg.stt_model if addon_cfg and addon_cfg.stt_model else "",
                "instruction": addon_cfg.instruction if addon_cfg and addon_cfg.instruction else DEFAULT_INSTRUCTION,
            }
        )

    async def post(self, request):
        hass: HomeAssistant = request.app["hass"]
        payload = await request.json()
        entry_id = payload.get("entry_id")
        entry, _client = _get_entry_and_client(hass, entry_id)
        if not entry:
            return self.json({"error": "No config entry found"}, status_code=400)
        updates: dict[str, Any] = {}
        addon_updates: dict[str, Any] = {}
        if "base_url" in payload:
            updates["base_url"] = payload.get("base_url")
        if "openai_key" in payload:
            addon_updates["openai_api_key"] = payload.get("openai_key")
        if "anthropic_key" in payload:
            addon_updates["anthropic_api_key"] = payload.get("anthropic_key")
        if "gemini_key" in payload:
            addon_updates["google_api_key"] = payload.get("gemini_key")
        if "model_reasoning" in payload:
            addon_updates["model_reasoning"] = payload.get("model_reasoning")
        if "model_fast" in payload:
            addon_updates["model_fast"] = payload.get("model_fast")
        if "tts_model" in payload:
            addon_updates["tts_model"] = payload.get("tts_model")
        if "stt_model" in payload:
            addon_updates["stt_model"] = payload.get("stt_model")
        if "instruction" in payload:
            addon_updates["instruction"] = payload.get("instruction")

        settings = await _update_settings(hass, entry, updates)
        entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry.entry_id, {})
        base_url = settings.get("base_url", DEFAULT_BASE_URL)
        addon_cfg = None
        if addon_updates:
            session = aiohttp_client.async_get_clientsession(hass)
            url = f"{base_url.rstrip('/')}/config"
            try:
                async with session.put(url, json=addon_updates, timeout=20) as resp:
                    data = await resp.json()
            except Exception as exc:  # noqa: BLE001
                return self.json({"error": f"Config update failed: {exc}"}, status_code=500)
            if isinstance(data, dict) and isinstance(data.get("config"), dict):
                addon_cfg = data.get("config")
        if addon_cfg:
            entry_data["addon_config"] = AddonConfig(
                model=addon_cfg.get("model"),
                temperature=addon_cfg.get("temperature"),
                max_output_tokens=addon_cfg.get("max_output_tokens"),
                enable_web_search=addon_cfg.get("enable_web_search"),
                model_reasoning=addon_cfg.get("model_reasoning"),
                model_fast=addon_cfg.get("model_fast"),
                tts_model=addon_cfg.get("tts_model"),
                stt_model=addon_cfg.get("stt_model"),
                instruction=addon_cfg.get("instruction"),
                api_keys_present=addon_cfg.get("api_keys"),
                db_path=addon_cfg.get("db_path"),
            )
            entry_data["addon_config_ts"] = asyncio.get_running_loop().time()
        elif not addon_updates:
            addon_cfg_obj = await _fetch_addon_config(hass, entry, force=True)
            if addon_cfg_obj:
                addon_cfg = {
                    "model_reasoning": addon_cfg_obj.model_reasoning,
                    "model_fast": addon_cfg_obj.model_fast,
                    "tts_model": addon_cfg_obj.tts_model,
                    "stt_model": addon_cfg_obj.stt_model,
                    "instruction": addon_cfg_obj.instruction,
                    "api_keys": addon_cfg_obj.api_keys_present,
                }
        return self.json(
            {
                "status": "ok",
                "base_url": settings.get("base_url", DEFAULT_BASE_URL),
                "openai_key_present": bool((addon_cfg or {}).get("api_keys", {}).get("openai_api_key")) if addon_cfg else False,
                "anthropic_key_present": bool((addon_cfg or {}).get("api_keys", {}).get("anthropic_api_key")) if addon_cfg else False,
                "gemini_key_present": bool((addon_cfg or {}).get("api_keys", {}).get("google_api_key")) if addon_cfg else False,
                "model_reasoning": addon_cfg.get("model_reasoning", "") if addon_cfg else "",
                "model_fast": addon_cfg.get("model_fast", "") if addon_cfg else "",
                "tts_model": addon_cfg.get("tts_model", "") if addon_cfg else "",
                "stt_model": addon_cfg.get("stt_model", "") if addon_cfg else "",
                "instruction": addon_cfg.get("instruction", DEFAULT_INSTRUCTION) if addon_cfg else DEFAULT_INSTRUCTION,
                "validation": None,
            }
        )


class HAAgentSuggestView(HomeAssistantView):
    """Proxy /entity/suggest to ha_agent_core."""

    url = "/api/home_assistant_agent/suggest"
    name = "api:home_assistant_agent:suggest"
    requires_auth = True

    async def post(self, request):
        hass: HomeAssistant = request.app["hass"]
        payload = await request.json()
        entry_id = payload.get("entry_id")
        entry, client = _get_entry_and_client(hass, entry_id)
        if not entry or not client:
            return self.json({"error": "No config entry found"}, status_code=400)

        model = payload.get("model")
        if not model:
            addon_cfg = await _fetch_addon_config(hass, entry)
            if addon_cfg:
                model = addon_cfg.model_reasoning or addon_cfg.model_fast
        llm_key = payload.get("llm_key")
        entities = payload.get("entities") or _build_entity_payload(hass)
        result = await client.async_entity_suggest(
            entities=entities,
            use_llm=payload.get("use_llm"),
            api_key=llm_key if llm_key else None,
            model=model,
        )
        return self.json(result)


class HAAgentHealthView(HomeAssistantView):
    """Check add-on config endpoint availability."""

    url = "/api/home_assistant_agent/health"
    name = "api:home_assistant_agent:health"
    requires_auth = True

    async def get(self, request):
        hass: HomeAssistant = request.app["hass"]
        entry_id = request.query.get("entry_id")
        entry, _client = _get_entry_and_client(hass, entry_id)
        if not entry:
            return self.json({"status": "error", "error": "No config entry found"}, status_code=400)

        entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry.entry_id, {})
        settings = entry_data.get("settings", {})
        base_url = settings.get("base_url", DEFAULT_BASE_URL)
        session = aiohttp_client.async_get_clientsession(hass)
        url = f"{base_url.rstrip('/')}/config"
        try:
            async with session.get(url, timeout=10) as resp:
                payload = await resp.json()
        except Exception as exc:  # noqa: BLE001
            return self.json({"status": "error", "error": str(exc)})

        if not isinstance(payload, dict) or payload.get("status") != "success":
            return self.json({"status": "error", "error": "Invalid response from add-on"})

        return self.json({"status": "success"})
