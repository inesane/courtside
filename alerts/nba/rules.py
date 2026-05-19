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
            metadata={"score_diff": game.score_diff, "time_left_seconds": time_left},
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
                    metadata={"points": pts, "player": player.player_name},
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
        alerts: list[Alert] = []

        # Detect the moment regulation ends tied — fires when period transitions 4 → 5
        if (
            prev_game is not None
            and prev_game.period == 4
            and game.period == 5
            and game.score_diff == 0
        ):
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"TIED AT THE BUZZER: {game.score_line()}",
                detail=f"Game is tied — heading to overtime!",
                priority="high",
                dedup_key=(game.game_id, self.name, "tied_to_ot"),
            ))

        # Fire when each OT period starts (period transition)
        if (
            prev_game is not None
            and game.period > 4
            and game.period != prev_game.period
        ):
            ot_number = game.period - 4
            label = "Overtime" if ot_number == 1 else f"Double OT" if ot_number == 2 else f"OT{ot_number}"
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"{label.upper()}: {game.score_line()}",
                detail=f"{label} — {game.detail}",
                priority="high",
                dedup_key=(game.game_id, self.name, game.period),
            ))
        elif game.status == "in_progress" and game.period > 4:
            # Fallback: if we missed the transition (e.g. app restarted), still fire once
            ot_number = game.period - 4
            label = "Overtime" if ot_number == 1 else "Double OT" if ot_number == 2 else f"OT{ot_number}"
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"{label.upper()}: {game.score_line()}",
                detail=f"{label} — {game.detail}",
                priority="high",
                dedup_key=(game.game_id, self.name, game.period),
            ))

        return alerts


def _get_stat(stats: dict[str, Any], *keys: str) -> Optional[int]:
    """Try multiple key names to find a stat value."""
    for key in keys:
        if key in stats:
            try:
                return int(stats[key])
            except (ValueError, TypeError):
                continue
    return None
