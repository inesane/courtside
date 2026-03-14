#!/usr/bin/env python3
"""Web UI for configuring sports notifications with built-in live monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any

import yaml
from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for

from alerts.base import Alert, AlertRule
from alerts.engine import AlertEngine
from alerts.nba.rules import (
    BlowoutComebackRule,
    CloseGameRule,
    HistoricScoringRule,
    HistoricStatLineRule,
    OvertimeRule,
)
from config import get_alert_config
from notifications.base import Notifier
from notifications.console import ConsoleNotifier
from notifications.desktop import DesktopNotifier
from notifications.discord import DiscordNotifier
from notifications.telegram import TelegramNotifier
from sports.espn import ESPNClient
from sports.nba.provider import NBAProvider

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

NBA_TEAMS = [
    ("ATL", "Atlanta Hawks"),
    ("BOS", "Boston Celtics"),
    ("BKN", "Brooklyn Nets"),
    ("CHA", "Charlotte Hornets"),
    ("CHI", "Chicago Bulls"),
    ("CLE", "Cleveland Cavaliers"),
    ("DAL", "Dallas Mavericks"),
    ("DEN", "Denver Nuggets"),
    ("DET", "Detroit Pistons"),
    ("GS", "Golden State Warriors"),
    ("HOU", "Houston Rockets"),
    ("IND", "Indiana Pacers"),
    ("LAC", "LA Clippers"),
    ("LAL", "Los Angeles Lakers"),
    ("MEM", "Memphis Grizzlies"),
    ("MIA", "Miami Heat"),
    ("MIL", "Milwaukee Bucks"),
    ("MIN", "Minnesota Timberwolves"),
    ("NO", "New Orleans Pelicans"),
    ("NY", "New York Knicks"),
    ("OKC", "Oklahoma City Thunder"),
    ("ORL", "Orlando Magic"),
    ("PHI", "Philadelphia 76ers"),
    ("PHX", "Phoenix Suns"),
    ("POR", "Portland Trail Blazers"),
    ("SAC", "Sacramento Kings"),
    ("SA", "San Antonio Spurs"),
    ("TOR", "Toronto Raptors"),
    ("UTAH", "Utah Jazz"),
    ("WSH", "Washington Wizards"),
]


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


def build_rules(config: dict, engine: AlertEngine) -> list[AlertRule]:
    rules: list[AlertRule] = []

    cfg = get_alert_config(config, "close_game")
    if cfg.get("enabled", True):
        rules.append(CloseGameRule(
            point_threshold=cfg.get("point_threshold", 5),
            minutes_remaining=cfg.get("minutes_remaining", 4.0),
            quarters=cfg.get("quarters", [4, 5, 6, 7]),
        ))

    cfg = get_alert_config(config, "historic_scoring")
    if cfg.get("enabled", True):
        rules.append(HistoricScoringRule(thresholds=cfg.get("thresholds")))

    cfg = get_alert_config(config, "historic_stats")
    if cfg.get("enabled", True):
        rules.append(HistoricStatLineRule())

    cfg = get_alert_config(config, "blowout_comeback")
    if cfg.get("enabled", True):
        rules.append(BlowoutComebackRule(
            deficit_threshold=cfg.get("deficit_threshold", 20),
            close_threshold=cfg.get("close_threshold", 5),
            engine=engine,
        ))

    cfg = get_alert_config(config, "overtime")
    if cfg.get("enabled", True):
        rules.append(OvertimeRule())

    return rules


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


def _serialize_games(games) -> list[dict[str, Any]]:
    serialized = []
    for g in games:
        serialized.append({
            "game_id": g.game_id,
            "status": g.status,
            "home_team": g.home_team,
            "away_team": g.away_team,
            "home_abbrev": g.home_abbrev,
            "away_abbrev": g.away_abbrev,
            "home_score": g.home_score,
            "away_score": g.away_score,
            "period": g.period,
            "clock": g.clock,
            "detail": g.detail,
        })
    return serialized


async def _poll_loop() -> None:
    global monitor_running, monitor_status

    config = load_config()
    polling_interval = config.get("polling_interval_seconds", 30)
    teams_filter = set(config.get("teams_filter", []))

    espn = ESPNClient()
    provider = NBAProvider(espn)

    engine = AlertEngine(rules=[])
    engine.rules = build_rules(config, engine)
    notifiers = build_notifiers(config)

    logger.info("Monitor started | rules=%s", [r.name for r in engine.rules])

    try:
        while monitor_running:
            try:
                games = await provider.get_games()
                live = [g for g in games if g.status == "in_progress"]
                scheduled = [g for g in games if g.status == "scheduled"]
                final = [g for g in games if g.status == "final"]

                monitor_status["last_poll"] = datetime.now().strftime("%H:%M:%S")
                monitor_status["games"] = {
                    "live": len(live), "scheduled": len(scheduled), "final": len(final),
                }

                serialized = _serialize_games(games)
                with games_lock:
                    games_data.clear()
                    games_data.extend(serialized)

                _broadcast_status({**monitor_status, "_games": serialized})

                for game in live:
                    if teams_filter and not (
                        game.home_abbrev in teams_filter or game.away_abbrev in teams_filter
                    ):
                        continue

                    try:
                        await provider.enrich_box_score(game)
                    except Exception:
                        logger.warning("Box score fetch failed for %s", game.game_id)

                    webapp_enabled = config.get("notifications", {}).get("webapp", {}).get("enabled", True)

                    fired = engine.evaluate(game)
                    for a in fired:
                        alert_dict = {
                            "_type": "alert",
                            "id": len(alert_history),
                            "rule": a.rule_name,
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

                # Smart sleep based on game state
                secs_until = _seconds_until_first_game(games)

                if live:
                    # Games are live — use normal fast polling
                    sleep_secs = polling_interval
                elif secs_until is not None and secs_until > 0:
                    # Games scheduled but not started — sleep until 5 min before tipoff
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
                        # Close to tipoff, poll normally
                        sleep_secs = polling_interval
                else:
                    # All games final or no games today — check back every 30 min
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
    <title>Sports Notifications</title>
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

    <!-- Main content -->
    <div class="container">
        <p class="subtitle">Configure which games and events you want to be notified about.</p>

        <!-- Live Scoreboard -->
        <div class="scoreboard-section" id="scoreboard-section">
            <h2><span class="icon">&#127941;</span> Today's Games</h2>
            <div class="scoreboard-grid" id="scoreboard-grid">
                {% if games_list %}
                    {% for g in games_list %}
                    <div class="game-card {{ 'live' if g.status == 'in_progress' else 'scheduled' if g.status == 'scheduled' else 'final' }}" data-game-id="{{ g.game_id }}">
                        <div class="game-status-bar">
                            {% if g.status == 'in_progress' %}
                                <span class="game-status-tag live">Live</span>
                                <span class="game-clock">{{ g.detail }}</span>
                            {% elif g.status == 'scheduled' %}
                                <span class="game-status-tag scheduled">Scheduled</span>
                                <span class="game-clock">{{ g.detail }}</span>
                            {% else %}
                                <span class="game-status-tag final">Final</span>
                                <span class="game-clock">{{ g.detail }}</span>
                            {% endif %}
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
                    {% endfor %}
                {% else %}
                    <div class="scoreboard-empty">
                        Start monitoring to see today's games
                    </div>
                {% endif %}
            </div>
        </div>

        <form method="POST" action="/save" id="config-form">

            <!-- Teams -->
            <div class="card">
                <h2><span class="icon">&#127936;</span> Teams</h2>

                <div class="all-teams-toggle">
                    <input type="checkbox" id="all-teams" name="all_teams"
                        {{ 'checked' if not config.get('teams_filter') }}>
                    <label for="all-teams">Follow all teams</label>
                </div>

                <div class="team-grid" id="team-grid">
                    {% for abbrev, name in teams %}
                    <label class="team-item">
                        <input type="checkbox" name="teams" value="{{ abbrev }}"
                            {{ 'checked' if abbrev in selected_teams }}>
                        <span class="abbrev">{{ abbrev }}</span>
                        <span class="name">{{ name }}</span>
                    </label>
                    {% endfor %}
                </div>
            </div>

            <!-- Alerts -->
            <div class="card">
                <h2><span class="icon">&#128276;</span> Notifications</h2>

                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">Close Games</div>
                        <div class="alert-desc">Games within a few points near the end of the 4th quarter or overtime</div>
                        <div class="settings-row" id="close-game-settings">
                            <label>Point margin &le;</label>
                            <input type="number" name="close_game_threshold" min="1" max="20"
                                value="{{ alerts_cfg.close_game.get('point_threshold', 5) }}">
                            <label>Time left &le;</label>
                            <input type="number" name="close_game_minutes" min="1" max="12" step="0.5"
                                value="{{ alerts_cfg.close_game.get('minutes_remaining', 4) }}">
                            <span>min</span>
                        </div>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="close_game_enabled"
                            {{ 'checked' if alerts_cfg.close_game.get('enabled', true) }}>
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">Historic Scoring</div>
                        <div class="alert-desc">Player on pace for a massive scoring game</div>
                        <div class="settings-row" id="scoring-settings">
                            <label>By halftime &ge;</label>
                            <input type="number" name="scoring_halftime" min="20" max="60"
                                value="{{ scoring_halftime }}">
                            <label>By 3rd Q &ge;</label>
                            <input type="number" name="scoring_third" min="30" max="80"
                                value="{{ scoring_third }}">
                            <span>pts</span>
                        </div>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="historic_scoring_enabled"
                            {{ 'checked' if alerts_cfg.historic_scoring.get('enabled', true) }}>
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">Historic Stat Lines</div>
                        <div class="alert-desc">Players putting up huge numbers in rebounds, assists, steals, or blocks</div>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="historic_stats_enabled"
                            {{ 'checked' if alerts_cfg.historic_stats.get('enabled', true) }}>
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">Blowout Comebacks</div>
                        <div class="alert-desc">A team that was down big is making it a game again</div>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="blowout_comeback_enabled"
                            {{ 'checked' if alerts_cfg.blowout_comeback.get('enabled', false) }}>
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">Overtime</div>
                        <div class="alert-desc">Games that go to overtime</div>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="overtime_enabled"
                            {{ 'checked' if alerts_cfg.overtime.get('enabled', true) }}>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <!-- Notification Channels -->
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
        const scoreboardGrid = document.getElementById('scoreboard-grid');

        function updateScoreboard(games) {
            // Sort: live first, then scheduled, then final
            const order = { 'in_progress': 0, 'scheduled': 1, 'final': 2 };
            games.sort((a, b) => (order[a.status] ?? 1) - (order[b.status] ?? 1));

            scoreboardGrid.innerHTML = '';
            if (!games.length) {
                scoreboardGrid.innerHTML = '<div class="scoreboard-empty">No games today</div>';
                return;
            }

            for (const g of games) {
                const isLive = g.status === 'in_progress';
                const isScheduled = g.status === 'scheduled';
                const isFinal = g.status === 'final';

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
                scoreboardGrid.appendChild(card);
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

        // ---- Existing toggles ----
        const allTeamsCheckbox = document.getElementById('all-teams');
        const teamGrid = document.getElementById('team-grid');

        function updateTeamGrid() {
            teamGrid.style.opacity = allTeamsCheckbox.checked ? '0.3' : '1';
            teamGrid.style.pointerEvents = allTeamsCheckbox.checked ? 'none' : 'auto';
        }

        allTeamsCheckbox.addEventListener('change', updateTeamGrid);
        updateTeamGrid();

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
def _get_scoring_threshold(alerts_cfg: dict, period: int, default: int) -> int:
    thresholds = alerts_cfg.get("historic_scoring", {}).get("thresholds", [])
    for t in thresholds:
        if t.get("period") == period:
            return t.get("points", default)
    return default


@app.route("/")
def index():
    config = load_config()
    alerts_cfg = config.get("alerts", {})
    for key in ("close_game", "historic_scoring", "historic_stats", "blowout_comeback", "overtime"):
        if key not in alerts_cfg:
            alerts_cfg[key] = {"enabled": key not in ("blowout_comeback",)}

    selected_teams = set(config.get("teams_filter", []))

    with alert_lock:
        alerts_snapshot = list(alert_history)

    with games_lock:
        games_snapshot = list(games_data)

    # Sort: live first, then scheduled, then final
    status_order = {"in_progress": 0, "scheduled": 1, "final": 2}
    games_snapshot.sort(key=lambda g: status_order.get(g["status"], 1))

    return render_template_string(
        TEMPLATE,
        config=config,
        teams=NBA_TEAMS,
        selected_teams=selected_teams,
        alerts_cfg=alerts_cfg,
        notif=config.get("notifications", {}),
        scoring_halftime=_get_scoring_threshold(alerts_cfg, 2, 40),
        scoring_third=_get_scoring_threshold(alerts_cfg, 3, 50),
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

    all_teams = "all_teams" in form
    teams_filter = [] if all_teams else form.getlist("teams")

    config = {
        "polling_interval_seconds": max(10, int(form.get("polling_interval", 30))),
        "teams_filter": teams_filter,
        "alerts": {
            "close_game": {
                "enabled": "close_game_enabled" in form,
                "point_threshold": int(form.get("close_game_threshold", 5)),
                "minutes_remaining": float(form.get("close_game_minutes", 4)),
                "quarters": [4, 5, 6, 7],
            },
            "historic_scoring": {
                "enabled": "historic_scoring_enabled" in form,
                "thresholds": [
                    {"period": 2, "points": int(form.get("scoring_halftime", 40))},
                    {"period": 3, "points": int(form.get("scoring_third", 50))},
                ],
            },
            "historic_stats": {
                "enabled": "historic_stats_enabled" in form,
            },
            "blowout_comeback": {
                "enabled": "blowout_comeback_enabled" in form,
                "deficit_threshold": 20,
                "close_threshold": 5,
            },
            "overtime": {
                "enabled": "overtime_enabled" in form,
            },
        },
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

    # Restart monitor if running so it picks up new config
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
    port = find_open_port()
    print(f"\n  Open http://localhost:{port} to configure notifications\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
