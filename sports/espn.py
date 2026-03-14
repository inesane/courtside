from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"


class ESPNClient:
    """Shared ESPN API client for all sports."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_scoreboard(self, sport: str, league: str) -> dict[str, Any]:
        url = f"{BASE_URL}/{sport}/{league}/scoreboard"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def get_summary(self, sport: str, league: str, event_id: str) -> dict[str, Any]:
        url = f"{BASE_URL}/{sport}/{league}/summary"
        resp = await self._client.get(url, params={"event": event_id})
        resp.raise_for_status()
        return resp.json()
