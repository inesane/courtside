from __future__ import annotations

import logging
from typing import Any

from sports.base import GameState, PlayerStats, SportProvider
from sports.espn import ESPNClient

logger = logging.getLogger(__name__)

STAT_KEYS = [
    "minutes", "field_goals_made", "field_goals_attempted",
    "three_pointers_made", "three_pointers_attempted",
    "free_throws_made", "free_throws_attempted",
    "offensive_rebounds", "defensive_rebounds", "rebounds",
    "assists", "steals", "blocks", "turnovers", "personal_fouls",
    "plus_minus", "points",
]


class NCAABProvider(SportProvider):
    sport = "basketball"
    league = "mens-college-basketball"

    def __init__(self, client: ESPNClient) -> None:
        self.client = client

    async def get_games(self) -> list[GameState]:
        data = await self.client.get_scoreboard(self.sport, self.league)
        games: list[GameState] = []

        for event in data.get("events", []):
            try:
                game = self._parse_event(event)
                games.append(game)
            except Exception:
                logger.exception("Failed to parse event %s", event.get("id"))

        return games

    async def enrich_box_score(self, game: GameState) -> GameState:
        data = await self.client.get_summary(self.sport, self.league, game.game_id)
        game.players = self._parse_box_score(data)
        return game

    def _parse_event(self, event: dict[str, Any]) -> GameState:
        competition = event["competitions"][0]
        status = event["status"]

        competitors = {
            c["homeAway"]: c for c in competition["competitors"]
        }
        home = competitors["home"]
        away = competitors["away"]

        status_name = status["type"]["name"]
        if status_name == "STATUS_IN_PROGRESS":
            game_status = "in_progress"
        elif status_name == "STATUS_FINAL":
            game_status = "final"
        else:
            game_status = "scheduled"

        # Parse seed if available (for March Madness upset detection)
        home_seed = home.get("curatedRank", {}).get("current", 0)
        away_seed = away.get("curatedRank", {}).get("current", 0)

        game = GameState(
            game_id=event["id"],
            sport_key="ncaab",
            status=game_status,
            home_team=home["team"]["displayName"],
            away_team=away["team"]["displayName"],
            home_abbrev=home["team"]["abbreviation"],
            away_abbrev=away["team"]["abbreviation"],
            home_score=int(home.get("score", 0)),
            away_score=int(away.get("score", 0)),
            period=status.get("period", 0),
            clock=status.get("displayClock", ""),
            detail=status["type"].get("shortDetail", ""),
            start_time=event.get("date", ""),
        )
        # Store seeds in extra data for upset detection
        game._home_seed = home_seed
        game._away_seed = away_seed
        return game

    def _parse_box_score(self, data: dict[str, Any]) -> list[PlayerStats]:
        players: list[PlayerStats] = []

        for team_data in data.get("boxscore", {}).get("players", []):
            team_name = team_data["team"]["displayName"]

            for stat_group in team_data.get("statistics", []):
                labels = [label.lower() for label in stat_group.get("labels", [])]

                for athlete in stat_group.get("athletes", []):
                    name = athlete["athlete"]["displayName"]
                    athlete_id = str(athlete["athlete"].get("id", ""))
                    raw_stats = athlete.get("stats", [])

                    stats: dict[str, Any] = {}
                    for i, val in enumerate(raw_stats):
                        if i < len(labels):
                            key = labels[i]
                        elif i < len(STAT_KEYS):
                            key = STAT_KEYS[i]
                        else:
                            continue

                        try:
                            if "-" in str(val) and key != "plus_minus":
                                parts = val.split("-")
                                stats[key] = int(parts[0])
                            else:
                                stats[key] = int(val)
                        except (ValueError, TypeError):
                            stats[key] = val

                    players.append(PlayerStats(
                        player_name=name,
                        team=team_name,
                        espn_id=athlete_id,
                        stats=stats,
                    ))

        return players
