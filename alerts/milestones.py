"""GOAT Tracker — checks career milestones after games go final."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MILESTONES_PATH = Path("milestones.json")
ESPN_STATS_URL = "https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/stats"


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


def check_milestones(
    player_stats: dict[str, Any],
    player_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Check which milestones are approaching or achieved."""
    alerts: list[dict[str, Any]] = []
    player_name = player_config["name"]

    for milestone in player_config.get("milestones", []):
        stat_key = f"{milestone['category']}:{milestone['stat']}"
        current_val = player_stats.get(stat_key)

        if current_val is None:
            logger.debug("Stat %s not found for %s", stat_key, player_name)
            continue

        if not isinstance(current_val, (int, float)):
            continue

        current_val = int(current_val)
        record = milestone["record_value"]
        alert_within = milestone["alert_within"]
        remaining = record - current_val

        if remaining <= 0:
            # Already passed the record
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
        elif remaining <= alert_within:
            alerts.append({
                "player": player_name,
                "stat": milestone["stat_label"],
                "current": current_val,
                "record_holder": milestone["current_record_holder"],
                "record_value": record,
                "remaining": remaining,
                "description": milestone["description"],
                "headline": f"MILESTONE WATCH: {player_name} is {remaining:,} {milestone['stat_label']} away from {milestone['current_record_holder']}",
                "detail": f"{player_name} has {current_val:,} career {milestone['stat_label']} — needs {remaining:,} more to pass {milestone['current_record_holder']} ({record:,}) for {milestone['description']}",
                "priority": "high" if remaining <= alert_within // 3 else "medium",
            })

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
