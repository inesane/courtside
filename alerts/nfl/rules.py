from __future__ import annotations

from typing import Any, Optional

from alerts.base import Alert, AlertRule
from sports.base import GameState


class HighScoringQBRule(AlertRule):
    """Alert when a QB has a massive passing game."""

    name = "high_scoring_qb"

    def __init__(self, td_threshold: int = 4, yards_threshold: int = 400) -> None:
        self.td_threshold = td_threshold
        self.yards_threshold = yards_threshold

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        alerts: list[Alert] = []
        for player in game.players:
            pass_tds = _get_stat(player.stats, "passing_td", "passing_tds")
            pass_yds = _get_stat(player.stats, "passing_yds", "passing_yards")

            if pass_tds is not None and pass_tds >= self.td_threshold:
                alerts.append(Alert(
                    rule_name=self.name,
                    game_id=game.game_id,
                    headline=f"QB ON FIRE: {player.player_name} has {pass_tds} TD passes!",
                    detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                    priority="high",
                    dedup_key=(game.game_id, self.name, player.player_name, "td", self.td_threshold),
                ))

            if pass_yds is not None and pass_yds >= self.yards_threshold:
                alerts.append(Alert(
                    rule_name=self.name,
                    game_id=game.game_id,
                    headline=f"HUGE PASSING GAME: {player.player_name} has {pass_yds} passing yards!",
                    detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                    priority="high",
                    dedup_key=(game.game_id, self.name, player.player_name, "yds", self.yards_threshold),
                ))

        return alerts


class BigRushingGameRule(AlertRule):
    """Alert when a RB/player has a monster rushing game."""

    name = "big_rushing_game"

    def __init__(self, yards_threshold: int = 150, td_threshold: int = 3) -> None:
        self.yards_threshold = yards_threshold
        self.td_threshold = td_threshold

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        alerts: list[Alert] = []
        for player in game.players:
            rush_yds = _get_stat(player.stats, "rushing_yds", "rushing_yards")
            rush_tds = _get_stat(player.stats, "rushing_td", "rushing_tds")

            if rush_yds is not None and rush_yds >= self.yards_threshold:
                alerts.append(Alert(
                    rule_name=self.name,
                    game_id=game.game_id,
                    headline=f"RUSHING EXPLOSION: {player.player_name} has {rush_yds} rushing yards!",
                    detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                    priority="high",
                    dedup_key=(game.game_id, self.name, player.player_name, "yds"),
                ))

            if rush_tds is not None and rush_tds >= self.td_threshold:
                alerts.append(Alert(
                    rule_name=self.name,
                    game_id=game.game_id,
                    headline=f"TD MACHINE: {player.player_name} has {rush_tds} rushing TDs!",
                    detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                    priority="high",
                    dedup_key=(game.game_id, self.name, player.player_name, "td"),
                ))

        return alerts


def _get_stat(stats: dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        if key in stats:
            try:
                return int(stats[key])
            except (ValueError, TypeError):
                continue
    return None
