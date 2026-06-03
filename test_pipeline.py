#!/usr/bin/env python3
"""
End-to-end pipeline test: simulates game data coming in from ESPN,
triggering alert rules, and verifying notifications would be sent.
Uses the real per-user engine + _send_to_user but mocks actual network calls.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from alerts.engine import AlertEngine
from alerts.base import Alert
from sports.base import GameState, PlayerStats

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []

def check(label: str, condition: bool) -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    results.append((label, condition))

# ---------------------------------------------------------------------------
# Fake user config — mirrors what's saved in prod DB
# ---------------------------------------------------------------------------
FAKE_USER = {
    "user_id": "test-user-123",
    "config": {
        "teams_filter": [],  # all teams
        "alerts": {
            "close_game":      {"enabled": True, "point_threshold": 10, "minutes_remaining": 4, "quarters": [4,5,6,7]},
            "historic_scoring":{"enabled": True, "points_threshold": 30},
            "historic_stats":  {"enabled": True},
            "blowout_comeback":{"enabled": True, "deficit_threshold": 20, "close_threshold": 5},
            "overtime":        {"enabled": True},
            "goat_tracker":    {"enabled": True},
        },
        "notifications": {
            "discord":  {"enabled": False},
            "telegram": {"enabled": False},
            "webapp":   {"enabled": True},
        },
    }
}

def live_game(game_id="g1", period=4, clock="3:30", home=90, away=85, players=None):
    return GameState(
        game_id=game_id, status="in_progress",
        home_team="Knicks", away_team="Cavaliers",
        home_abbrev="NY", away_abbrev="CLE",
        home_score=home, away_score=away,
        period=period, clock=clock, detail=f"Q{period} {clock}",
        players=players or [],
    )

# ---------------------------------------------------------------------------
# Test full pipeline: game poll → alert engine → _send_to_user
# ---------------------------------------------------------------------------

async def simulate_poll(game: GameState, user_cfg: dict) -> list[dict]:
    """Simulate one poll cycle for one user. Returns list of notifications sent."""
    from webapp import build_rules, _get_user_engine, _send_to_user

    sent = []

    user_id = user_cfg["user_id"]
    cfg = user_cfg["config"]

    # Build user's engine
    eng = _get_user_engine(user_id)
    eng.rules = build_rules(cfg, eng)
    fired = eng.evaluate(game)

    # For each alert, call _send_to_user with mocked notifiers
    for alert in fired:
        with patch("webapp.build_notifiers", return_value=[]), \
             patch("webapp._send_web_push_to_user") as mock_push, \
             patch("webapp.alert_history", {}), \
             patch("webapp._push_to_user") as mock_sse, \
             patch("webapp.VAPID_PRIVATE_KEY", "fake-key"):
            await _send_to_user(user_id, cfg, alert)
            sent.append({
                "alert": alert,
                "sse_pushed": mock_sse.called,
                "web_push_called": mock_push.called,
            })

    return sent


print("\n=== Close game alert → push pipeline ===")

async def test_close_game():
    g = live_game(period=4, clock="3:30", home=95, away=88)  # 7-pt, 3.5 min
    sent = await simulate_poll(g, FAKE_USER)
    close_alerts = [s for s in sent if s["alert"].rule_name == "close_game"]
    check("Close game alert fires for 7-pt game within 4 min", len(close_alerts) >= 1)
    if close_alerts:
        check("SSE push called for close game", close_alerts[0]["sse_pushed"])
        check("Web push called for close game", close_alerts[0]["web_push_called"])

asyncio.run(test_close_game())


print("\n=== Historic scoring → push pipeline ===")

async def test_historic_scoring():
    p = PlayerStats("Wembanyama", "SA", {"pts": 35})
    g = live_game(game_id="hs1", period=3, clock="8:00", players=[p])
    sent = await simulate_poll(g, FAKE_USER)
    scoring_alerts = [s for s in sent if s["alert"].rule_name == "historic_scoring"]
    check("Historic scoring alert fires for 35-pt player", len(scoring_alerts) >= 1)
    if scoring_alerts:
        check("SSE push called for scoring alert", scoring_alerts[0]["sse_pushed"])
        check("Web push called for scoring alert", scoring_alerts[0]["web_push_called"])
    check("Alert fires in Q3 with 8 min left (not time-gated)", len(scoring_alerts) >= 1)

asyncio.run(test_historic_scoring())


print("\n=== OT transition → push pipeline ===")

async def test_ot():
    from webapp import _get_user_engine, build_rules
    user_id = "ot-test-user"
    cfg = FAKE_USER["config"]
    eng = _get_user_engine(user_id)
    eng.rules = build_rules(cfg, eng)

    # Poll 1: end of Q4, tied
    g_q4 = live_game(game_id="ot1", period=4, clock="0:01", home=90, away=90)
    eng.evaluate(g_q4)

    # Poll 2: OT starts
    g_ot = live_game(game_id="ot1", period=5, clock="5:00", home=90, away=90)
    eng.rules = build_rules(cfg, eng)
    fired = eng.evaluate(g_ot)

    ot_alerts = [a for a in fired if a.rule_name == "overtime"]
    tied_alerts = [a for a in fired if "TIED" in a.headline.upper() or "BUZZER" in a.headline.upper()]

    check("OT alert fires on period transition", len(ot_alerts) >= 1)
    check("Tied-at-buzzer alert fires", len(tied_alerts) >= 1)

    # Verify push would be sent
    sent = []
    for alert in fired:
        with patch("webapp.build_notifiers", return_value=[]), \
             patch("webapp._send_web_push_to_user") as mock_push, \
             patch("webapp.alert_history", {}), \
             patch("webapp._push_to_user") as mock_sse, \
             patch("webapp.VAPID_PRIVATE_KEY", "fake-key"):
            from webapp import _send_to_user
            await _send_to_user(user_id, cfg, alert)
            sent.append(mock_push.called)

    check("Web push called for OT alert(s)", any(sent))

asyncio.run(test_ot())


print("\n=== Teams filter respected ===")

async def test_teams_filter():
    from webapp import _get_user_engine, build_rules

    cfg_lakers_only = {**FAKE_USER["config"], "teams_filter": ["LAL"]}
    user_id = "lakers-fan"
    eng = _get_user_engine(user_id)
    eng.rules = build_rules(cfg_lakers_only, eng)

    # Game not involving Lakers — engine evaluates but teams filter skips it in poll loop
    g_no_lakers = live_game(game_id="nl1", period=4, clock="2:00", home=88, away=85)
    # Simulate teams filter check (as poll loop does it)
    user_filter = set(cfg_lakers_only.get("teams_filter", []))
    skip = bool(user_filter and not (
        g_no_lakers.home_abbrev in user_filter or g_no_lakers.away_abbrev in user_filter
    ))
    check("Knicks vs Cavs skipped for Lakers-only user", skip)

    # Lakers game — should not be skipped
    g_lakers = GameState(
        game_id="lal1", status="in_progress",
        home_team="Lakers", away_team="Warriors",
        home_abbrev="LAL", away_abbrev="GS",
        home_score=95, away_score=92,
        period=4, clock="2:00", detail="Q4 2:00",
    )
    skip_lakers = bool(user_filter and not (
        g_lakers.home_abbrev in user_filter or g_lakers.away_abbrev in user_filter
    ))
    check("Lakers game NOT skipped for Lakers-only user", not skip_lakers)

asyncio.run(test_teams_filter())


print("\n=== User with notifications disabled ===")

async def test_disabled():
    from webapp import _get_user_engine, build_rules
    cfg_disabled = {
        **FAKE_USER["config"],
        "alerts": {**FAKE_USER["config"]["alerts"], "close_game": {"enabled": False}},
    }
    user_id = "disabled-user"
    eng = _get_user_engine(user_id)
    eng.rules = build_rules(cfg_disabled, eng)

    g = live_game(game_id="dis1", period=4, clock="2:00", home=90, away=88)
    fired = eng.evaluate(g)
    close = [a for a in fired if a.rule_name == "close_game"]
    check("No close game alert when rule disabled", len(close) == 0)

asyncio.run(test_disabled())


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\n  {passed}/{len(results)} passed", end="")
if failed:
    print(f"  ({failed} FAILED):")
    for label, ok in results:
        if not ok:
            print(f"    - {label}")
else:
    print("  — all good!")
print()
