#!/usr/bin/env python3
"""Web UI for configuring sports notifications with built-in live monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from authlib.integrations.flask_client import OAuth

from database import get_all_user_configs, get_or_create_google_user, init_db, load_user_config, save_user_config, milestone_already_sent, mark_milestone_sent, save_push_subscription, delete_push_subscription, get_push_subscriptions_for_user, get_all_push_subscriptions

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
from alerts.milestones import run_milestone_check
from sports.espn import ESPNClient
from sports.nba.provider import NBAProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webapp")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS = {"sub": "mailto:admin@courtside.app"}

_PUBLIC_ROUTES = {"login", "auth_google", "auth_google_callback", "static", "manifest_json", "service_worker"}


@app.before_request
def _require_login() -> None:
    if request.endpoint in _PUBLIC_ROUTES:
        return
    if "user_id" not in session:
        return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# Shared state for the background monitor
# ---------------------------------------------------------------------------
# Per-user alert history and SSE clients, keyed by user_id
alert_history: "dict[str, list[dict[str, Any]]]" = {}
alert_lock = threading.Lock()
sse_clients: "dict[str, list[Queue]]" = {}
sse_lock = threading.Lock()
monitor_thread: threading.Thread | None = None
monitor_running = False
monitor_status: dict[str, Any] = {"state": "stopped", "last_poll": None, "games": {}}
games_data: list[dict[str, Any]] = []
games_lock = threading.Lock()
_milestones_checked_today: str = ""  # date string to track once-per-day check

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
# Config helpers (thin wrappers so callers that pass a user_id work uniformly)
# ---------------------------------------------------------------------------
def load_config(user_id: str | None = None) -> dict[str, Any]:
    if user_id is None:
        user_id = session.get("user_id", "")
    return load_user_config(user_id) if user_id else {}


def save_config(config: dict[str, Any], user_id: str | None = None) -> None:
    if user_id is None:
        user_id = session.get("user_id", "")
    if user_id:
        save_user_config(user_id, config)


# ---------------------------------------------------------------------------
# Monitor background thread
# ---------------------------------------------------------------------------
def _push_to_user(user_id: str, data: dict[str, Any]) -> None:
    with sse_lock:
        queues = sse_clients.get(user_id, [])
        dead: list[Queue] = []
        for q in queues:
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            queues.remove(q)


def _broadcast_status(status: dict[str, Any]) -> None:
    """Push monitor status update to all connected users."""
    with sse_lock:
        for queues in sse_clients.values():
            dead: list[Queue] = []
            for q in queues:
                try:
                    q.put_nowait({"_type": "status", **status})
                except Exception:
                    dead.append(q)
            for q in dead:
                queues.remove(q)


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
        rules.append(HistoricScoringRule(points_threshold=cfg.get("points_threshold", 50)))

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


def _user_wants_alert(cfg: dict[str, Any], alert: Alert, home_abbrev: str, away_abbrev: str) -> bool:
    """Return True if this user's config means they should receive this alert."""
    user_filter = set(cfg.get("teams_filter", []))
    if user_filter and home_abbrev and not (
        home_abbrev in user_filter or away_abbrev in user_filter
    ):
        return False

    rule_cfg = cfg.get("alerts", {}).get(alert.rule_name, {})
    if not rule_cfg.get("enabled", True):
        return False

    # Per-user threshold checks using alert metadata
    meta = alert.metadata or {}
    if alert.rule_name == "close_game":
        user_threshold = rule_cfg.get("point_threshold", 5)
        user_minutes = rule_cfg.get("minutes_remaining", 4)
        if "score_diff" in meta and meta["score_diff"] > user_threshold:
            return False
        if "time_left_seconds" in meta and meta["time_left_seconds"] > user_minutes * 60:
            return False
    elif alert.rule_name == "historic_scoring":
        user_pts = rule_cfg.get("points_threshold", 50)
        if "points" in meta and meta["points"] < user_pts:
            return False

    return True


