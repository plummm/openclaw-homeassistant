"""Conversation agent for Home Assistant Agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.intent import IntentResponse

from .const import DEFAULT_BASE_URL, DOMAIN


@dataclass
class AddonConfig:
    model_reasoning: str | None = None
    model_fast: str | None = None


async def _fetch_addon_config(hass: HomeAssistant, entry_id: str) -> AddonConfig | None:
    entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry_id, {})
    if not entry_data:
        return None
    now = asyncio.get_running_loop().time()
    cached = entry_data.get("addon_config")
    cached_ts = float(entry_data.get("addon_config_ts") or 0.0)
    if cached and (now - cached_ts) < 15:
        return cached

    base_url = entry_data.get("settings", {}).get("base_url", DEFAULT_BASE_URL)
    session = aiohttp_client.async_get_clientsession(hass)
    url = f"{base_url.rstrip('/')}/config"
    try:
        async with session.get(url, timeout=15) as resp:
            payload = await resp.json()
    except Exception:  # noqa: BLE001
        entry_data["addon_config_ts"] = now
        entry_data["addon_config"] = None
        return None

    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        entry_data["addon_config_ts"] = now
        entry_data["addon_config"] = None
        return None

    parsed = AddonConfig(
        model_reasoning=config.get("model_reasoning"),
        model_fast=config.get("model_fast"),
    )
    entry_data["addon_config"] = parsed
    entry_data["addon_config_ts"] = now
    return parsed


class HAAgentConversationAgent(AbstractConversationAgent):
    """Conversation agent that proxies to ha_agent_core."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._entry_id = entry_id

    @property
    def agent_id(self) -> str:
        return self._entry_id

    @property
    def name(self) -> str:
        return "Home Assistant Agent"

    @property
    def supported_languages(self) -> list[str]:
        return ["*"]

    @property
    def attribution(self) -> str:
        return "Powered by ha_agent_core"

    async def async_process(
        self, conversation_input: ConversationInput
    ) -> ConversationResult:
        entry_data = (
            self.hass.data.get(DOMAIN, {})
            .get("entries", {})
            .get(self._entry_id, {})
        )
        client = entry_data.get("client")
        addon_cfg = await _fetch_addon_config(self.hass, self._entry_id)
        model = addon_cfg.model_reasoning if addon_cfg else None
        if not model and addon_cfg:
            model = addon_cfg.model_fast

        response_text = "Sorry, I couldn't reach the agent."
        conversation_id = conversation_input.conversation_id
        if client:
            result: dict[str, Any] = await client.async_chat(
                conversation_input.text,
                conversation_id=conversation_id,
                use_llm=True,
                model=model,
            )
            response_text = result.get("response", response_text)
            conversation_id = result.get("conversation_id", conversation_id)

        intent_response = IntentResponse(language=conversation_input.language)
        intent_response.async_set_speech(response_text)
        return ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )


async def _maybe_await(result: Any) -> None:
    if asyncio.iscoroutine(result):
        await result


async def async_register_agent(
    hass: HomeAssistant, entry: ConfigEntry, agent: AbstractConversationAgent
) -> None:
    if hasattr(conversation, "async_set_agent"):
        try:
            result = conversation.async_set_agent(hass, entry, agent)
        except TypeError:
            try:
                result = conversation.async_set_agent(hass, agent)
            except TypeError:
                result = conversation.async_set_agent(hass, entry.entry_id, agent)
        await _maybe_await(result)


async def async_unregister_agent(
    hass: HomeAssistant, entry: ConfigEntry, agent: AbstractConversationAgent
) -> None:
    if hasattr(conversation, "async_unset_agent"):
        try:
            result = conversation.async_unset_agent(hass, entry)
        except TypeError:
            try:
                result = conversation.async_unset_agent(hass, agent)
            except TypeError:
                result = conversation.async_unset_agent(hass, entry.entry_id)
        await _maybe_await(result)


async def async_set_default_agent(
    hass: HomeAssistant, agent: AbstractConversationAgent
) -> None:
    if hasattr(conversation, "async_set_default_agent"):
        try:
            result = conversation.async_set_default_agent(hass, agent)
        except TypeError:
            result = conversation.async_set_default_agent(hass, agent.agent_id)
        await _maybe_await(result)
        return

    if hasattr(conversation, "async_set_default_agent_id"):
        result = conversation.async_set_default_agent_id(hass, agent.agent_id)
        await _maybe_await(result)
