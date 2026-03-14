from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from sports.base import GameState


@dataclass
class Alert:
    """An alert to be sent to the user."""

    rule_name: str
    game_id: str
    headline: str
    detail: str
    priority: str  # "high", "medium", "low"
    dedup_key: tuple  # Used to prevent sending the same alert twice


class AlertRule(ABC):
    """A single alert condition to check against a game state."""

    name: str

    @abstractmethod
    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        """Return alerts if this rule's conditions are met."""
        ...
