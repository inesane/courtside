#!/usr/bin/env python3
"""Sports notification system — monitors live games and alerts on exciting moments."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from alerts.base import AlertRule
from alerts.engine import AlertEngine
from alerts.nba.rules import (
    BlowoutComebackRule,
    CloseGameRule,
    HistoricScoringRule,
    HistoricStatLineRule,
    OvertimeRule,
)
from config import get_alert_config, load_config
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
logger = logging.getLogger("main")


def build_rules(config: dict, engine: AlertEngine) -> list[AlertRule]:
    """Build alert rules from config."""
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
        rules.append(HistoricScoringRule(
            thresholds=cfg.get("thresholds"),
        ))

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
    """Build notification channels from config."""
    notifiers: list[Notifier] = []
    notif_cfg = config.get("notifications", {})

    if notif_cfg.get("console", {}).get("enabled", True):
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


async def run(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    polling_interval = config.get("polling_interval_seconds", 30)
    teams_filter = set(config.get("teams_filter", []))

    espn = ESPNClient()
    provider = NBAProvider(espn)

    engine = AlertEngine(rules=[])  # Init empty, then build rules with engine ref
    engine.rules = build_rules(config, engine)

    notifiers = build_notifiers(config)

    logger.info("Sports Notifications started")
    logger.info("Polling every %ds | Teams filter: %s",
                polling_interval,
                list(teams_filter) if teams_filter else "ALL")
    logger.info("Active rules: %s", [r.name for r in engine.rules])
    logger.info("Active notifiers: %s", [type(n).__name__ for n in notifiers])

    try:
        while True:
            try:
                await poll_cycle(provider, engine, notifiers, teams_filter)
            except Exception:
                logger.exception("Error during poll cycle")

            await asyncio.sleep(polling_interval)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await espn.close()


async def poll_cycle(
    provider: NBAProvider,
    engine: AlertEngine,
    notifiers: list[Notifier],
    teams_filter: set[str],
) -> None:
    games = await provider.get_games()

    live_games = [g for g in games if g.status == "in_progress"]
    scheduled = [g for g in games if g.status == "scheduled"]
    final = [g for g in games if g.status == "final"]

    now = datetime.now().strftime("%H:%M:%S")
    logger.info(
        "[%s] Games: %d live, %d scheduled, %d final",
        now, len(live_games), len(scheduled), len(final),
    )

    for game in live_games:
        # Apply team filter
        if teams_filter and not (
            game.home_abbrev in teams_filter or game.away_abbrev in teams_filter
        ):
            continue

        logger.info("  LIVE: %s | %s", game.score_line(), game.detail)

        # Fetch box score for player stats
        try:
            await provider.enrich_box_score(game)
        except Exception:
            logger.warning("Failed to fetch box score for %s", game.game_id)

        # Evaluate alert rules
        alerts = engine.evaluate(game)

        # Send notifications
        for alert in alerts:
            for notifier in notifiers:
                try:
                    await notifier.send(alert)
                except Exception:
                    logger.warning("Notification failed: %s", type(notifier).__name__)

    # Also track final games to detect overtime that just ended
    for game in final:
        if teams_filter and not (
            game.home_abbrev in teams_filter or game.away_abbrev in teams_filter
        ):
            continue
        engine.evaluate(game)  # Triggers cleanup


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    if not Path(config_path).exists():
        print("No config.yaml found. Run the config app first:")
        print("  python3 webapp.py")
        return

    asyncio.run(run(config_path))


if __name__ == "__main__":
    main()
