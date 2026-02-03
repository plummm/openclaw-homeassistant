"""Storage helper for Home Assistant Agent settings."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_BASE_URL,
)

STORAGE_KEY = "home_assistant_agent"
STORAGE_VERSION = 1


class HAAgentStorage:
    """Persist settings without reloading config entries."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._cache: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = await self._store.async_load() or {}
        return self._cache

    async def async_get_entry(self, entry_id: str) -> dict[str, Any]:
        data = await self.async_load()
        entries = data.setdefault("entries", {})
        entry = entries.get(entry_id) or {}
        return {
            "base_url": entry.get("base_url", DEFAULT_BASE_URL),
        }

    async def async_entry_exists(self, entry_id: str) -> bool:
        data = await self.async_load()
        return entry_id in data.get("entries", {})

    async def async_get_entry_raw(self, entry_id: str) -> dict[str, Any]:
        data = await self.async_load()
        return (data.get("entries", {}) or {}).get(entry_id, {})

    async def async_set_entry(
        self, entry_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        data = await self.async_load()
        entries = data.setdefault("entries", {})
        entry = entries.get(entry_id, {})
        entry.update({k: v for k, v in updates.items() if v is not None})
        entries[entry_id] = entry
        await self._store.async_save(data)
        return {
            "base_url": entry.get("base_url", DEFAULT_BASE_URL),
        }
