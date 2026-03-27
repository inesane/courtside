from __future__ import annotations

from typing import Any, Optional

from alerts.base import Alert, AlertRule
from sports.base import GameState


class UpsetAlertRule(AlertRule):
    """Alert when a lower-seeded team is beating a higher seed late in the game."""

    name = "upset_alert"

    def __init__(self, seed_difference: int = 5) -> None:
        self.seed_difference = seed_difference

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []
        # Must be in 2nd half or OT
        if game.period < 2:
            return []

        home_seed = getattr(game, "_home_seed", 0)
        away_seed = getattr(game, "_away_seed", 0)

        if not home_seed or not away_seed:
            return []

        # Determine the underdog (higher seed number = lower ranked)
        if home_seed > away_seed and home_seed - away_seed >= self.seed_difference:
            # Home is the underdog
            if game.home_score > game.away_score:
                return [self._make_alert(game, game.home_team, home_seed, game.away_team, away_seed)]
        elif away_seed > home_seed and away_seed - home_seed >= self.seed_difference:
            # Away is the underdog
            if game.away_score > game.home_score:
                return [self._make_alert(game, game.away_team, away_seed, game.home_team, home_seed)]

        return []

    def _make_alert(self, game: GameState, underdog: str, underdog_seed: int, favorite: str, fav_seed: int) -> Alert:
        return Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"UPSET ALERT: #{underdog_seed} {underdog} leads #{fav_seed} {favorite}!",
            detail=f"{game.score_line()} | {game.detail}",
            priority="high",
            dedup_key=(game.game_id, self.name),
        )
