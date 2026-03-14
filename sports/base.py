from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class GameState:
    """Sport-agnostic game state."""

    game_id: str
    status: str  # "scheduled", "in_progress", "final"
    home_team: str
    away_team: str
    home_abbrev: str
    away_abbrev: str
    home_score: int
    away_score: int
    period: int
    clock: str
    detail: str  # e.g., "4th 2:30"
    start_time: str = ""  # ISO 8601 UTC, e.g. "2026-03-15T17:00Z"
    players: list[PlayerStats] = field(default_factory=list)

    @property
    def clock_seconds(self) -> float:
        """Parse clock string like '4:30' or '30.2' into total seconds."""
        try:
            parts = self.clock.replace(" ", "").split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(parts[0])
        except (ValueError, IndexError):
            return 0.0

    @property
    def score_diff(self) -> int:
        return abs(self.home_score - self.away_score)

    @property
    def leading_team(self) -> str:
        if self.home_score > self.away_score:
            return self.home_team
        elif self.away_score > self.home_score:
            return self.away_team
        return "Tied"

    def score_line(self) -> str:
        return f"{self.away_team} {self.away_score} @ {self.home_team} {self.home_score}"


@dataclass
class PlayerStats:
    """Sport-agnostic player stat line."""

    player_name: str
    team: str
    stats: dict[str, Any] = field(default_factory=dict)


class SportProvider(ABC):
    """Fetches and parses game data for a specific sport."""

    sport: str
    league: str

    @abstractmethod
    async def get_games(self) -> list[GameState]:
        """Get all games for today with current scores."""
        ...

    @abstractmethod
    async def enrich_box_score(self, game: GameState) -> GameState:
        """Fetch detailed box score and attach player stats to game."""
        ...