async def _notify_users(alert: Alert, home_abbrev: str, away_abbrev: str) -> None:
    """Send alert to every user whose teams filter and alert rules match."""
    now_str = datetime.now().strftime("%H:%M:%S")
    for uc in get_all_user_configs():
        user_id = uc["user_id"]
        cfg = uc["config"]
        if not _user_wants_alert(cfg, alert, home_abbrev, away_abbrev):
            continue

        # In-app SSE push
        alert_dict = {
            "_type": "alert",
            "rule": alert.rule_name,
            "headline": alert.headline,
            "detail": alert.detail,
            "priority": alert.priority,
            "time": now_str,
        }
        with alert_lock:
            user_alerts = alert_history.setdefault(user_id, [])
            alert_dict["id"] = len(user_alerts)
            user_alerts.append(alert_dict)
        _push_to_user(user_id, alert_dict)

        # External notifications (Discord, Telegram, etc.)
        for n in build_notifiers(cfg):
            try:
                await n.send(alert)
            except Exception:
                logger.warning("Notifier %s failed for user %s", type(n).__name__, user_id)

        # Web push
        if VAPID_PRIVATE_KEY:
            _send_web_push_to_user(user_id, alert)


def _send_web_push_to_user(user_id: str, alert: Alert) -> None:
    from pywebpush import webpush, WebPushException
    subscriptions = get_push_subscriptions_for_user(user_id)
    payload = json.dumps({"headline": alert.headline, "detail": alert.detail, "priority": alert.priority})
    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
            )
        except WebPushException as e:
            if e.response is not None and e.response.status_code == 410:
                # Subscription expired — remove it
                delete_push_subscription(sub["endpoint"])
            else:
                logger.warning("Web push failed for user %s: %s", user_id, e)
        except Exception as e:
            logger.warning("Web push failed for user %s: %s", user_id, e)


def _build_permissive_rules(engine: AlertEngine) -> list[AlertRule]:
    """Build rules using the most permissive thresholds across all user configs.
    This ensures the engine fires whenever *any* user would want an alert.
    Per-user threshold filtering happens in _user_wants_alert."""
    user_configs = get_all_user_configs()
    if not user_configs:
        return build_rules({}, engine)

    # Most permissive = highest point_threshold, highest minutes_remaining, lowest points_threshold
    max_point_threshold = max(
        uc["config"].get("alerts", {}).get("close_game", {}).get("point_threshold", 5)
        for uc in user_configs
    )
    max_minutes = max(
        uc["config"].get("alerts", {}).get("close_game", {}).get("minutes_remaining", 4)
        for uc in user_configs
    )
    min_scoring_threshold = min(
        uc["config"].get("alerts", {}).get("historic_scoring", {}).get("points_threshold", 50)
        for uc in user_configs
    )

    permissive_config = {
        "alerts": {
            "close_game": {"enabled": True, "point_threshold": max_point_threshold, "minutes_remaining": max_minutes, "quarters": [4, 5, 6, 7]},
            "historic_scoring": {"enabled": True, "points_threshold": min_scoring_threshold},
            "historic_stats": {"enabled": True},
            "blowout_comeback": {"enabled": True, "deficit_threshold": 20, "close_threshold": 5},
            "overtime": {"enabled": True},
        }
    }
    return build_rules(permissive_config, engine)


