#!/usr/bin/env python3
"""Web UI for configuring sports notifications with built-in live monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from typing import Any

import yaml
from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for

from alerts.base import Alert, AlertRule
from alerts.engine import AlertEngine
from alerts.milestones import run_milestone_check
from alerts.player_tracking import PlayerTrackingRule
from notifications.base import Notifier
from notifications.console import ConsoleNotifier
from notifications.desktop import DesktopNotifier
from notifications.discord import DiscordNotifier
from notifications.telegram import TelegramNotifier
from sports.espn import ESPNClient
from sports.nba.provider import NBAProvider
from sports.ncaab.provider import NCAABProvider
from sports.nfl.provider import NFLProvider
from sports.soccer.provider import SoccerProvider
from sports.registry import SPORT_REGISTRY, get_enabled_sports, SOCCER_LEAGUES, SOCCER_TEAMS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webapp")

app = Flask(__name__)
CONFIG_PATH = Path("config.yaml")

# ---------------------------------------------------------------------------
# Shared state for the background monitor
# ---------------------------------------------------------------------------
alert_history: list[dict[str, Any]] = []
alert_lock = threading.Lock()
sse_clients: list[Queue] = []
sse_lock = threading.Lock()
monitor_thread: threading.Thread | None = None
monitor_running = False
monitor_status: dict[str, Any] = {"state": "stopped", "last_poll": None, "games": {}}
games_data: list[dict[str, Any]] = []
games_lock = threading.Lock()
_milestones_checked_today: str = ""  # date string to track once-per-day check

def migrate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Migrate old flat config format to new per-sport structure."""
    if "sports" in config:
        return config  # Already migrated

    # Convert old flat structure to new format
    old_alerts = config.get("alerts", {})
    new_config: dict[str, Any] = {
        "polling_interval_seconds": config.get("polling_interval_seconds", 30),
        "sports": {
            "nba": {
                "enabled": True,
                "teams_filter": config.get("teams_filter", []),
                "alerts": {
                    "close_game": old_alerts.get("close_game", {"enabled": True}),
                    "historic_scoring": old_alerts.get("historic_scoring", {"enabled": True}),
                    "historic_stats": old_alerts.get("historic_stats", {"enabled": True}),
                    "blowout_comeback": old_alerts.get("blowout_comeback", {"enabled": False}),
                    "overtime": old_alerts.get("overtime", {"enabled": True}),
                    "goat_tracker": old_alerts.get("goat_tracker", {"enabled": True}),
                },
            },
            "ncaab": {"enabled": False, "teams_filter": [], "alerts": {}},
            "nfl": {"enabled": False, "teams_filter": [], "alerts": {}},
            "soccer": {"enabled": False, "leagues": ["eng.1"], "teams_filter": [], "alerts": {}},
        },
        "tracked_players": config.get("tracked_players", []),
        "notifications": config.get("notifications", {}),
    }
    save_config(new_config)
    return new_config


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_config(config: dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Monitor background thread
# ---------------------------------------------------------------------------
def _broadcast_alert(alert_dict: dict[str, Any]) -> None:
    with sse_lock:
        dead: list[Queue] = []
        for q in sse_clients:
            try:
                q.put_nowait(alert_dict)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


def _broadcast_status(status: dict[str, Any]) -> None:
    with sse_lock:
        dead: list[Queue] = []
        for q in sse_clients:
            try:
                q.put_nowait({"_type": "status", **status})
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


def _build_providers(config: dict[str, Any], espn: ESPNClient) -> dict[str, Any]:
    """Build providers and engines for all enabled sports."""
    providers: dict[str, Any] = {}
    sports_cfg = config.get("sports", {})

    for sport_key, sport_meta in SPORT_REGISTRY.items():
        scfg = sports_cfg.get(sport_key, {})
        if not scfg.get("enabled", False):
            continue

        if sport_key == "nba":
            provider = NBAProvider(espn)
        elif sport_key == "ncaab":
            provider = NCAABProvider(espn)
        elif sport_key == "nfl":
            provider = NFLProvider(espn)
        elif sport_key == "soccer":
            leagues = scfg.get("leagues", ["eng.1"])
            # Create a provider per league — we'll merge games
            provider = [SoccerProvider(espn, league=lg) for lg in leagues]
        else:
            continue

        engine = AlertEngine(rules=[])
        alerts_cfg = scfg.get("alerts", {})
        engine.rules = sport_meta.build_rules(alerts_cfg, engine)

        # Add player tracking rule if any tracked players for this sport
        tracked = config.get("tracked_players", [])
        sport_tracked = [p for p in tracked if _player_matches_sport(p, sport_key)]
        if sport_tracked:
            engine.rules.append(PlayerTrackingRule(sport_tracked))

        providers[sport_key] = {
            "provider": provider,
            "engine": engine,
            "teams_filter": set(scfg.get("teams_filter", [])),
        }

    return providers


def _player_matches_sport(player: dict, sport_key: str) -> bool:
    """Check if a tracked player belongs to a sport."""
    league = player.get("league", "")
    if sport_key == "nba" and league == "nba":
        return True
    if sport_key == "ncaab" and league == "mens-college-basketball":
        return True
    if sport_key == "nfl" and league == "nfl":
        return True
    if sport_key == "soccer" and player.get("sport") == "soccer":
        return True
    return False


def build_notifiers(config: dict) -> list[Notifier]:
    notifiers: list[Notifier] = []
    notif_cfg = config.get("notifications", {})

    if notif_cfg.get("console", {}).get("enabled", False):
        notifiers.append(ConsoleNotifier())

    if notif_cfg.get("desktop", {}).get("enabled", False):
        notifiers.append(DesktopNotifier())

    discord_cfg = notif_cfg.get("discord", {})
    if discord_cfg.get("enabled", False) and discord_cfg.get("webhook_url"):
        notifiers.append(DiscordNotifier(discord_cfg["webhook_url"]))

    tg_cfg = notif_cfg.get("telegram", {})
    if tg_cfg.get("enabled", False) and tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
        notifiers.append(TelegramNotifier(tg_cfg["bot_token"], tg_cfg["chat_id"]))

    return notifiers


def _parse_start_time(iso_str: str) -> datetime | None:
    """Parse ESPN ISO date like '2026-03-15T17:00Z' into a datetime."""
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.replace(tzinfo=None)  # naive UTC
    except (ValueError, AttributeError):
        return None


def _seconds_until_first_game(games) -> float | None:
    """Return seconds until the earliest scheduled game, or None if any are live."""
    now_utc = datetime.utcnow()  # noqa: DTZ003
    earliest = None

    for g in games:
        if g.status == "in_progress":
            return 0  # Game is live, poll now
        if g.status == "scheduled" and g.start_time:
            dt = _parse_start_time(g.start_time)
            if dt and dt > now_utc:
                secs = (dt - now_utc).total_seconds()
                if earliest is None or secs < earliest:
                    earliest = secs

    return earliest


def _local_game_time(iso_str: str) -> str | None:
    """Convert ESPN ISO timestamp to local time string like '7:00 PM'."""
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(cleaned)
        dt_local = dt_utc.astimezone()  # converts to system local timezone
        return dt_local.strftime("%-I:%M %p %Z")
    except (ValueError, AttributeError):
        return None


def _serialize_games(games) -> list[dict[str, Any]]:
    serialized = []
    for g in games:
        detail = g.detail
        if g.status == "scheduled" and g.start_time:
            local_time = _local_game_time(g.start_time)
            if local_time:
                detail = local_time
        serialized.append({
            "game_id": g.game_id,
            "sport_key": g.sport_key,
            "status": g.status,
            "home_team": g.home_team,
            "away_team": g.away_team,
            "home_abbrev": g.home_abbrev,
            "away_abbrev": g.away_abbrev,
            "home_score": g.home_score,
            "away_score": g.away_score,
            "period": g.period,
            "clock": g.clock,
            "detail": detail,
        })
    return serialized


async def _poll_loop() -> None:
    global monitor_running, monitor_status

    config = load_config()
    config = migrate_config(config)
    polling_interval = config.get("polling_interval_seconds", 30)

    espn = ESPNClient()
    sport_providers = _build_providers(config, espn)
    notifiers = build_notifiers(config)

    enabled = list(sport_providers.keys())
    logger.info("Monitor started | sports=%s", enabled)

    try:
        while monitor_running:
            try:
                all_games: list = []
                all_live: list = []
                all_scheduled: list = []
                all_final: list = []

                # Poll all enabled sports
                for sport_key, sp in sport_providers.items():
                    provider = sp["provider"]
                    engine = sp["engine"]
                    teams_filter = sp["teams_filter"]

                    # Handle soccer multi-league
                    if isinstance(provider, list):
                        games = []
                        for p in provider:
                            games.extend(await p.get_games())
                    else:
                        games = await provider.get_games()

                    live = [g for g in games if g.status == "in_progress"]
                    scheduled = [g for g in games if g.status == "scheduled"]
                    final = [g for g in games if g.status == "final"]

                    all_games.extend(games)
                    all_live.extend(live)
                    all_scheduled.extend(scheduled)
                    all_final.extend(final)

                    webapp_enabled = config.get("notifications", {}).get("webapp", {}).get("enabled", True)

                    for game in live:
                        if teams_filter and not (
                            game.home_abbrev in teams_filter or game.away_abbrev in teams_filter
                        ):
                            continue

                        try:
                            if isinstance(provider, list):
                                # Find the right soccer provider for this game's league
                                for p in provider:
                                    if game.sport_key == f"soccer_{p.league}":
                                        await p.enrich_box_score(game)
                                        break
                            else:
                                await provider.enrich_box_score(game)
                        except Exception:
                            logger.warning("Box score fetch failed for %s", game.game_id)

                        fired = engine.evaluate(game)
                        for a in fired:
                            alert_dict = {
                                "_type": "alert",
                                "id": len(alert_history),
                                "rule": a.rule_name,
                                "sport": sport_key,
                                "headline": a.headline,
                                "detail": a.detail,
                                "priority": a.priority,
                                "time": datetime.now().strftime("%H:%M:%S"),
                            }
                            if webapp_enabled:
                                with alert_lock:
                                    alert_history.append(alert_dict)
                                _broadcast_alert(alert_dict)

                            for n in notifiers:
                                try:
                                    await n.send(a)
                                except Exception:
                                    logger.warning("Notifier %s failed", type(n).__name__)

                    for game in final:
                        if teams_filter and not (
                            game.home_abbrev in teams_filter or game.away_abbrev in teams_filter
                        ):
                            continue
                        engine.evaluate(game)

                # Update status
                monitor_status["last_poll"] = datetime.now().strftime("%H:%M:%S")
                monitor_status["games"] = {
                    "live": len(all_live), "scheduled": len(all_scheduled), "final": len(all_final),
                }

                serialized = _serialize_games(all_games)
                with games_lock:
                    games_data.clear()
                    games_data.extend(serialized)
                _broadcast_status({**monitor_status, "_games": serialized})

                # GOAT Tracker: check milestones once per day after NBA games finish
                global _milestones_checked_today
                nba_cfg = config.get("sports", {}).get("nba", {}).get("alerts", {})
                goat_enabled = nba_cfg.get("goat_tracker", {}).get("enabled", True)
                today_str = datetime.now().strftime("%Y-%m-%d")
                nba_live = [g for g in all_live if g.sport_key == "nba"]
                nba_final = [g for g in all_final if g.sport_key == "nba"]

                if goat_enabled and nba_final and not nba_live and _milestones_checked_today != today_str:
                    _milestones_checked_today = today_str
                    logger.info("Running GOAT Tracker milestone check...")
                    try:
                        milestone_alerts = await run_milestone_check()
                        webapp_enabled = config.get("notifications", {}).get("webapp", {}).get("enabled", True)
                        for ma in milestone_alerts:
                            alert_dict = {
                                "_type": "alert",
                                "id": len(alert_history),
                                "rule": "goat_tracker",
                                "sport": "nba",
                                "headline": ma["headline"],
                                "detail": ma["detail"],
                                "priority": ma["priority"],
                                "time": datetime.now().strftime("%H:%M:%S"),
                            }
                            if webapp_enabled:
                                with alert_lock:
                                    alert_history.append(alert_dict)
                                _broadcast_alert(alert_dict)

                            alert_obj = Alert(
                                rule_name="goat_tracker",
                                game_id="milestone",
                                headline=ma["headline"],
                                detail=ma["detail"],
                                priority=ma["priority"],
                                dedup_key=("milestone", ma["stat"], ma["record_holder"]),
                            )
                            for n in notifiers:
                                try:
                                    await n.send(alert_obj)
                                except Exception:
                                    logger.warning("Notifier %s failed for milestone", type(n).__name__)
                    except Exception:
                        logger.exception("GOAT Tracker check failed")

                # Smart sleep based on game state
                secs_until = _seconds_until_first_game(all_games)

                if all_live:
                    sleep_secs = polling_interval
                elif secs_until is not None and secs_until > 0:
                    wait = max(0, secs_until - 300)
                    if wait > polling_interval:
                        mins = int(wait // 60)
                        logger.info(
                            "No live games. Next game in ~%dh %dm. Sleeping until 5 min before tipoff.",
                            mins // 60, mins % 60,
                        )
                        monitor_status["state"] = f"waiting (next game in {mins // 60}h {mins % 60}m)"
                        _broadcast_status(monitor_status)
                        sleep_secs = wait
                    else:
                        sleep_secs = polling_interval
                else:
                    idle_interval = 1800
                    logger.info("No upcoming games. Checking again in 30 minutes.")
                    monitor_status["state"] = "idle (no upcoming games)"
                    _broadcast_status(monitor_status)
                    sleep_secs = idle_interval

            except Exception:
                logger.exception("Poll cycle error")
                sleep_secs = polling_interval

            for _ in range(int(sleep_secs)):
                if not monitor_running:
                    break
                await asyncio.sleep(1)

            if monitor_running:
                monitor_status["state"] = "running"
    finally:
        await espn.close()
        monitor_status["state"] = "stopped"
        logger.info("Monitor stopped")


def _run_monitor() -> None:
    asyncio.run(_poll_loop())


def start_monitor() -> None:
    global monitor_thread, monitor_running, monitor_status
    if monitor_running:
        return
    monitor_running = True
    monitor_status["state"] = "running"
    monitor_thread = threading.Thread(target=_run_monitor, daemon=True)
    monitor_thread.start()


def stop_monitor() -> None:
    global monitor_running
    monitor_running = False


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------
TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Courtside</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f1117;
            color: #e1e4e8;
            min-height: 100vh;
        }

        .header {
            position: sticky;
            top: 0;
            z-index: 50;
            background: #161b22;
            border-bottom: 1px solid #30363d;
            padding: 12px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .header-left {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .header h1 {
            font-size: 20px;
            color: #fff;
        }

        .monitor-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
        }

        .monitor-badge.stopped {
            background: #21262d;
            color: #8b949e;
        }

        .monitor-badge.running {
            background: #0d301e;
            color: #3fb950;
        }

        .monitor-badge .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: currentColor;
        }

        .monitor-badge.running .dot {
            animation: pulse 2s ease-in-out infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }

        .header-right {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .status-text {
            font-size: 12px;
            color: #8b949e;
        }

        .notif-bell {
            position: relative;
            background: none;
            border: none;
            cursor: pointer;
            padding: 6px;
            border-radius: 8px;
            transition: background 0.15s;
        }

        .notif-bell:hover {
            background: #21262d;
        }

        .notif-bell svg {
            width: 24px;
            height: 24px;
            fill: #e1e4e8;
        }

        .notif-badge {
            position: absolute;
            top: 2px;
            right: 2px;
            min-width: 16px;
            height: 16px;
            background: #da3633;
            color: #fff;
            border-radius: 8px;
            font-size: 10px;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 0 4px;
        }

        .notif-badge.hidden { display: none; }

        /* Notification panel */
        .notif-panel {
            position: fixed;
            top: 0;
            right: -420px;
            width: 420px;
            height: 100vh;
            background: #161b22;
            border-left: 1px solid #30363d;
            z-index: 200;
            transition: right 0.3s ease;
            display: flex;
            flex-direction: column;
        }

        .notif-panel.open {
            right: 0;
        }

        .notif-panel-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 16px 20px;
            border-bottom: 1px solid #30363d;
        }

        .notif-panel-header h2 {
            font-size: 16px;
            color: #fff;
        }

        .notif-panel-close {
            background: none;
            border: none;
            color: #8b949e;
            cursor: pointer;
            font-size: 22px;
            padding: 4px 8px;
            border-radius: 6px;
        }

        .notif-panel-close:hover {
            background: #21262d;
            color: #fff;
        }

        .notif-panel-actions {
            display: flex;
            gap: 8px;
        }

        .notif-clear {
            background: none;
            border: 1px solid #30363d;
            color: #8b949e;
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 6px;
            cursor: pointer;
        }

        .notif-clear:hover {
            color: #e1e4e8;
            border-color: #8b949e;
        }

        .notif-list {
            flex: 1;
            overflow-y: auto;
            padding: 8px 0;
        }

        .notif-empty {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 200px;
            color: #484f58;
            font-size: 14px;
            gap: 8px;
        }

        .notif-empty svg {
            width: 40px;
            height: 40px;
            fill: #30363d;
        }

        .notif-item {
            padding: 12px 20px;
            border-bottom: 1px solid #21262d;
            animation: slideIn 0.3s ease;
        }

        @keyframes slideIn {
            from { opacity: 0; transform: translateX(20px); }
            to { opacity: 1; transform: translateX(0); }
        }

        .notif-item .notif-header {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 4px;
        }

        .notif-item .notif-priority {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .notif-priority.high { background: #da3633; }
        .notif-priority.medium { background: #d29922; }
        .notif-priority.low { background: #3fb950; }

        .notif-item .notif-headline {
            font-size: 14px;
            font-weight: 600;
            color: #fff;
            flex: 1;
        }

        .notif-item .notif-time {
            font-size: 11px;
            color: #484f58;
            flex-shrink: 0;
        }

        .notif-item .notif-detail {
            font-size: 13px;
            color: #8b949e;
            margin-top: 2px;
            padding-left: 16px;
        }

        .notif-item .notif-rule {
            display: inline-block;
            font-size: 10px;
            color: #58a6ff;
            background: #0d2240;
            padding: 1px 6px;
            border-radius: 4px;
            margin-top: 4px;
            margin-left: 16px;
        }

        .overlay {
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.5);
            z-index: 150;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.3s;
        }

        .overlay.open {
            opacity: 1;
            pointer-events: auto;
        }

        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 32px 20px;
        }

        .subtitle {
            color: #8b949e;
            margin-bottom: 32px;
            font-size: 15px;
        }

        .card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
        }

        .card h2 {
            font-size: 18px;
            margin-bottom: 16px;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .card h2 .icon { font-size: 20px; }

        .team-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 8px;
        }

        .team-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 8px;
            cursor: pointer;
            transition: background 0.15s;
            user-select: none;
        }

        .team-item:hover { background: #21262d; }

        .team-item input[type="checkbox"] {
            width: 16px;
            height: 16px;
            accent-color: #58a6ff;
        }

        .team-item .abbrev {
            color: #8b949e;
            font-size: 12px;
            font-weight: 600;
            min-width: 36px;
        }

        .team-item .name { font-size: 14px; }

        .all-teams-toggle {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 16px;
            padding: 10px 14px;
            background: #21262d;
            border-radius: 8px;
        }

        .all-teams-toggle label { font-size: 14px; cursor: pointer; }

        .alert-row {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            padding: 16px 0;
            border-bottom: 1px solid #21262d;
        }

        .alert-row:last-child { border-bottom: none; padding-bottom: 0; }
        .alert-row:first-child { padding-top: 0; }

        .alert-info { flex: 1; }

        .alert-info .alert-name { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
        .alert-info .alert-desc { font-size: 13px; color: #8b949e; }

        .toggle {
            position: relative;
            width: 44px;
            height: 24px;
            flex-shrink: 0;
            margin-left: 16px;
            margin-top: 2px;
        }

        .toggle input { opacity: 0; width: 0; height: 0; }

        .toggle .slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #30363d;
            border-radius: 12px;
            transition: background 0.2s;
        }

        .toggle .slider::before {
            content: "";
            position: absolute;
            height: 18px; width: 18px;
            left: 3px; bottom: 3px;
            background: #e1e4e8;
            border-radius: 50%;
            transition: transform 0.2s;
        }

        .toggle input:checked + .slider { background: #238636; }
        .toggle input:checked + .slider::before { transform: translateX(20px); }

        .settings-row {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 10px;
            padding: 8px 12px;
            background: #0d1117;
            border-radius: 8px;
        }

        .settings-row label { font-size: 13px; color: #8b949e; white-space: nowrap; }

        .settings-row input[type="number"] {
            width: 70px;
            padding: 4px 8px;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            color: #e1e4e8;
            font-size: 13px;
        }

        .channel-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid #21262d;
        }

        .channel-row:last-child { border-bottom: none; }

        .channel-info { display: flex; align-items: center; gap: 10px; }
        .channel-info .channel-name { font-size: 15px; font-weight: 500; }
        .channel-info .channel-desc { font-size: 12px; color: #8b949e; }

        .webhook-input {
            width: 100%;
            margin-top: 8px;
            padding: 8px 12px;
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 8px;
            color: #e1e4e8;
            font-size: 13px;
        }

        .polling-input {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .polling-input input[type="number"] {
            width: 80px;
            padding: 6px 10px;
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 8px;
            color: #e1e4e8;
            font-size: 14px;
        }

        .polling-input span { font-size: 14px; color: #8b949e; }

        .btn-save {
            display: block;
            width: 100%;
            padding: 14px;
            background: #238636;
            color: #fff;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.15s;
            margin-top: 8px;
        }

        .btn-save:hover { background: #2ea043; }

        .toast {
            position: fixed;
            bottom: 30px;
            left: 50%;
            transform: translateX(-50%) translateY(100px);
            background: #238636;
            color: #fff;
            padding: 12px 24px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 500;
            opacity: 0;
            transition: all 0.3s ease;
            z-index: 100;
        }

        .toast.show {
            transform: translateX(-50%) translateY(0);
            opacity: 1;
        }

        /* Scoreboard */
        .scoreboard-section {
            margin-bottom: 32px;
        }

        .scoreboard-section h2 {
            font-size: 18px;
            color: #fff;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .scoreboard-empty {
            text-align: center;
            color: #484f58;
            padding: 32px;
            font-size: 14px;
        }

        .scoreboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 12px;
        }

        .game-card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 10px;
            padding: 14px 18px;
            transition: border-color 0.2s;
        }

        .game-card.live {
            border-color: #238636;
            background: #0d1f0d;
        }

        .game-card.scheduled {
            border-color: #1f3d6f;
        }

        .game-card.final {
            border-color: #30363d;
            opacity: 0.7;
        }

        .game-card .game-status-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 10px;
        }

        .game-status-tag {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            padding: 2px 8px;
            border-radius: 4px;
        }

        .game-status-tag.live {
            background: #0d301e;
            color: #3fb950;
        }

        .game-status-tag.scheduled {
            background: #0d2240;
            color: #58a6ff;
        }

        .game-status-tag.final {
            background: #21262d;
            color: #8b949e;
        }

        .game-clock {
            font-size: 12px;
            color: #8b949e;
        }

        .game-teams {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .game-team-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .game-team-info {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .game-team-abbrev {
            font-size: 12px;
            font-weight: 700;
            color: #8b949e;
            min-width: 36px;
        }

        .game-team-name {
            font-size: 14px;
            color: #e1e4e8;
        }

        .game-team-score {
            font-size: 20px;
            font-weight: 700;
            color: #fff;
            min-width: 36px;
            text-align: right;
        }

        .game-team-row.winning .game-team-name,
        .game-team-row.winning .game-team-score {
            color: #fff;
        }

        .game-team-row.losing .game-team-name,
        .game-team-row.losing .game-team-score {
            color: #8b949e;
        }

        .game-divider {
            height: 1px;
            background: #21262d;
            margin: 6px 0;
        }

        /* Sport tabs */
        .sport-tabs {
            display: flex;
            gap: 4px;
            padding: 8px 24px;
            background: #0d1117;
            border-bottom: 1px solid #21262d;
            overflow-x: auto;
        }

        .sport-tab {
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            background: none;
            border: 1px solid transparent;
            color: #8b949e;
            transition: all 0.15s;
            white-space: nowrap;
        }

        .sport-tab:hover { background: #161b22; color: #e1e4e8; }
        .sport-tab.active { background: #161b22; color: #fff; border-color: #30363d; }
        .sport-tab .tab-icon { margin-right: 4px; }

        .sport-panel { display: none; }
        .sport-panel.active { display: block; }

        /* Player tracking */
        .player-search-box {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
        }

        .player-search-input {
            flex: 1;
            padding: 10px 14px;
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 8px;
            color: #e1e4e8;
            font-size: 14px;
        }

        .player-search-btn {
            padding: 10px 16px;
            background: #21262d;
            border: 1px solid #30363d;
            border-radius: 8px;
            color: #e1e4e8;
            font-size: 14px;
            cursor: pointer;
        }

        .player-search-btn:hover { background: #30363d; }

        .player-results {
            margin-bottom: 16px;
        }

        .player-result-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 14px;
            border: 1px solid #21262d;
            border-radius: 8px;
            margin-bottom: 6px;
            background: #0d1117;
        }

        .player-result-info {
            display: flex;
            flex-direction: column;
        }

        .player-result-name { font-size: 14px; font-weight: 600; }
        .player-result-team { font-size: 12px; color: #8b949e; }
        .player-result-sport { font-size: 11px; color: #58a6ff; }

        .player-track-btn {
            padding: 6px 14px;
            background: #238636;
            border: none;
            border-radius: 6px;
            color: #fff;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
        }

        .player-track-btn:hover { background: #2ea043; }

        .tracked-player-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 14px;
            border: 1px solid #30363d;
            border-radius: 8px;
            margin-bottom: 6px;
        }

        .player-remove-btn {
            padding: 4px 10px;
            background: none;
            border: 1px solid #da3633;
            border-radius: 6px;
            color: #da3633;
            font-size: 12px;
            cursor: pointer;
        }

        .player-remove-btn:hover { background: #da3633; color: #fff; }

        .soccer-leagues-grid {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 8px;
        }
    </style>
</head>
<body>
    <!-- Header -->
    <div class="header">
        <div class="header-left">
            <h1>Sports Notifications</h1>
            <button class="monitor-badge {{ 'running' if monitor_running else 'stopped' }}"
                    id="monitor-btn" onclick="toggleMonitor()">
                <span class="dot"></span>
                <span id="monitor-label">{{ 'Monitoring' if monitor_running else 'Stopped' }}</span>
            </button>
        </div>
        <div class="header-right">
            <span class="status-text" id="status-text">
                {% if monitor_running and monitor_status.last_poll %}
                    Last poll: {{ monitor_status.last_poll }} |
                    {{ monitor_status.games.live or 0 }} live,
                    {{ monitor_status.games.scheduled or 0 }} scheduled
                {% endif %}
            </span>
            <button class="notif-bell" onclick="togglePanel()">
                <svg viewBox="0 0 24 24"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/></svg>
                <span class="notif-badge {{ 'hidden' if not alert_count }}" id="notif-count">{{ alert_count }}</span>
            </button>
        </div>
    </div>

    <!-- Overlay -->
    <div class="overlay" id="overlay" onclick="togglePanel()"></div>

    <!-- Notification Panel -->
    <div class="notif-panel" id="notif-panel">
        <div class="notif-panel-header">
            <h2>Notifications</h2>
            <div class="notif-panel-actions">
                <button class="notif-clear" onclick="clearAlerts()">Clear all</button>
                <button class="notif-panel-close" onclick="togglePanel()">&times;</button>
            </div>
        </div>
        <div class="notif-list" id="notif-list">
            {% if not alerts %}
            <div class="notif-empty" id="notif-empty">
                <svg viewBox="0 0 24 24"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/></svg>
                <span>No notifications yet</span>
                <span style="font-size:12px">Start monitoring to receive alerts</span>
            </div>
            {% endif %}
            {% for a in alerts|reverse %}
            <div class="notif-item">
                <div class="notif-header">
                    <span class="notif-priority {{ a.priority }}"></span>
                    <span class="notif-headline">{{ a.headline }}</span>
                    <span class="notif-time">{{ a.time }}</span>
                </div>
                <div class="notif-detail">{{ a.detail }}</div>
                <span class="notif-rule">{{ a.rule }}</span>
            </div>
            {% endfor %}
        </div>
    </div>

    <!-- Sport Tabs -->
    <div class="sport-tabs">
        {% for st in sport_tabs %}
        <button class="sport-tab {% if loop.first %}active{% endif %}" onclick="switchTab('{{ st.key }}')" id="tab-{{ st.key }}">
            <span class="tab-icon">{{ st.icon|safe }}</span> {{ st.display_name }}
        </button>
        {% endfor %}
        <button class="sport-tab" onclick="switchTab('players')" id="tab-players">
            <span class="tab-icon">&#11088;</span> Player Tracker
        </button>
    </div>

    <!-- Main content -->
    <div class="container">
        <form method="POST" action="/save" id="config-form">

        {% for st in sport_tabs %}
        <div class="sport-panel {% if loop.first %}active{% endif %}" id="panel-{{ st.key }}">

            <!-- Enable sport toggle -->
            <div class="card">
                <h2>{{ st.icon|safe }} {{ st.display_name }}
                    <label class="toggle" style="margin-left: auto;">
                        <input type="checkbox" name="{{ st.key }}_enabled" {{ 'checked' if st.enabled }}>
                        <span class="slider"></span>
                    </label>
                </h2>

                {% if st.key == 'soccer' %}
                <p style="font-size: 13px; color: #8b949e; margin-bottom: 12px;">Select leagues to monitor:</p>
                <div class="soccer-leagues-grid">
                    {% for lg_key, lg_name in soccer_leagues.items() %}
                    <label class="team-item" style="padding: 6px 12px;">
                        <input type="checkbox" name="soccer_leagues" value="{{ lg_key }}"
                            {{ 'checked' if lg_key in st.leagues }}>
                        <span class="name">{{ lg_name }}</span>
                    </label>
                    {% endfor %}
                </div>
                {% endif %}
            </div>

            <!-- Scoreboard for this sport -->
            <div class="scoreboard-section">
                <h2>{{ st.icon|safe }} Today's Games</h2>
                <div class="scoreboard-grid" id="scoreboard-{{ st.key }}">
                    {% set ns = namespace(has_games=false) %}
                    {% for g in games_list %}
                        {% if g.sport_key is defined and g.sport_key == st.key or (st.key == 'soccer' and g.sport_key is defined and g.sport_key.startswith('soccer')) %}
                        {% set ns.has_games = true %}
                        <div class="game-card {{ 'live' if g.status == 'in_progress' else 'scheduled' if g.status == 'scheduled' else 'final' }}" data-game-id="{{ g.game_id }}">
                            <div class="game-status-bar">
                                {% if g.status == 'in_progress' %}
                                    <span class="game-status-tag live">Live</span>
                                {% elif g.status == 'scheduled' %}
                                    <span class="game-status-tag scheduled">Scheduled</span>
                                {% else %}
                                    <span class="game-status-tag final">Final</span>
                                {% endif %}
                                <span class="game-clock">{{ g.detail }}</span>
                            </div>
                            <div class="game-teams">
                                <div class="game-team-row {{ 'winning' if g.away_score > g.home_score else 'losing' if g.away_score < g.home_score else '' }}">
                                    <div class="game-team-info">
                                        <span class="game-team-abbrev">{{ g.away_abbrev }}</span>
                                        <span class="game-team-name">{{ g.away_team }}</span>
                                    </div>
                                    <span class="game-team-score">{{ g.away_score if g.status != 'scheduled' else '' }}</span>
                                </div>
                                <div class="game-divider"></div>
                                <div class="game-team-row {{ 'winning' if g.home_score > g.away_score else 'losing' if g.home_score < g.away_score else '' }}">
                                    <div class="game-team-info">
                                        <span class="game-team-abbrev">{{ g.home_abbrev }}</span>
                                        <span class="game-team-name">{{ g.home_team }}</span>
                                    </div>
                                    <span class="game-team-score">{{ g.home_score if g.status != 'scheduled' else '' }}</span>
                                </div>
                            </div>
                        </div>
                        {% endif %}
                    {% endfor %}
                    {% if not ns.has_games %}
                    <div class="scoreboard-empty">
                        {% if st.enabled %}Start monitoring to see today's games{% else %}Enable {{ st.display_name }} to see games{% endif %}
                    </div>
                    {% endif %}
                </div>
            </div>

            {% if st.key == 'soccer' %}
            <!-- Soccer Teams (filtered by selected leagues) -->
            <div class="card">
                <h2><span class="icon">{{ st.icon|safe }}</span> Teams</h2>
                <div class="all-teams-toggle">
                    <input type="checkbox" id="{{ st.key }}-all-teams" name="{{ st.key }}_all_teams"
                        {{ 'checked' if not st.teams_filter }}
                        onchange="toggleTeamGrid('{{ st.key }}')">
                    <label for="{{ st.key }}-all-teams">Follow all teams</label>
                </div>
                <div class="team-grid" id="{{ st.key }}-team-grid">
                    {% for lg_key in st.leagues %}
                        {% if lg_key in soccer_teams %}
                        <div style="grid-column: 1 / -1; font-size: 13px; color: #58a6ff; font-weight: 600; margin-top: 8px;">
                            {{ soccer_leagues.get(lg_key, lg_key) }}
                        </div>
                        {% for abbrev, name in soccer_teams[lg_key] %}
                        <label class="team-item">
                            <input type="checkbox" name="{{ st.key }}_teams" value="{{ abbrev }}"
                                {{ 'checked' if abbrev in st.teams_filter }}>
                            <span class="abbrev">{{ abbrev }}</span>
                            <span class="name">{{ name }}</span>
                        </label>
                        {% endfor %}
                        {% endif %}
                    {% endfor %}
                </div>
            </div>
            {% elif st.teams %}
            <!-- Teams -->
            <div class="card">
                <h2><span class="icon">{{ st.icon|safe }}</span> Teams</h2>
                <div class="all-teams-toggle">
                    <input type="checkbox" id="{{ st.key }}-all-teams" name="{{ st.key }}_all_teams"
                        {{ 'checked' if not st.teams_filter }}
                        onchange="toggleTeamGrid('{{ st.key }}')">
                    <label for="{{ st.key }}-all-teams">Follow all teams</label>
                </div>
                <div class="team-grid" id="{{ st.key }}-team-grid">
                    {% for abbrev, name in st.teams %}
                    <label class="team-item">
                        <input type="checkbox" name="{{ st.key }}_teams" value="{{ abbrev }}"
                            {{ 'checked' if abbrev in st.teams_filter }}>
                        <span class="abbrev">{{ abbrev }}</span>
                        <span class="name">{{ name }}</span>
                    </label>
                    {% endfor %}
                </div>
            </div>
            {% endif %}

            <!-- Sport-specific alerts -->
            <div class="card">
                <h2><span class="icon">&#128276;</span> {{ st.display_name }} Alerts</h2>

                {% if st.key == 'nba' %}
                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">Close Games</div>
                        <div class="alert-desc">Games within a few points near the end of the 4th quarter or OT</div>
                        <div class="settings-row">
                            <label>Margin &le;</label>
                            <input type="number" name="{{ st.key }}_close_game_threshold" min="1" max="20"
                                value="{{ st.alerts_cfg.get('close_game', {}).get('point_threshold', 5) }}">
                            <label>Time &le;</label>
                            <input type="number" name="{{ st.key }}_close_game_minutes" min="1" max="12" step="0.5"
                                value="{{ st.alerts_cfg.get('close_game', {}).get('minutes_remaining', 4) }}">
                            <span>min</span>
                        </div>
                    </div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_close_game_enabled"
                        {{ 'checked' if st.alerts_cfg.get('close_game', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">Historic Scoring</div>
                        <div class="alert-desc">Player hits a massive point total</div>
                        <div class="settings-row">
                            <label>Points &ge;</label>
                            <input type="number" name="{{ st.key }}_scoring_points" min="30" max="80"
                                value="{{ st.alerts_cfg.get('historic_scoring', {}).get('points_threshold', 50) }}">
                        </div>
                    </div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_historic_scoring_enabled"
                        {{ 'checked' if st.alerts_cfg.get('historic_scoring', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Historic Stat Lines</div>
                    <div class="alert-desc">Huge rebounds, assists, steals, or blocks</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_historic_stats_enabled"
                        {{ 'checked' if st.alerts_cfg.get('historic_stats', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Blowout Comebacks</div>
                    <div class="alert-desc">Team erasing a big deficit</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_blowout_comeback_enabled"
                        {{ 'checked' if st.alerts_cfg.get('blowout_comeback', {}).get('enabled', false) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Overtime</div><div class="alert-desc">Games that go to OT</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_overtime_enabled"
                        {{ 'checked' if st.alerts_cfg.get('overtime', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">GOAT Tracker</div>
                    <div class="alert-desc">Career milestone alerts (LeBron tracking)</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_goat_tracker_enabled"
                        {{ 'checked' if st.alerts_cfg.get('goat_tracker', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>

                {% elif st.key == 'ncaab' %}
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Close Games</div>
                    <div class="alert-desc">Tight games in the 2nd half or OT</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_close_game_enabled"
                        {{ 'checked' if st.alerts_cfg.get('close_game', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Upset Alert</div>
                    <div class="alert-desc">Lower seed leading a higher seed late</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_upset_alert_enabled"
                        {{ 'checked' if st.alerts_cfg.get('upset_alert', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Historic Scoring</div>
                    <div class="alert-desc">Player hits 40+ points</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_historic_scoring_enabled"
                        {{ 'checked' if st.alerts_cfg.get('historic_scoring', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Historic Stat Lines</div>
                    <div class="alert-desc">Huge rebounds, assists, steals, or blocks</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_historic_stats_enabled"
                        {{ 'checked' if st.alerts_cfg.get('historic_stats', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Blowout Comebacks</div>
                    <div class="alert-desc">Team erasing a big deficit</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_blowout_comeback_enabled"
                        {{ 'checked' if st.alerts_cfg.get('blowout_comeback', {}).get('enabled', false) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Overtime</div><div class="alert-desc">Games that go to OT</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_overtime_enabled"
                        {{ 'checked' if st.alerts_cfg.get('overtime', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>

                {% elif st.key == 'nfl' %}
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Close Games</div>
                    <div class="alert-desc">Within one score in the 4th quarter</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_close_game_enabled"
                        {{ 'checked' if st.alerts_cfg.get('close_game', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">QB on Fire</div>
                    <div class="alert-desc">Quarterback with 4+ TD passes or 400+ yards</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_high_scoring_qb_enabled"
                        {{ 'checked' if st.alerts_cfg.get('high_scoring_qb', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Rushing Explosion</div>
                    <div class="alert-desc">Player with 150+ rushing yards or 3+ TDs</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_big_rushing_game_enabled"
                        {{ 'checked' if st.alerts_cfg.get('big_rushing_game', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Blowout Comebacks</div>
                    <div class="alert-desc">Team erasing a 17+ point deficit</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_blowout_comeback_enabled"
                        {{ 'checked' if st.alerts_cfg.get('blowout_comeback', {}).get('enabled', false) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Overtime</div><div class="alert-desc">Games that go to OT</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_overtime_enabled"
                        {{ 'checked' if st.alerts_cfg.get('overtime', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>

                {% elif st.key == 'soccer' %}
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Late Goals</div>
                    <div class="alert-desc">Goals scored in the 80th minute or later</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_late_goal_enabled"
                        {{ 'checked' if st.alerts_cfg.get('late_goal', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Equalizer</div>
                    <div class="alert-desc">Team ties the match late (75th minute+)</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_equalizer_enabled"
                        {{ 'checked' if st.alerts_cfg.get('equalizer', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Comeback</div>
                    <div class="alert-desc">Team comes back from 2+ goals down</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_comeback_enabled"
                        {{ 'checked' if st.alerts_cfg.get('comeback', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Red Card</div>
                    <div class="alert-desc">Player receives a red card</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_red_card_enabled"
                        {{ 'checked' if st.alerts_cfg.get('red_card', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                <div class="alert-row">
                    <div class="alert-info"><div class="alert-name">Extra Time</div>
                    <div class="alert-desc">Match goes to extra time</div></div>
                    <label class="toggle"><input type="checkbox" name="{{ st.key }}_extra_time_enabled"
                        {{ 'checked' if st.alerts_cfg.get('extra_time', {}).get('enabled', true) }}><span class="slider"></span></label>
                </div>
                {% endif %}
            </div>
        </div>
        {% endfor %}

        <!-- Player Tracker Panel -->
        <div class="sport-panel" id="panel-players">
            <div class="card">
                <h2><span class="icon">&#11088;</span> Track Players</h2>
                <p style="font-size: 13px; color: #8b949e; margin-bottom: 16px;">
                    Search for any player to get notified about their big moments during live games.
                </p>

                <div class="player-search-box">
                    <input type="text" class="player-search-input" id="player-search" placeholder="Search players (e.g. Luka Doncic, Haaland)..." autocomplete="off">
                    <button type="button" class="player-search-btn" onclick="searchPlayers()">Search</button>
                </div>

                <div class="player-results" id="player-results"></div>

                <h3 style="font-size: 15px; color: #fff; margin: 20px 0 12px;">Tracked Players</h3>
                <div id="tracked-players-list">
                    {% for p in tracked_players %}
                    <div class="tracked-player-item" id="tracked-{{ p.espn_id }}">
                        <div class="player-result-info">
                            <span class="player-result-name">{{ p.name }}</span>
                            <span class="player-result-sport">{{ p.league | upper }}</span>
                        </div>
                        <button type="button" class="player-remove-btn" onclick="untrackPlayer('{{ p.espn_id }}')">Remove</button>
                    </div>
                    {% endfor %}
                    {% if not tracked_players %}
                    <p style="color: #484f58; font-size: 14px;" id="no-tracked-msg">No players tracked yet. Search above to add players.</p>
                    {% endif %}
                </div>
            </div>
        </div>

        <!-- Notification Channels (shared across all sports) -->
        <div class="card">
            <h2><span class="icon">&#128228;</span> Channels</h2>

            <div class="channel-row">
                <div class="channel-info">
                    <span class="channel-name">In-App (Bell)</span>
                    <span class="channel-desc">Notifications in the bell icon panel</span>
                </div>
                <label class="toggle">
                    <input type="checkbox" name="webapp_enabled"
                        {{ 'checked' if notif.get('webapp', {}).get('enabled', true) }}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="channel-row">
                <div class="channel-info">
                    <span class="channel-name">Console</span>
                    <span class="channel-desc">Print alerts to the terminal</span>
                </div>
                <label class="toggle">
                    <input type="checkbox" name="console_enabled"
                        {{ 'checked' if notif.get('console', {}).get('enabled', true) }}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="channel-row">
                <div class="channel-info">
                    <span class="channel-name">Desktop Notifications</span>
                    <span class="channel-desc">OS-level push notifications</span>
                </div>
                <label class="toggle">
                    <input type="checkbox" name="desktop_enabled"
                        {{ 'checked' if notif.get('desktop', {}).get('enabled', false) }}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="channel-row" style="flex-wrap: wrap;">
                <div class="channel-info" style="flex: 1;">
                    <span class="channel-name">Discord</span>
                </div>
                <label class="toggle">
                    <input type="checkbox" name="discord_enabled" id="discord-toggle"
                        {{ 'checked' if notif.get('discord', {}).get('enabled', false) }}>
                    <span class="slider"></span>
                </label>
                <input type="text" class="webhook-input" name="discord_webhook"
                    placeholder="Webhook URL"
                    value="{{ notif.get('discord', {}).get('webhook_url', '') }}"
                    id="discord-url"
                    style="display: {{ 'block' if notif.get('discord', {}).get('enabled', false) else 'none' }};">
            </div>

            <div class="channel-row" style="flex-wrap: wrap;">
                <div class="channel-info" style="flex: 1;">
                    <span class="channel-name">Telegram</span>
                </div>
                <label class="toggle">
                    <input type="checkbox" name="telegram_enabled" id="telegram-toggle"
                        {{ 'checked' if notif.get('telegram', {}).get('enabled', false) }}>
                    <span class="slider"></span>
                </label>
                <div id="telegram-fields"
                    style="display: {{ 'block' if notif.get('telegram', {}).get('enabled', false) else 'none' }}; width: 100%;">
                    <input type="text" class="webhook-input" name="telegram_bot_token"
                        placeholder="Bot token (from @BotFather)"
                        value="{{ notif.get('telegram', {}).get('bot_token', '') }}"
                        style="margin-bottom: 6px;">
                    <input type="text" class="webhook-input" name="telegram_chat_id"
                        placeholder="Chat ID"
                        value="{{ notif.get('telegram', {}).get('chat_id', '') }}">
                </div>
            </div>
        </div>

        <!-- Polling -->
        <div class="card">
            <h2><span class="icon">&#9201;</span> Polling Interval</h2>
            <div class="polling-input">
                <input type="number" name="polling_interval" min="10" max="120"
                    value="{{ config.get('polling_interval_seconds', 30) }}">
                <span>seconds</span>
            </div>
        </div>

        <button type="submit" class="btn-save">Save Configuration</button>
        </form>
    </div>

    <div class="toast" id="toast">Configuration saved!</div>

    <script>
        // ---- Notification panel ----
        const panel = document.getElementById('notif-panel');
        const overlay = document.getElementById('overlay');
        const notifList = document.getElementById('notif-list');
        const notifCount = document.getElementById('notif-count');
        const notifEmpty = document.getElementById('notif-empty');
        let panelOpen = false;
        let unseenCount = {{ unseen_count }};

        function togglePanel() {
            panelOpen = !panelOpen;
            panel.classList.toggle('open', panelOpen);
            overlay.classList.toggle('open', panelOpen);
            if (panelOpen) {
                unseenCount = 0;
                updateBadge();
            }
        }

        function updateBadge() {
            notifCount.textContent = unseenCount;
            notifCount.classList.toggle('hidden', unseenCount === 0);
        }

        function addNotification(data) {
            // Remove empty state
            if (notifEmpty) notifEmpty.remove();

            const item = document.createElement('div');
            item.className = 'notif-item';
            item.innerHTML = `
                <div class="notif-header">
                    <span class="notif-priority ${data.priority}"></span>
                    <span class="notif-headline">${escapeHtml(data.headline)}</span>
                    <span class="notif-time">${data.time}</span>
                </div>
                <div class="notif-detail">${escapeHtml(data.detail)}</div>
                <span class="notif-rule">${data.rule}</span>
            `;

            // Prepend (newest first)
            notifList.insertBefore(item, notifList.firstChild);

            if (!panelOpen) {
                unseenCount++;
                updateBadge();
            }
        }

        function clearAlerts() {
            fetch('/api/alerts/clear', { method: 'POST' });
            notifList.innerHTML = `
                <div class="notif-empty" id="notif-empty">
                    <svg viewBox="0 0 24 24"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/></svg>
                    <span>No notifications yet</span>
                </div>`;
            unseenCount = 0;
            updateBadge();
        }

        function escapeHtml(text) {
            const d = document.createElement('div');
            d.textContent = text;
            return d.innerHTML;
        }

        // ---- Scoreboard ----
        function updateScoreboard(games) {
            const order = { 'in_progress': 0, 'scheduled': 1, 'final': 2 };
            games.sort((a, b) => (order[a.status] ?? 1) - (order[b.status] ?? 1));

            // Group games by sport
            const bySport = {};
            for (const g of games) {
                const sk = (g.sport_key || '').startsWith('soccer') ? 'soccer' : (g.sport_key || 'nba');
                if (!bySport[sk]) bySport[sk] = [];
                bySport[sk].push(g);
            }

            // Update each sport's scoreboard grid
            for (const sportKey of ['nba', 'ncaab', 'nfl', 'soccer']) {
                const grid = document.getElementById('scoreboard-' + sportKey);
                if (!grid) continue;

                const sportGames = bySport[sportKey] || [];
                grid.innerHTML = '';
                if (!sportGames.length) {
                    grid.innerHTML = '<div class="scoreboard-empty">No games right now</div>';
                    continue;
                }

                for (const g of sportGames) {
                    const isLive = g.status === 'in_progress';
                    const isScheduled = g.status === 'scheduled';

                    const statusTag = isLive
                        ? '<span class="game-status-tag live">Live</span>'
                        : isScheduled
                        ? '<span class="game-status-tag scheduled">Scheduled</span>'
                        : '<span class="game-status-tag final">Final</span>';

                    const awayWin = g.away_score > g.home_score;
                    const homeWin = g.home_score > g.away_score;
                    const awayClass = awayWin ? 'winning' : homeWin ? 'losing' : '';
                    const homeClass = homeWin ? 'winning' : awayWin ? 'losing' : '';

                    const card = document.createElement('div');
                    const statusClass = isLive ? 'live' : isScheduled ? 'scheduled' : 'final';
                    card.className = `game-card ${statusClass}`;
                    card.dataset.gameId = g.game_id;
                    card.innerHTML = `
                        <div class="game-status-bar">
                            ${statusTag}
                            <span class="game-clock">${escapeHtml(g.detail)}</span>
                        </div>
                        <div class="game-teams">
                            <div class="game-team-row ${awayClass}">
                                <div class="game-team-info">
                                    <span class="game-team-abbrev">${escapeHtml(g.away_abbrev)}</span>
                                    <span class="game-team-name">${escapeHtml(g.away_team)}</span>
                                </div>
                                <span class="game-team-score">${isScheduled ? '' : g.away_score}</span>
                            </div>
                            <div class="game-divider"></div>
                            <div class="game-team-row ${homeClass}">
                                <div class="game-team-info">
                                    <span class="game-team-abbrev">${escapeHtml(g.home_abbrev)}</span>
                                    <span class="game-team-name">${escapeHtml(g.home_team)}</span>
                                </div>
                                <span class="game-team-score">${isScheduled ? '' : g.home_score}</span>
                            </div>
                        </div>
                    `;
                    grid.appendChild(card);
                }
            }
        }

        // ---- Monitor toggle ----
        const monitorBtn = document.getElementById('monitor-btn');
        const monitorLabel = document.getElementById('monitor-label');
        const statusText = document.getElementById('status-text');

        function toggleMonitor() {
            const running = monitorBtn.classList.contains('running');
            fetch(running ? '/api/monitor/stop' : '/api/monitor/start', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    monitorBtn.classList.toggle('running', data.running);
                    monitorBtn.classList.toggle('stopped', !data.running);
                    monitorLabel.textContent = data.running ? 'Monitoring' : 'Stopped';
                });
        }

        function updateMonitorState(state) {
            if (!state) return;
            if (state.startsWith('waiting') || state.startsWith('idle')) {
                monitorBtn.classList.add('running');
                monitorBtn.classList.remove('stopped');
                // Capitalize first letter
                monitorLabel.textContent = state.charAt(0).toUpperCase() + state.slice(1);
            } else if (state === 'running') {
                monitorBtn.classList.add('running');
                monitorBtn.classList.remove('stopped');
                monitorLabel.textContent = 'Monitoring';
            } else if (state === 'stopped') {
                monitorBtn.classList.remove('running');
                monitorBtn.classList.add('stopped');
                monitorLabel.textContent = 'Stopped';
            }
        }

        // ---- SSE for real-time updates ----
        function connectSSE() {
            const source = new EventSource('/api/alerts/stream');

            source.onmessage = function(event) {
                const data = JSON.parse(event.data);

                if (data._type === 'status') {
                    // Update status bar
                    let text = '';
                    if (data.last_poll) {
                        text = `Last poll: ${data.last_poll}`;
                        if (data.games) {
                            text += ` | ${data.games.live || 0} live, ${data.games.scheduled || 0} scheduled`;
                        }
                    }
                    statusText.textContent = text;

                    // Update monitor badge state
                    if (data.state) {
                        updateMonitorState(data.state);
                    }

                    // Update scoreboard
                    if (data._games) {
                        updateScoreboard(data._games);
                    }
                } else if (data._type === 'alert') {
                    addNotification(data);
                }
            };

            source.onerror = function() {
                source.close();
                setTimeout(connectSSE, 3000);
            };
        }

        connectSSE();

        // ---- Sport tabs ----
        function switchTab(key) {
            document.querySelectorAll('.sport-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.sport-panel').forEach(p => p.classList.remove('active'));
            document.getElementById('tab-' + key).classList.add('active');
            document.getElementById('panel-' + key).classList.add('active');
        }

        // ---- Team grid toggles ----
        function toggleTeamGrid(sportKey) {
            const cb = document.getElementById(sportKey + '-all-teams');
            const grid = document.getElementById(sportKey + '-team-grid');
            if (cb && grid) {
                grid.style.opacity = cb.checked ? '0.3' : '1';
                grid.style.pointerEvents = cb.checked ? 'none' : 'auto';
            }
        }
        // Initialize all team grids
        {% for st in sport_tabs %}
        {% if st.teams %}toggleTeamGrid('{{ st.key }}');{% endif %}
        {% endfor %}

        // ---- Channel toggles ----
        const discordToggle = document.getElementById('discord-toggle');
        const discordUrl = document.getElementById('discord-url');
        discordToggle.addEventListener('change', () => {
            discordUrl.style.display = discordToggle.checked ? 'block' : 'none';
        });

        const telegramToggle = document.getElementById('telegram-toggle');
        const telegramFields = document.getElementById('telegram-fields');
        telegramToggle.addEventListener('change', () => {
            telegramFields.style.display = telegramToggle.checked ? 'block' : 'none';
        });

        document.querySelectorAll('.alert-row').forEach(row => {
            const toggle = row.querySelector('.toggle input');
            const settings = row.querySelector('.settings-row');
            if (toggle && settings) {
                function updateSettings() {
                    settings.style.opacity = toggle.checked ? '1' : '0.3';
                    settings.style.pointerEvents = toggle.checked ? 'auto' : 'none';
                }
                toggle.addEventListener('change', updateSettings);
                updateSettings();
            }
        });

        // ---- Player tracking ----
        const playerSearch = document.getElementById('player-search');
        const playerResults = document.getElementById('player-results');
        const trackedList = document.getElementById('tracked-players-list');

        playerSearch.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); searchPlayers(); } });

        function searchPlayers() {
            const q = playerSearch.value.trim();
            if (q.length < 2) return;

            playerResults.innerHTML = '<p style="color: #8b949e;">Searching...</p>';

            fetch('/api/players/search?q=' + encodeURIComponent(q))
                .then(r => r.json())
                .then(players => {
                    playerResults.innerHTML = '';
                    if (!players.length) {
                        playerResults.innerHTML = '<p style="color: #484f58;">No players found</p>';
                        return;
                    }
                    for (const p of players) {
                        const div = document.createElement('div');
                        div.className = 'player-result-item';
                        div.innerHTML = `
                            <div class="player-result-info">
                                <span class="player-result-name">${escapeHtml(p.name)}</span>
                                <span class="player-result-team">${escapeHtml(p.team)}</span>
                                <span class="player-result-sport">${escapeHtml(p.league || p.sport || '')}</span>
                            </div>
                            <button type="button" class="player-track-btn" onclick='trackPlayer(${JSON.stringify(p).replace(/'/g, "&#39;")})'>Track</button>
                        `;
                        playerResults.appendChild(div);
                    }
                })
                .catch(() => { playerResults.innerHTML = '<p style="color: #da3633;">Search failed</p>'; });
        }

        function trackPlayer(p) {
            fetch('/api/players/track', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(p),
            })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    const noMsg = document.getElementById('no-tracked-msg');
                    if (noMsg) noMsg.remove();

                    // Add to tracked list
                    const div = document.createElement('div');
                    div.className = 'tracked-player-item';
                    div.id = 'tracked-' + p.espn_id;
                    div.innerHTML = `
                        <div class="player-result-info">
                            <span class="player-result-name">${escapeHtml(p.name)}</span>
                            <span class="player-result-sport">${escapeHtml((p.league || '').toUpperCase())}</span>
                        </div>
                        <button type="button" class="player-remove-btn" onclick="untrackPlayer('${p.espn_id}')">Remove</button>
                    `;
                    trackedList.appendChild(div);
                    playerResults.innerHTML = '';
                    playerSearch.value = '';
                }
            });
        }

        function untrackPlayer(espnId) {
            fetch('/api/players/untrack', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ espn_id: espnId }),
            })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    const el = document.getElementById('tracked-' + espnId);
                    if (el) el.remove();
                }
            });
        }

        {% if saved %}
        const toast = document.getElementById('toast');
        toast.classList.add('show');
        setTimeout(() => toast.classList.remove('show'), 3000);
        {% endif %}
    </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    config = load_config()
    config = migrate_config(config)

    sports_cfg = config.get("sports", {})
    # Build per-sport context
    sport_tabs = []
    for key, meta in SPORT_REGISTRY.items():
        scfg = sports_cfg.get(key, {})
        sport_tabs.append({
            "key": key,
            "display_name": meta.display_name,
            "icon": meta.icon,
            "enabled": scfg.get("enabled", False),
            "teams": meta.teams,
            "teams_filter": set(scfg.get("teams_filter", [])),
            "alerts_cfg": scfg.get("alerts", {}),
            "leagues": scfg.get("leagues", []) if key == "soccer" else [],
        })

    with alert_lock:
        alerts_snapshot = list(alert_history)

    with games_lock:
        games_snapshot = list(games_data)

    status_order = {"in_progress": 0, "scheduled": 1, "final": 2}
    games_snapshot.sort(key=lambda g: status_order.get(g["status"], 1))

    return render_template_string(
        TEMPLATE,
        config=config,
        sport_tabs=sport_tabs,
        sport_registry=SPORT_REGISTRY,
        soccer_leagues=SOCCER_LEAGUES,
        soccer_teams=SOCCER_TEAMS,
        tracked_players=config.get("tracked_players", []),
        notif=config.get("notifications", {}),
        saved=request.args.get("saved"),
        alerts=alerts_snapshot,
        alert_count=len(alerts_snapshot),
        unseen_count=len(alerts_snapshot),
        monitor_running=monitor_running,
        monitor_status=monitor_status,
        games_list=games_snapshot,
    )


@app.route("/save", methods=["POST"])
def save():
    form = request.form

    # Build per-sport config
    sports = {}
    for sport_key in SPORT_REGISTRY:
        enabled = f"{sport_key}_enabled" in form
        teams_key = f"{sport_key}_teams"
        all_teams_key = f"{sport_key}_all_teams"
        all_teams = all_teams_key in form
        teams_filter = [] if all_teams else form.getlist(teams_key)

        sport_alerts = {}
        if sport_key == "nba":
            sport_alerts = {
                "close_game": {
                    "enabled": f"{sport_key}_close_game_enabled" in form,
                    "point_threshold": int(form.get(f"{sport_key}_close_game_threshold", 5)),
                    "minutes_remaining": float(form.get(f"{sport_key}_close_game_minutes", 4)),
                    "quarters": [4, 5, 6, 7],
                },
                "historic_scoring": {
                    "enabled": f"{sport_key}_historic_scoring_enabled" in form,
                    "points_threshold": int(form.get(f"{sport_key}_scoring_points", 50)),
                },
                "historic_stats": {"enabled": f"{sport_key}_historic_stats_enabled" in form},
                "blowout_comeback": {"enabled": f"{sport_key}_blowout_comeback_enabled" in form, "deficit_threshold": 20, "close_threshold": 5},
                "overtime": {"enabled": f"{sport_key}_overtime_enabled" in form},
                "goat_tracker": {"enabled": f"{sport_key}_goat_tracker_enabled" in form},
            }
        elif sport_key == "ncaab":
            sport_alerts = {
                "close_game": {"enabled": f"{sport_key}_close_game_enabled" in form, "point_threshold": 5, "minutes_remaining": 4.0, "quarters": [2, 3, 4, 5]},
                "historic_scoring": {"enabled": f"{sport_key}_historic_scoring_enabled" in form, "points_threshold": 40},
                "historic_stats": {"enabled": f"{sport_key}_historic_stats_enabled" in form},
                "upset_alert": {"enabled": f"{sport_key}_upset_alert_enabled" in form, "seed_difference": 5},
                "blowout_comeback": {"enabled": f"{sport_key}_blowout_comeback_enabled" in form},
                "overtime": {"enabled": f"{sport_key}_overtime_enabled" in form},
            }
        elif sport_key == "nfl":
            sport_alerts = {
                "close_game": {"enabled": f"{sport_key}_close_game_enabled" in form, "point_threshold": 7, "minutes_remaining": 4.0, "quarters": [4, 5, 6]},
                "high_scoring_qb": {"enabled": f"{sport_key}_high_scoring_qb_enabled" in form},
                "big_rushing_game": {"enabled": f"{sport_key}_big_rushing_game_enabled" in form},
                "blowout_comeback": {"enabled": f"{sport_key}_blowout_comeback_enabled" in form},
                "overtime": {"enabled": f"{sport_key}_overtime_enabled" in form},
            }
        elif sport_key == "soccer":
            sport_alerts = {
                "late_goal": {"enabled": f"{sport_key}_late_goal_enabled" in form},
                "equalizer": {"enabled": f"{sport_key}_equalizer_enabled" in form},
                "comeback": {"enabled": f"{sport_key}_comeback_enabled" in form},
                "red_card": {"enabled": f"{sport_key}_red_card_enabled" in form},
                "extra_time": {"enabled": f"{sport_key}_extra_time_enabled" in form},
            }

        sport_cfg = {
            "enabled": enabled,
            "teams_filter": teams_filter,
            "alerts": sport_alerts,
        }
        if sport_key == "soccer":
            sport_cfg["leagues"] = form.getlist("soccer_leagues") or ["eng.1"]

        sports[sport_key] = sport_cfg

    # Preserve tracked_players from existing config
    existing = load_config()
    existing = migrate_config(existing)

    config = {
        "polling_interval_seconds": max(10, int(form.get("polling_interval", 30))),
        "sports": sports,
        "tracked_players": existing.get("tracked_players", []),
        "notifications": {
            "webapp": {"enabled": "webapp_enabled" in form},
            "console": {"enabled": "console_enabled" in form},
            "desktop": {"enabled": "desktop_enabled" in form},
            "discord": {
                "enabled": "discord_enabled" in form,
                "webhook_url": form.get("discord_webhook", ""),
            },
            "telegram": {
                "enabled": "telegram_enabled" in form,
                "bot_token": form.get("telegram_bot_token", ""),
                "chat_id": form.get("telegram_chat_id", ""),
            },
        },
    }

    save_config(config)

    if monitor_running:
        stop_monitor()
        time.sleep(1)
        start_monitor()

    return redirect(url_for("index", saved="1"))


@app.route("/api/monitor/start", methods=["POST"])
def api_start_monitor():
    start_monitor()
    return jsonify({"running": True})


@app.route("/api/monitor/stop", methods=["POST"])
def api_stop_monitor():
    stop_monitor()
    return jsonify({"running": False})


@app.route("/api/alerts/clear", methods=["POST"])
def api_clear_alerts():
    with alert_lock:
        alert_history.clear()
    return jsonify({"ok": True})


@app.route("/api/alerts/stream")
def api_alert_stream():
    def stream():
        q: Queue = Queue()
        with sse_lock:
            sse_clients.append(q)
        try:
            while True:
                data = q.get()
                yield f"data: {json.dumps(data)}\n\n"
        except GeneratorExit:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/config")
def api_config():
    return jsonify(load_config())


@app.route("/api/players/search")
def api_player_search():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])

    import asyncio
    async def _search():
        espn = ESPNClient()
        try:
            return await espn.search_players(query, limit=10)
        finally:
            await espn.close()

    results = asyncio.run(_search())
    return jsonify(results)


@app.route("/api/players/track", methods=["POST"])
def api_track_player():
    data = request.get_json()
    if not data or not data.get("espn_id"):
        return jsonify({"error": "espn_id required"}), 400

    config = load_config()
    config = migrate_config(config)
    tracked = config.get("tracked_players", [])

    # Don't add duplicates
    if any(p["espn_id"] == data["espn_id"] for p in tracked):
        return jsonify({"ok": True, "message": "already tracked"})

    player_entry = {
        "name": data.get("name", ""),
        "espn_id": data["espn_id"],
        "sport": data.get("sport", ""),
        "league": data.get("league", ""),
        "thresholds": data.get("thresholds", {}),
        "milestones": [],
    }
    tracked.append(player_entry)
    config["tracked_players"] = tracked
    save_config(config)

    return jsonify({"ok": True, "player": player_entry})


@app.route("/api/players/untrack", methods=["POST"])
def api_untrack_player():
    data = request.get_json()
    if not data or not data.get("espn_id"):
        return jsonify({"error": "espn_id required"}), 400

    config = load_config()
    config = migrate_config(config)
    tracked = config.get("tracked_players", [])
    config["tracked_players"] = [p for p in tracked if p["espn_id"] != data["espn_id"]]
    save_config(config)

    return jsonify({"ok": True})


@app.route("/api/players/tracked")
def api_tracked_players():
    config = load_config()
    config = migrate_config(config)
    return jsonify(config.get("tracked_players", []))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def find_open_port(start: int = 5050, end: int = 5100) -> int:
    import socket
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No open port found in range {start}-{end}")


def main():
    import os
    port = int(os.environ.get("PORT", 0)) or find_open_port()
    start_monitor()
    print(f"\n  Open http://localhost:{port} to configure notifications\n")
    print("  Monitor started automatically — watching for games.\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
