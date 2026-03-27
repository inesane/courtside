from __future__ import annotations

import re
from typing import Any, Optional

from alerts.base import Alert, AlertRule
from sports.base import GameState


def _get_minute(game: GameState) -> int | None:
    """Extract the current match minute from the clock or detail string."""
    # ESPN puts things like "65'" or "45'+2" in the clock
    for text in [game.clock, game.detail]:
        match = re.search(r"(\d+)'", str(text))
        if match:
            return int(match.group(1))
    return None


class LateGoalRule(AlertRule):
    """Alert when a goal is scored late in the match (80th minute+)."""

    name = "late_goal"

    def __init__(self, minute_threshold: int = 80) -> None:
        self.minute_threshold = minute_threshold

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        minute = _get_minute(game)
        if minute is None or minute < self.minute_threshold:
            return []

        if prev_game is None:
            return []

        # Detect if a goal was just scored by comparing scores
        prev_total = prev_game.home_score + prev_game.away_score
        curr_total = game.home_score + game.away_score

        if curr_total <= prev_total:
            return []

        # Figure out who scored
        if game.home_score > prev_game.home_score:
            scorer_team = game.home_team
        else:
            scorer_team = game.away_team

        return [Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"LATE GOAL: {scorer_team} scores in the {minute}'!",
            detail=f"{game.score_line()} | {game.detail}",
            priority="high",
            dedup_key=(game.game_id, self.name, curr_total),
        )]


class EqualizerRule(AlertRule):
    """Alert when a team equalizes late in the match."""

    name = "equalizer"

    def __init__(self, minute_threshold: int = 75) -> None:
        self.minute_threshold = minute_threshold

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        minute = _get_minute(game)
        if minute is None or minute < self.minute_threshold:
            return []

        if prev_game is None:
            return []

        # Was not tied before, is tied now
        was_tied = prev_game.home_score == prev_game.away_score
        is_tied = game.home_score == game.away_score

        if was_tied or not is_tied:
            return []

        # Who equalized?
        if game.home_score > prev_game.home_score:
            equalizer = game.home_team
        else:
            equalizer = game.away_team

        return [Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"EQUALIZER: {equalizer} ties it up in the {minute}'!",
            detail=f"{game.score_line()} | {game.detail} — Drama!",
            priority="high",
            dedup_key=(game.game_id, self.name, minute),
        )]


class ComebackRule(AlertRule):
    """Alert when a team comes back from a 2+ goal deficit."""

    name = "comeback"

    def __init__(self, deficit_threshold: int = 2, engine: Any = None) -> None:
        self.deficit_threshold = deficit_threshold
        self.engine = engine

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []
        if self.engine is None:
            return []

        max_deficit = self.engine.get_max_deficit(game.game_id)
        if max_deficit < self.deficit_threshold:
            return []

        # Game is now tied or the trailing team leads
        if game.score_diff > 0:
            return []

        return [Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"COMEBACK: {game.score_line()} (was {max_deficit}-0)!",
            detail=f"{game.detail} — What a turnaround!",
            priority="high",
            dedup_key=(game.game_id, self.name),
        )]


class RedCardRule(AlertRule):
    """Alert when a red card is shown."""

    name = "red_card"

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        # Red card events are stored in goal_events by the provider
        # ESPN includes red cards in keyEvents / details
        events = getattr(game, "_goal_events", [])

        alerts: list[Alert] = []
        for event in events:
            text = event.get("player", "")
            if "red card" in text.lower():
                alerts.append(Alert(
                    rule_name=self.name,
                    game_id=game.game_id,
                    headline=f"RED CARD: {text}",
                    detail=f"{game.score_line()} | {game.detail}",
                    priority="high",
                    dedup_key=(game.game_id, self.name, text),
                ))

        return alerts


class ExtraTimeRule(AlertRule):
    """Alert when a match goes to extra time."""

    name = "extra_time"

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []
        # Extra time is period 3 or 4 in soccer
        if game.period <= 2:
            return []

        return [Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"EXTRA TIME: {game.score_line()}",
            detail=f"{game.detail}",
            priority="medium",
            dedup_key=(game.game_id, self.name),
        )]
