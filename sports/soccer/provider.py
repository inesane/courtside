from __future__ import annotations

import logging
from typing import Any

from sports.base import GameState, PlayerStats, SportProvider
from sports.espn import ESPNClient

logger = logging.getLogger(__name__)


class SoccerProvider(SportProvider):
    sport = "soccer"

    def __init__(self, client: ESPNClient, league: str = "eng.1") -> None:
        self.client = client
        self.league = league

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
        # Parse goal events for hat trick / equalizer detection
        game._goal_events = self._parse_goal_events(data)
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

        # Soccer clock: ESPN provides the minute in displayClock
        return GameState(
            game_id=event["id"],
            sport_key=f"soccer_{self.league}",
            status=game_status,
            home_team=home["team"]["displayName"],
            away_team=away["team"]["displayName"],
            home_abbrev=home["team"].get("abbreviation", home["team"]["shortDisplayName"]),
            away_abbrev=away["team"].get("abbreviation", away["team"]["shortDisplayName"]),
            home_score=int(home.get("score", 0)),
            away_score=int(away.get("score", 0)),
            period=status.get("period", 0),
            clock=status.get("displayClock", ""),
            detail=status["type"].get("shortDetail", ""),
            start_time=event.get("date", ""),
        )

    def _parse_box_score(self, data: dict[str, Any]) -> list[PlayerStats]:
        players: list[PlayerStats] = []

        # Soccer box scores are structured differently
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
                        else:
                            continue

                        try:
                            stats[key] = int(val)
                        except (ValueError, TypeError):
                            try:
                                stats[key] = float(val)
                            except (ValueError, TypeError):
                                stats[key] = val

                    players.append(PlayerStats(
                        player_name=name,
                        team=team_name,
                        espn_id=athlete_id,
                        stats=stats,
                    ))

        return players

    def _parse_goal_events(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse goal events from the summary for hat trick / late goal detection."""
        goals: list[dict[str, Any]] = []

        # ESPN includes key events / scoring plays
        for play in data.get("scoringPlays", []):
            goals.append({
                "minute": play.get("clock", {}).get("displayValue", ""),
                "team": play.get("team", {}).get("displayName", ""),
                "player": play.get("text", ""),
                "period": play.get("period", {}).get("number", 0),
            })

        return goals
