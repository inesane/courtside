"""In-game performance alerts for user-tracked players."""

from __future__ import annotations

from typing import Any, Optional

from alerts.base import Alert, AlertRule
from sports.base import GameState


class PlayerTrackingRule(AlertRule):
    """Alert when a tracked player hits performance thresholds during a live game."""

    name = "player_tracking"

    def __init__(self, tracked_players: list[dict[str, Any]]) -> None:
        # Build lookup: espn_id -> player config
        self.tracked: dict[str, dict[str, Any]] = {}
        for p in tracked_players:
            self.tracked[p["espn_id"]] = p

    def evaluate(self, game: GameState, prev_game: Optional[GameState]) -> list[Alert]:
        if game.status != "in_progress":
            return []

        alerts: list[Alert] = []
        sport = game.sport_key

        for player in game.players:
            if not player.espn_id or player.espn_id not in self.tracked:
                continue

            config = self.tracked[player.espn_id]
            thresholds = config.get("thresholds", {})

            if sport in ("nba", "ncaab"):
                alerts.extend(self._check_basketball(game, player, thresholds))
            elif sport == "nfl":
                alerts.extend(self._check_football(game, player, thresholds))
            elif sport.startswith("soccer"):
                alerts.extend(self._check_soccer(game, player, thresholds))

        return alerts

    def _check_basketball(self, game, player, thresholds) -> list[Alert]:
        alerts = []
        pts = _get_stat(player.stats, "pts", "points")
        reb = _get_stat(player.stats, "reb", "rebounds")
        ast = _get_stat(player.stats, "ast", "assists")
        stl = _get_stat(player.stats, "stl", "steals")
        blk = _get_stat(player.stats, "blk", "blocks")

        # Points threshold
        pts_thresh = thresholds.get("points", 30)
        if pts is not None and pts >= pts_thresh:
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"PLAYER WATCH: {player.player_name} has {pts} PTS!",
                detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                priority="high",
                dedup_key=(game.game_id, self.name, player.espn_id, "pts", pts_thresh),
            ))

        # Double-double check
        if thresholds.get("double_double", True):
            categories = [v for v in [pts, reb, ast, stl, blk] if v is not None and v >= 10]
            if len(categories) >= 2:
                stats_line = []
                if pts and pts >= 10: stats_line.append(f"{pts} PTS")
                if reb and reb >= 10: stats_line.append(f"{reb} REB")
                if ast and ast >= 10: stats_line.append(f"{ast} AST")
                if stl and stl >= 10: stats_line.append(f"{stl} STL")
                if blk and blk >= 10: stats_line.append(f"{blk} BLK")

                label = "TRIPLE-DOUBLE" if len(categories) >= 3 else "DOUBLE-DOUBLE"
                alerts.append(Alert(
                    rule_name=self.name,
                    game_id=game.game_id,
                    headline=f"{label}: {player.player_name} — {', '.join(stats_line)}",
                    detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                    priority="high",
                    dedup_key=(game.game_id, self.name, player.espn_id, "dd", len(categories)),
                ))

        return alerts

    def _check_football(self, game, player, thresholds) -> list[Alert]:
        alerts = []

        pass_yds = _get_stat(player.stats, "passing_yds", "passing_yards")
        pass_tds = _get_stat(player.stats, "passing_td", "passing_tds")
        rush_yds = _get_stat(player.stats, "rushing_yds", "rushing_yards")
        rush_tds = _get_stat(player.stats, "rushing_td", "rushing_tds")
        rec_yds = _get_stat(player.stats, "receiving_yds", "receiving_yards")

        yds_thresh = thresholds.get("passing_yards", 300)
        if pass_yds is not None and pass_yds >= yds_thresh:
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"PLAYER WATCH: {player.player_name} has {pass_yds} passing yards!",
                detail=f"{game.score_line()} | {game.detail}",
                priority="high",
                dedup_key=(game.game_id, self.name, player.espn_id, "pass_yds"),
            ))

        td_thresh = thresholds.get("passing_tds", 3)
        if pass_tds is not None and pass_tds >= td_thresh:
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"PLAYER WATCH: {player.player_name} has {pass_tds} TD passes!",
                detail=f"{game.score_line()} | {game.detail}",
                priority="high",
                dedup_key=(game.game_id, self.name, player.espn_id, "pass_td"),
            ))

        rush_thresh = thresholds.get("rushing_yards", 150)
        if rush_yds is not None and rush_yds >= rush_thresh:
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"PLAYER WATCH: {player.player_name} has {rush_yds} rushing yards!",
                detail=f"{game.score_line()} | {game.detail}",
                priority="high",
                dedup_key=(game.game_id, self.name, player.espn_id, "rush_yds"),
            ))

        rec_thresh = thresholds.get("receiving_yards", 150)
        if rec_yds is not None and rec_yds >= rec_thresh:
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"PLAYER WATCH: {player.player_name} has {rec_yds} receiving yards!",
                detail=f"{game.score_line()} | {game.detail}",
                priority="high",
                dedup_key=(game.game_id, self.name, player.espn_id, "rec_yds"),
            ))

        return alerts

    def _check_soccer(self, game, player, thresholds) -> list[Alert]:
        alerts = []

        goals = _get_stat(player.stats, "g", "goals")
        assists = _get_stat(player.stats, "a", "assists")

        if goals is not None and goals >= 1 and thresholds.get("goals", True):
            label = "HAT TRICK" if goals >= 3 else "BRACE" if goals == 2 else "GOAL"
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"{label}: {player.player_name} ({goals} goal{'s' if goals > 1 else ''})!",
                detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                priority="high",
                dedup_key=(game.game_id, self.name, player.espn_id, "goals", goals),
            ))

        if assists is not None and assists >= 1 and thresholds.get("assists", True):
            alerts.append(Alert(
                rule_name=self.name,
                game_id=game.game_id,
                headline=f"ASSIST: {player.player_name} ({assists} assist{'s' if assists > 1 else ''})!",
                detail=f"{game.score_line()} | {game.detail} — {player.player_name} ({player.team})",
                priority="medium",
                dedup_key=(game.game_id, self.name, player.espn_id, "assists", assists),
            ))

        return alerts


def _get_stat(stats: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in stats:
            try:
                return int(stats[key])
            except (ValueError, TypeError):
                continue
    return None
