from __future__ import annotations

from typing import Any, Optional

from alerts.base import Alert, AlertRule
from sports.base import GameState


class CloseGameRule(AlertRule):
    """Alert when a game is close with limited time remaining."""

    name = "close_game"

    def __init__(
        self,
        point_threshold: int = 5,
        minutes_remaining: float = 4.0,
        quarters: list[int] | None = None,
    ) -> None:
        self.point_threshold = point_threshold
        self.minutes_remaining = minutes_remaining
        self.quarters = quarters or [4, 5, 6, 7]

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []
        if game.period not in self.quarters:
            return []
        if game.score_diff > self.point_threshold:
            return []

        # Only alert when time remaining is within the configured threshold
        time_left = game.clock_seconds
        if time_left > self.minutes_remaining * 60:
            return []

        priority = "high" if game.score_diff <= 3 else "medium"
        mins = int(time_left // 60)
        secs = int(time_left % 60)

        return [Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"CLOSE GAME: {game.score_line()}",
            detail=f"{game.detail} — {game.score_diff} point game with {mins}:{secs:02d} left!",
            priority=priority,
            dedup_key=(game.game_id, self.name, game.period),
        )]


class HistoricScoringRule(AlertRule):
    """Alert when a player is on a historic scoring pace."""

    name = "historic_scoring"

    def __init__(self, points_threshold: int = 50) -> None:
        self.points_threshold = points_threshold

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        alerts: list[Alert] = []
        for player in game.players:
            pts = _get_stat(player.stats, "pts", "points")
            if pts is None:
                continue

            if pts >= self.points_threshold:
                alerts.append(Alert(
                    rule_name=self.name,
                    game_id=game.game_id,
                    headline=f"HISTORIC SCORING: {player.player_name} has {pts} PTS",
                    detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team}) is on fire!",
                    priority="high",
                    dedup_key=(game.game_id, self.name, player.player_name, self.points_threshold),
                ))

        return alerts


class HistoricStatLineRule(AlertRule):
    """Alert when a player is putting up historic numbers in any stat category."""

    name = "historic_stats"

    # Default thresholds: (stat_name, display_label, threshold)
    STAT_THRESHOLDS = [
        ("reb", "REB", 25),
        ("ast", "AST", 18),
        ("stl", "STL", 7),
        ("blk", "BLK", 8),
    ]

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        alerts: list[Alert] = []
        for player in game.players:
            for stat_key, label, threshold in self.STAT_THRESHOLDS:
                val = _get_stat(player.stats, stat_key, stat_key + "ounds" if stat_key == "reb" else stat_key + "ists" if stat_key == "ast" else stat_key)
                if val is None:
                    continue

                if val >= threshold:
                    alerts.append(Alert(
                        rule_name=self.name,
                        game_id=game.game_id,
                        headline=f"HISTORIC STAT LINE: {player.player_name} has {val} {label}",
                        detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                        priority="high",
                        dedup_key=(game.game_id, self.name, player.player_name, label, threshold),
                    ))

        return alerts


class BlowoutComebackRule(AlertRule):
    """Alert when a team mounts a big comeback."""

    name = "blowout_comeback"

    def __init__(
        self,
        deficit_threshold: int = 20,
        close_threshold: int = 5,
        engine: Any = None,
    ) -> None:
        self.deficit_threshold = deficit_threshold
        self.close_threshold = close_threshold
        self.engine = engine  # Reference to AlertEngine for max deficit tracking

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []
        if self.engine is None:
            return []

        max_deficit = self.engine.get_max_deficit(game.game_id)
        if max_deficit < self.deficit_threshold:
            return []
        if game.score_diff > self.close_threshold:
            return []

        return [Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"COMEBACK ALERT: {game.score_line()}",
            detail=f"{game.detail} — was a {max_deficit}-point game, now just {game.score_diff}!",
            priority="high",
            dedup_key=(game.game_id, self.name),
        )]


class OvertimeRule(AlertRule):
    """Alert when a game goes to overtime."""

    name = "overtime"

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []
        if game.period <= 4:
            return []

        ot_number = game.period - 4
        return [Alert(
            rule_name=self.name,
            game_id=game.game_id,
            headline=f"OVERTIME: {game.score_line()}",
            detail=f"OT{ot_number} — {game.detail}",
            priority="medium",
            dedup_key=(game.game_id, self.name, game.period),
        )]


def _get_stat(stats: dict[str, Any], *keys: str) -> Optional[int]:
    """Try multiple key names to find a stat value."""
    for key in keys:
        if key in stats:
            try:
                return int(stats[key])
            except (ValueError, TypeError):
                continue
    return None
