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

    async def search_players(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search ESPN for players by name."""
        url = "https://site.api.espn.com/apis/common/v3/search"
        resp = await self._client.get(url, params={
            "query": query,
            "type": "player",
            "limit": limit,
        })
        resp.raise_for_status()
        data = resp.json()

        players: list[dict[str, Any]] = []
        for item in data.get("items", []):
            player: dict[str, Any] = {
                "name": item.get("displayName", ""),
                "espn_id": str(item.get("id", "")),
                "sport": item.get("sport", ""),
                "league": item.get("league", ""),
                "team": item.get("description", ""),
                "headshot_url": item.get("image", ""),
            }
            players.append(player)

        return players

    async def get_athlete_stats(self, sport: str, league: str, athlete_id: str) -> dict[str, Any]:
        """Fetch career stats for an athlete."""
        url = f"https://site.api.espn.com/apis/common/v3/sports/{sport}/{league}/athletes/{athlete_id}/stats"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.json()
