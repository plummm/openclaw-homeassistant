"""API client for the ha_agent_core service."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError


class HAAgentApi:
    """A thin async client for ha_agent_core."""

    def __init__(
        self,
        base_url: str,
        session: aiohttp.ClientSession,
        auth_key: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.set_base_url(base_url)
        self.set_auth_key(auth_key)

    def set_base_url(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def set_auth_key(self, auth_key: str | None) -> None:
        self._auth_key = auth_key

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {}
        if self._auth_key:
            headers["Authorization"] = f"Bearer {self._auth_key}"
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_data,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    raise HomeAssistantError(
                        f"Home Assistant Agent error {resp.status}: {data}"
                    )
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                "Error communicating with Home Assistant Agent"
            ) from err

    async def async_chat(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        history_limit: int | None = None,
        use_llm: bool | None = None,
        journal_names: list[str] | None = None,
        api_key: str | None = None,
        model: str | None = None,
        default_reply: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if history_limit is not None:
            payload["history_limit"] = history_limit
        if use_llm is not None:
            payload["use_llm"] = use_llm
        if journal_names is not None:
            payload["journal_names"] = journal_names
        if api_key:
            payload["api_key"] = api_key
        if model:
            payload["model"] = model
        if default_reply:
            payload["default_reply"] = default_reply
        return await self._request("POST", "/chat", json_data=payload)

    async def async_journals(self) -> dict[str, Any]:
        return await self._request("GET", "/journals")

    async def async_get_journal(self, name: str) -> dict[str, Any]:
        return await self._request("GET", "/journal", params={"name": name})

    async def async_put_journal(
        self,
        name: str,
        content: str,
        *,
        mode: str = "replace",
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "content": content,
            "mode": mode,
        }
        if source:
            payload["source"] = source
        if metadata:
            payload["metadata"] = metadata
        return await self._request("PUT", "/journal", json_data=payload)

    async def async_get_journal_entries(
        self,
        name: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await self._request("GET", "/journal/entries", params=params)

    async def async_memory_write(
        self,
        kind: str,
        content: str,
        *,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": kind, "content": content}
        if source:
            payload["source"] = source
        if metadata:
            payload["metadata"] = metadata
        return await self._request("POST", "/memory/write", json_data=payload)

    async def async_memory_query(
        self,
        kind: str,
        text: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"kind": kind, "text": text}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await self._request("GET", "/memory/query", params=params)

    async def async_entity_suggest(
        self,
        entities: list[dict[str, Any]],
        *,
        use_llm: bool | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"entities": entities}
        if use_llm is not None:
            payload["use_llm"] = use_llm
        if api_key:
            payload["api_key"] = api_key
        if model:
            payload["model"] = model
        return await self._request("POST", "/entity/suggest", json_data=payload)

    async def async_health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def async_logs(self) -> dict[str, Any]:
        return await self._request("GET", "/logs")

    async def async_root(self) -> dict[str, Any]:
        return await self._request("GET", "/")
