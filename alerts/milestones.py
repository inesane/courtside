"""GOAT Tracker — checks career milestones after games go final."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MILESTONES_PATH = Path("milestones.json")
ALERTED_PATH = Path(".milestones_alerted.json")
ESPN_STATS_URL = "https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/stats"


def _load_alerted() -> set[str]:
    """Load set of milestone keys that have already been alerted."""
    if ALERTED_PATH.exists():
        with open(ALERTED_PATH) as f:
            return set(json.load(f))
    return set()


def _save_alerted(alerted: set[str]) -> None:
    with open(ALERTED_PATH, "w") as f:
        json.dump(sorted(alerted), f)


def _milestone_key(player_name: str, milestone: dict) -> str:
    """Unique key for a milestone to track if it's been alerted."""
    return f"{player_name}:{milestone['stat']}:{milestone['record_value']}"


def load_milestones() -> dict[str, Any]:
    if MILESTONES_PATH.exists():
        with open(MILESTONES_PATH) as f:
            return json.load(f)
    return {"players": []}


async def fetch_career_stats(espn_id: str) -> dict[str, Any]:
    """Fetch career totals and averages from ESPN."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(ESPN_STATS_URL.format(id=espn_id))
        resp.raise_for_status()
        data = resp.json()

    stats: dict[str, Any] = {}
    for category in data.get("categories", []):
        cat_name = category.get("name", "")
        labels = category.get("labels", [])
        totals = category.get("totals", [])
        for i, label in enumerate(labels):
            if i < len(totals):
                key = f"{cat_name}:{label}"
                # Parse numeric value
                val_str = str(totals[i])
                # Handle "made-attempted" format like "15849-31298"
                if "-" in val_str and not val_str.startswith("-"):
                    try:
                        stats[key] = int(val_str.split("-")[0])
                    except ValueError:
                        try:
                            stats[key] = float(val_str.split("-")[0])
                        except ValueError:
                            stats[key] = val_str
                else:
                    try:
                        stats[key] = int(val_str)
                    except ValueError:
                        try:
                            stats[key] = float(val_str)
                        except ValueError:
                            stats[key] = val_str

    return stats


def _get_per_game_avg(player_stats: dict[str, Any], stat: str) -> float | None:
    """Get per-game average for a stat from the averages category."""
    # GP is always 1 per game
    if stat == "GP":
        return 1.0
    val = player_stats.get(f"averages:{stat}")
    if isinstance(val, (int, float)):
        return float(val)
    return None


def check_milestones(
    player_stats: dict[str, Any],
    player_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Check which milestones could be broken next game. Skips already-alerted ones."""
    alerts: list[dict[str, Any]] = []
    player_name = player_config["name"]
    alerted = _load_alerted()
    newly_alerted = False

    for milestone in player_config.get("milestones", []):
        mkey = _milestone_key(player_name, milestone)

        # Skip milestones we've already notified about
        if mkey in alerted:
            continue

        stat_key = f"{milestone['category']}:{milestone['stat']}"
        current_val = player_stats.get(stat_key)

        if current_val is None:
            logger.debug("Stat %s not found for %s", stat_key, player_name)
            continue

        if not isinstance(current_val, (int, float)):
            continue

        current_val = int(current_val)
        record = milestone["record_value"]
        remaining = record - current_val

        if remaining <= 0:
            # Record broken — alert once then mark as done
            alerts.append({
                "player": player_name,
                "stat": milestone["stat_label"],
                "current": current_val,
                "record_holder": milestone["current_record_holder"],
                "record_value": record,
                "remaining": 0,
                "description": milestone["description"],
                "headline": f"RECORD BROKEN: {player_name} has passed {milestone['current_record_holder']} for {milestone['description']}!",
                "detail": f"{player_name} now has {current_val:,} career {milestone['stat_label']} (record was {record:,})",
                "priority": "high",
            })
            alerted.add(mkey)
            newly_alerted = True
            continue

        # Only alert if the record could realistically be broken next game
        avg = _get_per_game_avg(player_stats, milestone["stat"])
        if avg is None or avg <= 0:
            continue

        # Alert if remaining is within 1.5x the per-game average (a good game could do it)
        if remaining <= avg * 1.5:
            alerts.append({
                "player": player_name,
                "stat": milestone["stat_label"],
                "current": current_val,
                "record_holder": milestone["current_record_holder"],
                "record_value": record,
                "remaining": remaining,
                "description": milestone["description"],
                "headline": f"MILESTONE ALERT: {player_name} needs just {remaining:,} {milestone['stat_label']} to pass {milestone['current_record_holder']}!",
                "detail": f"{player_name} has {current_val:,} career {milestone['stat_label']} — needs {remaining:,} more to pass {milestone['current_record_holder']} ({record:,}) for {milestone['description']}. Could break it next game (averages {avg:.1f}/game).",
                "priority": "high",
            })
            alerted.add(mkey)
            newly_alerted = True

    if newly_alerted:
        _save_alerted(alerted)

    return alerts


async def run_milestone_check() -> list[dict[str, Any]]:
    """Run milestone checks for all tracked players."""
    config = load_milestones()
    all_alerts: list[dict[str, Any]] = []

    for player in config.get("players", []):
        try:
            stats = await fetch_career_stats(player["espn_id"])
            alerts = check_milestones(stats, player)
            all_alerts.extend(alerts)
        except Exception:
            logger.exception("Milestone check failed for %s", player.get("name"))

    return all_alerts
