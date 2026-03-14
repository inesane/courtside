from __future__ import annotations

import logging
from typing import Optional

from alerts.base import Alert, AlertRule
from sports.base import GameState

logger = logging.getLogger(__name__)


class AlertEngine:
    """Evaluates alert rules against game states with deduplication."""

    def __init__(self, rules: list[AlertRule]) -> None:
        self.rules = rules
        self._fired: set[tuple] = set()
        self._prev_states: dict[str, GameState] = {}
        self._max_deficits: dict[str, int] = {}  # game_id -> max point deficit

    def evaluate(self, game: GameState) -> list[Alert]:
        prev = self._prev_states.get(game.game_id)
        alerts: list[Alert] = []

        # Track max deficit for comeback detection
        self._track_deficit(game)

        for rule in self.rules:
            try:
                rule_alerts = rule.evaluate(game, prev)
                for alert in rule_alerts:
                    if alert.dedup_key not in self._fired:
                        self._fired.add(alert.dedup_key)
                        alerts.append(alert)
            except Exception:
                logger.exception("Rule %s failed for game %s", rule.name, game.game_id)

        # Update previous state
        self._prev_states[game.game_id] = game

        # Clean up finished games
        if game.status == "final":
            self._cleanup_game(game.game_id)

        return alerts

    def get_max_deficit(self, game_id: str) -> int:
        return self._max_deficits.get(game_id, 0)

    def _track_deficit(self, game: GameState) -> None:
        diff = game.score_diff
        current_max = self._max_deficits.get(game.game_id, 0)
        if diff > current_max:
            self._max_deficits[game.game_id] = diff

    def _cleanup_game(self, game_id: str) -> None:
        self._prev_states.pop(game_id, None)
        self._max_deficits.pop(game_id, None)
        # Remove fired alerts for this game
        self._fired = {k for k in self._fired if k[0] != game_id}