async def _poll_loop() -> None:
    global monitor_running, monitor_status

    polling_interval = 30  # fixed system default; users control notification routing

    espn = ESPNClient()
    provider = NBAProvider(espn)

    engine = AlertEngine(rules=[])
    engine.rules = _build_permissive_rules(engine)

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

                # Refresh rules each cycle so new users / config changes are picked up
                engine.rules = _build_permissive_rules(engine)

                for game in live:
                    try:
                        await provider.enrich_box_score(game)
                    except Exception:
                        logger.warning("Box score fetch failed for %s", game.game_id)

                    fired = engine.evaluate(game)
                    for a in fired:
                        await _notify_users(a, game.home_abbrev, game.away_abbrev)

                for game in final:
                    engine.evaluate(game)

                # GOAT Tracker: check milestones once per day after games finish
                global _milestones_checked_today
                today_str = datetime.now().strftime("%Y-%m-%d")

                any_goat_enabled = any(
                    uc["config"].get("alerts", {}).get("goat_tracker", {}).get("enabled", True)
                    for uc in get_all_user_configs()
                ) if get_all_user_configs() else True

                if any_goat_enabled and final and not live and _milestones_checked_today != today_str:
                    _milestones_checked_today = today_str
                    logger.info("Running GOAT Tracker milestone check...")
                    try:
                        milestone_alerts = await run_milestone_check()
                        for ma in milestone_alerts:
                            alert_key = f"{ma['stat']}:{ma['record_holder']}:{ma.get('current', '')}"
                            if milestone_already_sent(alert_key):
                                continue
                            alert_obj = Alert(
                                rule_name="goat_tracker",
                                game_id="milestone",
                                headline=ma["headline"],
                                detail=ma["detail"],
                                priority=ma["priority"],
                                dedup_key=("milestone", ma["stat"], ma["record_holder"]),
                            )
                            await _notify_users(alert_obj, "", "")
                            mark_milestone_sent(alert_key)
                    except Exception:
                        logger.exception("GOAT Tracker check failed")

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
    <title>Courtside</title>
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#161b22">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Courtside">
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

        .channel-actions {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .btn-info {
            width: 22px;
            height: 22px;
            border-radius: 50%;
            background: #21262d;
            border: 1px solid #30363d;
            color: #8b949e;
            font-size: 12px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }

        .btn-info:hover { background: #30363d; color: #e1e4e8; }

        .btn-test {
            font-size: 11px;
            padding: 3px 10px;
            background: #21262d;
            border: 1px solid #30363d;
            border-radius: 6px;
            color: #8b949e;
            cursor: pointer;
            white-space: nowrap;
        }

        .btn-test:hover { background: #30363d; color: #e1e4e8; }
        .btn-test.sending { opacity: 0.5; pointer-events: none; }

        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.6);
            z-index: 200;
            align-items: center;
            justify-content: center;
        }

        .modal-overlay.open { display: flex; }

        .modal {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 28px;
            max-width: 420px;
            width: 90%;
        }

        .modal h3 { font-size: 16px; margin-bottom: 16px; }

        .modal ol { padding-left: 18px; color: #8b949e; font-size: 13px; line-height: 2; }

        .modal ol li { margin-bottom: 4px; }

        .modal code {
            background: #0d1117;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 12px;
            color: #79c0ff;
        }

        .modal-close {
            margin-top: 20px;
            padding: 8px 20px;
            background: #21262d;
            border: 1px solid #30363d;
            border-radius: 8px;
            color: #e1e4e8;
            cursor: pointer;
            font-size: 13px;
        }

        .modal-close:hover { background: #30363d; }

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
            <h1>Courtside</h1>
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
            <span style="font-size:13px; color:#8b949e;">{{ user_name }}</span>
            <a href="/logout" style="font-size:12px; color:#8b949e; text-decoration:none; padding:4px 10px; border:1px solid #30363d; border-radius:6px;">Sign out</a>
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
                            <input type="number" name="close_game_minutes" min="1" max="12" step="1"
                                value="{{ alerts_cfg.close_game.get('minutes_remaining', 4) | int }}">
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
                        <div class="alert-desc">Player hits a massive point total at any point in the game</div>
                        <div class="settings-row" id="scoring-settings">
                            <label>Points &ge;</label>
                            <input type="number" name="scoring_points" min="30" max="80"
                                value="{{ alerts_cfg.historic_scoring.get('points_threshold', 50) }}">
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

                <div class="alert-row">
                    <div class="alert-info">
                        <div class="alert-name">GOAT Tracker</div>
                        <div class="alert-desc">Track LeBron's career milestones (records, all-time rankings)</div>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="goat_tracker_enabled"
                            {{ 'checked' if alerts_cfg.get('goat_tracker', {}).get('enabled', true) }}>
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
                        <span class="channel-name">Push Notifications</span>
                        <span class="channel-desc">Native alerts on this device — works on phone and desktop</span>
                    </div>
                    <div class="channel-actions">
                        <button type="button" class="btn-test" id="push-test-btn" onclick="testNotification('push')">Test</button>
                        <button type="button" class="btn-test" id="push-btn" onclick="togglePushSubscription()" style="padding: 3px 12px;">
                            Enable
                        </button>
                    </div>
                </div>

                {# <div class="channel-row">
                    <div class="channel-info">
                        <span class="channel-name">Console</span>
                        <span class="channel-desc">Print alerts to the terminal</span>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="console_enabled"
                            {{ 'checked' if notif.get('console', {}).get('enabled', true) }}>
                        <span class="slider"></span>
                    </label>
                </div> #}

                {# <div class="channel-row">
                    <div class="channel-info">
                        <span class="channel-name">Desktop Notifications</span>
                        <span class="channel-desc">OS-level push notifications</span>
                    </div>
                    <label class="toggle">
                        <input type="checkbox" name="desktop_enabled"
                            {{ 'checked' if notif.get('desktop', {}).get('enabled', false) }}>
                        <span class="slider"></span>
                    </label>
                </div> #}

                <div class="channel-row" style="flex-wrap: wrap;">
                    <div class="channel-info" style="flex: 1;">
                        <span class="channel-name">Discord</span>
                    </div>
                    <div class="channel-actions">
                        <button type="button" class="btn-info" onclick="document.getElementById('discord-modal').classList.add('open')">?</button>
                        <button type="button" class="btn-test" id="discord-test-btn" onclick="testNotification('discord')">Test</button>
                        <label class="toggle">
                            <input type="checkbox" name="discord_enabled" id="discord-toggle"
                                {{ 'checked' if notif.get('discord', {}).get('enabled', false) }}>
                            <span class="slider"></span>
                        </label>
                    </div>
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
                    <div class="channel-actions">
                        <button type="button" class="btn-info" onclick="document.getElementById('telegram-modal').classList.add('open')">?</button>
                        <button type="button" class="btn-test" id="telegram-test-btn" onclick="testNotification('telegram')">Test</button>
                        <label class="toggle">
                            <input type="checkbox" name="telegram_enabled" id="telegram-toggle"
                                {{ 'checked' if notif.get('telegram', {}).get('enabled', false) }}>
                            <span class="slider"></span>
                        </label>
                    </div>
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


            <button type="submit" class="btn-save">Save Configuration</button>
        </form>
    </div>

    <!-- Discord info modal -->
    <div class="modal-overlay" id="discord-modal">
        <div class="modal">
            <h3>Setting up Discord notifications</h3>
            <ol>
                <li>Open Discord and go to your server</li>
                <li>Click <strong>Edit Channel</strong> on the channel you want alerts in</li>
                <li>Go to <strong>Integrations</strong> → <strong>Webhooks</strong> → <strong>New Webhook</strong></li>
                <li>Give it a name, then click <strong>Copy Webhook URL</strong></li>
                <li>Paste the URL into the Discord field above and save</li>
            </ol>
            <button class="modal-close" onclick="document.getElementById('discord-modal').classList.remove('open')">Close</button>
        </div>
    </div>

    <!-- Telegram info modal -->
    <div class="modal-overlay" id="telegram-modal">
        <div class="modal">
            <h3>Setting up Telegram notifications</h3>
            <ol>
                <li>Open Telegram and search for <code>@BotFather</code></li>
                <li>Send <code>/newbot</code> and follow the prompts to create a bot</li>
                <li>BotFather gives you a <strong>bot token</strong> — copy it</li>
                <li>Start a chat with your new bot and send it any message</li>
                <li>Open <code>https://api.telegram.org/botYOUR_TOKEN/getUpdates</code> in your browser</li>
                <li>Find <code>"chat":{"id": ...}</code> — that number is your <strong>Chat ID</strong></li>
                <li>Paste both the token and chat ID above and save</li>
            </ol>
            <button class="modal-close" onclick="document.getElementById('telegram-modal').classList.remove('open')">Close</button>
        </div>
    </div>

    <div class="toast" id="toast">Configuration saved!</div>

    <script>
        // ---- PWA / Web Push ----
        let swRegistration = null;
        let pushSubscription = null;

        async function initPush() {
            if (!('serviceWorker' in navigator)) {
                console.warn('Service workers not supported');
                return;
            }
            if (!('PushManager' in window)) {
                console.warn('PushManager not supported');
                return;
            }
            try {
                swRegistration = await navigator.serviceWorker.register('/sw.js');
                await navigator.serviceWorker.ready;
                pushSubscription = await swRegistration.pushManager.getSubscription();
                updatePushButton();
            } catch(e) {
                console.warn('SW registration failed:', e);
                swRegistration = null;
            }
        }

        function updatePushButton() {
            const btn = document.getElementById('push-btn');
            if (!btn) return;
            if (pushSubscription) {
                btn.textContent = 'Enabled ✓';
                btn.style.color = '#3fb950';
                btn.style.borderColor = '#3fb950';
            } else {
                btn.textContent = 'Enable';
                btn.style.color = '';
                btn.style.borderColor = '';
            }
        }

        async function togglePushSubscription() {
            if (!('serviceWorker' in navigator)) {
                alert('Service workers are not supported in this browser.');
                return;
            }
            if (!('PushManager' in window)) {
                alert('Push notifications are not supported in this browser.');
                return;
            }
            if (!swRegistration) {
                await initPush();
                if (!swRegistration) {
                    alert('Could not register service worker. Make sure you are on HTTPS and try again.');
                    return;
                }
            }
            const btn = document.getElementById('push-btn');
            btn.textContent = '...';
            btn.disabled = true;
            try {
                if (pushSubscription) {
                    await fetch('/api/push/unsubscribe', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({endpoint: pushSubscription.endpoint})
                    });
                    await pushSubscription.unsubscribe();
                    pushSubscription = null;
                } else {
                    const keyResp = await fetch('/api/push/vapid-public-key');
                    const { key } = await keyResp.json();
                    const applicationServerKey = urlBase64ToUint8Array(key);
                    pushSubscription = await swRegistration.pushManager.subscribe({
                        userVisibleOnly: true,
                        applicationServerKey
                    });
                    await fetch('/api/push/subscribe', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(pushSubscription.toJSON())
                    });
                }
            } catch(e) {
                console.error('Push toggle failed:', e);
                btn.textContent = 'Failed';
            }
            btn.disabled = false;
            updatePushButton();
        }

        function urlBase64ToUint8Array(base64String) {
            const padding = '='.repeat((4 - base64String.length % 4) % 4);
            const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
            const rawData = atob(base64);
            return new Uint8Array([...rawData].map(c => c.charCodeAt(0)));
        }

        window.addEventListener('load', initPush);

        // ---- Test notifications ----
        async function testNotification(channel) {
            const btn = document.getElementById(channel + '-test-btn');
            btn.classList.add('sending');
            btn.textContent = 'Sending...';
            try {
                const res = await fetch('/api/notify/test', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({channel})
                });
                const data = await res.json();
                btn.textContent = data.ok ? 'Sent!' : 'Failed';
                btn.style.color = data.ok ? '#3fb950' : '#f85149';
            } catch {
                btn.textContent = 'Failed';
                btn.style.color = '#f85149';
            }
            btn.classList.remove('sending');
            setTimeout(() => { btn.textContent = 'Test'; btn.style.color = ''; }, 3000);
        }

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
# PWA routes (manifest + service worker)
# ---------------------------------------------------------------------------
@app.route("/manifest.json")
def manifest_json():
    return jsonify({
        "name": "Courtside",
        "short_name": "Courtside",
        "description": "Live sports alerts for NBA, NFL, Soccer and more",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1117",
        "theme_color": "#161b22",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


@app.route("/sw.js")
def service_worker():
    sw = """
self.addEventListener('push', event => {
    const data = event.data ? event.data.json() : {};
    const title = data.headline || 'Courtside Alert';
    const options = {
        body: data.detail || '',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        vibrate: [200, 100, 200],
        data: { url: '/' },
        requireInteraction: data.priority === 'high',
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    event.waitUntil(clients.openWindow(event.notification.data.url || '/'));
});
"""
    return Response(sw, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": "/"})


# ---------------------------------------------------------------------------
# Push subscription routes
# ---------------------------------------------------------------------------
@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    user_id = session.get("user_id", "")
    data = request.json
    try:
        save_push_subscription(
            user_id=user_id,
            endpoint=data["endpoint"],
            p256dh=data["keys"]["p256dh"],
            auth=data["keys"]["auth"],
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    data = request.json
    delete_push_subscription(data["endpoint"])
    return jsonify({"ok": True})


@app.route("/api/push/vapid-public-key")
def api_vapid_public_key():
    return jsonify({"key": VAPID_PUBLIC_KEY})


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in — Courtside</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0f1117; color: #e1e4e8; min-height: 100vh;
       display: flex; align-items: center; justify-content: center; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
        padding: 48px 40px; text-align: center; max-width: 360px; width: 100%; }
h1 { font-size: 24px; margin-bottom: 8px; }
p { color: #8b949e; font-size: 14px; margin-bottom: 32px; }
.btn-google { display: inline-flex; align-items: center; gap: 12px;
              background: #fff; color: #1f1f1f; border: none; border-radius: 8px;
              padding: 12px 24px; font-size: 15px; font-weight: 500;
              cursor: pointer; text-decoration: none; transition: background 0.2s; }
.btn-google:hover { background: #f0f0f0; }
.btn-google svg { width: 20px; height: 20px; }
</style>
</head>
<body>
<div class="card">
  <h1>Courtside</h1>
  <p>Sign in to configure your alerts and receive notifications on Discord or Telegram.</p>
  <a href="/auth/google" class="btn-google">
    <svg viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.2l6.8-6.8C35.8 2.2 30.2 0 24 0 14.6 0 6.6 5.4 2.7 13.3l7.9 6.1C12.4 13 17.8 9.5 24 9.5z"/><path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h12.7c-.5 2.8-2.2 5.2-4.7 6.8l7.3 5.7c4.3-4 6.8-9.9 6.8-16.5z"/><path fill="#FBBC05" d="M10.6 28.6A14.6 14.6 0 0 1 9.5 24c0-1.6.3-3.2.8-4.6l-7.9-6.1A23.9 23.9 0 0 0 0 24c0 3.9.9 7.5 2.7 10.7l7.9-6.1z"/><path fill="#34A853" d="M24 48c6.2 0 11.4-2 15.2-5.5l-7.3-5.7c-2 1.4-4.6 2.2-7.9 2.2-6.2 0-11.5-4.2-13.4-9.8l-7.9 6.1C6.6 42.6 14.6 48 24 48z"/></svg>
    Sign in with Google
  </a>
</div>
</body>
</html>"""


@app.route("/login")
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template_string(LOGIN_TEMPLATE)


@app.route("/auth/google")
def auth_google():
    redirect_uri = url_for("auth_google_callback", _external=True, _scheme="https")
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.userinfo()
    google_id = userinfo["sub"]
    email = userinfo.get("email", "")
    name = userinfo.get("name", email)
    user_id = get_or_create_google_user(google_id, email, name)
    session["user_id"] = user_id
    session["user_name"] = name
    session["user_email"] = email
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    config = load_config()
    alerts_cfg = config.get("alerts", {})
    for key in ("close_game", "historic_scoring", "historic_stats", "blowout_comeback", "overtime"):
        if key not in alerts_cfg:
            alerts_cfg[key] = {"enabled": key not in ("blowout_comeback",)}

    selected_teams = set(config.get("teams_filter", []))

    user_id = session.get("user_id", "")
    with alert_lock:
        alerts_snapshot = list(alert_history.get(user_id, []))

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
        saved=request.args.get("saved"),
        alerts=alerts_snapshot,
        alert_count=len(alerts_snapshot),
        unseen_count=len(alerts_snapshot),
        monitor_running=monitor_running,
        monitor_status=monitor_status,
        games_list=games_snapshot,
        user_name=session.get("user_name", session.get("user_email", "Account")),
    )


@app.route("/save", methods=["POST"])
def save():
    form = request.form

    all_teams = "all_teams" in form
    teams_filter = [] if all_teams else form.getlist("teams")

    config = {
        "polling_interval_seconds": 30,
        "teams_filter": teams_filter,
        "alerts": {
            "close_game": {
                "enabled": "close_game_enabled" in form,
                "point_threshold": int(form.get("close_game_threshold", 5)),
                "minutes_remaining": int(form.get("close_game_minutes", 4)),
                "quarters": [4, 5, 6, 7],
            },
            "historic_scoring": {
                "enabled": "historic_scoring_enabled" in form,
                "points_threshold": int(form.get("scoring_points", 50)),
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
            "goat_tracker": {
                "enabled": "goat_tracker_enabled" in form,
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
    user_id = session.get("user_id", "")
    with alert_lock:
        alert_history.pop(user_id, None)
    return jsonify({"ok": True})


@app.route("/api/alerts/stream")
def api_alert_stream():
    user_id = session.get("user_id", "")

    def stream():
        q: Queue = Queue()
        with sse_lock:
            sse_clients.setdefault(user_id, []).append(q)
        try:
            while True:
                data = q.get()
                yield f"data: {json.dumps(data)}\n\n"
        except GeneratorExit:
            with sse_lock:
                queues = sse_clients.get(user_id, [])
                if q in queues:
                    queues.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/config")
def api_config():
    return jsonify(load_config())


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    channel = request.json.get("channel")
    cfg = load_config()
    test_alert = Alert(
        rule_name="test",
        game_id="test",
        headline="Test notification from Courtside",
        detail="Your notification setup is working correctly!",
        priority="low",
        dedup_key=("test",),
    )
    notif_cfg = cfg.get("notifications", {})
    try:
        if channel == "discord":
            discord_cfg = notif_cfg.get("discord", {})
            if not discord_cfg.get("webhook_url"):
                return jsonify({"ok": False, "error": "No webhook URL saved"})
            import asyncio
            asyncio.run(DiscordNotifier(discord_cfg["webhook_url"]).send(test_alert))
        elif channel == "telegram":
            tg_cfg = notif_cfg.get("telegram", {})
            if not tg_cfg.get("bot_token") or not tg_cfg.get("chat_id"):
                return jsonify({"ok": False, "error": "Bot token or chat ID missing"})
            import asyncio
            asyncio.run(TelegramNotifier(tg_cfg["bot_token"], tg_cfg["chat_id"]).send(test_alert))
        elif channel == "push":
            user_id = session.get("user_id", "")
            if not VAPID_PRIVATE_KEY:
                return jsonify({"ok": False, "error": "VAPID keys not configured on server"})
            _send_web_push_to_user(user_id, test_alert)
        else:
            return jsonify({"ok": False, "error": "Unknown channel"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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
    init_db()
    port = int(os.environ.get("PORT", 0)) or find_open_port()
    start_monitor()
    print(f"\n  Open http://localhost:{port} to configure notifications\n")
    print("  Monitor started automatically — watching for games.\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
