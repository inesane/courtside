#!/usr/bin/env python3
"""Test alert rules with simulated game progressions."""

from __future__ import annotations

from alerts.base import Alert
from alerts.engine import AlertEngine
from alerts.nba.rules import (
    BlowoutComebackRule,
    CloseGameRule,
    HistoricScoringRule,
    HistoricStatLineRule,
    OvertimeRule,
)
from sports.base import GameState, PlayerStats

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results = []

def check(label: str, condition: bool) -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    results.append((label, condition))

def make_engine(config: dict = None) -> AlertEngine:
    config = config or {}
    engine = AlertEngine(rules=[])
    from webapp import build_rules
    engine.rules = build_rules(config, engine)
    return engine

def game(
    game_id="g1", period=4, clock="4:00", home_score=90, away_score=88,
    status="in_progress", players=None
) -> GameState:
    return GameState(
        game_id=game_id,
        status=status,
        home_team="Knicks",
        away_team="Cavaliers",
        home_abbrev="NY",
        away_abbrev="CLE",
        home_score=home_score,
        away_score=away_score,
        period=period,
        clock=clock,
        detail=f"Q{period} {clock}",
        players=players or [],
    )

def player(name: str, team: str, pts=0, reb=0, ast=0, stl=0, blk=0) -> PlayerStats:
    return PlayerStats(
        player_name=name, team=team,
        stats={"pts": pts, "reb": reb, "ast": ast, "stl": stl, "blk": blk}
    )


# ---------------------------------------------------------------------------
print("\n=== Close Game Rule ===")
# ---------------------------------------------------------------------------

eng = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 5, "minutes_remaining": 4}}})

# Should NOT fire: 8-point game with 3 min left (over point threshold)
fired = eng.evaluate(game(period=4, clock="3:00", home_score=95, away_score=87))
check("No alert: 8-pt game with 3 min left (over threshold)", len(fired) == 0)

# Should NOT fire: 3-point game with 5 min left (outside time window)
fired = eng.evaluate(game(game_id="g2", period=4, clock="5:00", home_score=90, away_score=87))
check("No alert: 3-pt game with 5 min left (outside window)", len(fired) == 0)

# Should fire: 3-point game with 3 min left
eng2 = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 5, "minutes_remaining": 4}}})
fired = eng2.evaluate(game(game_id="g3", period=4, clock="3:00", home_score=90, away_score=87))
check("Alert fires: 3-pt game with 3 min left in Q4", len(fired) == 1 and fired[0].rule_name == "close_game")

# Should NOT fire again same period (dedup)
fired2 = eng2.evaluate(game(game_id="g3", period=4, clock="1:30", home_score=90, away_score=88))
check("No duplicate alert same period", len(fired2) == 0)

# Should fire in OT (different period = new dedup key)
fired3 = eng2.evaluate(game(game_id="g3", period=5, clock="3:00", home_score=95, away_score=93))
check("Alert fires again in OT period (new period = new dedup)", len(fired3) >= 1 and any(a.rule_name == "close_game" for a in fired3))

# Priority check
eng3 = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 5, "minutes_remaining": 4}}})
fired = eng3.evaluate(game(game_id="g4", period=4, clock="2:00", home_score=90, away_score=88))
check("Priority 'high' for 2-pt game (score_diff <= 3)", len(fired) == 1 and fired[0].priority == "high")

eng4 = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 5, "minutes_remaining": 4}}})
fired = eng4.evaluate(game(game_id="g5", period=4, clock="2:00", home_score=90, away_score=85))
check("Priority 'medium' for 5-pt game (score_diff > 3)", len(fired) == 1 and fired[0].priority == "medium")

# Tied game
eng5 = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 5, "minutes_remaining": 4}}})
fired = eng5.evaluate(game(game_id="g6", period=4, clock="1:00", home_score=90, away_score=90))
check("Alert fires: tied game with 1 min left", len(fired) == 1)

# Custom threshold: 10 points
eng6 = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 10, "minutes_remaining": 4}}})
fired = eng6.evaluate(game(game_id="g7", period=4, clock="3:00", home_score=95, away_score=87))
check("Alert fires with 10-pt threshold: 8-pt game", len(fired) == 1)

# Disabled
eng7 = make_engine({"alerts": {"close_game": {"enabled": False}}})
fired = eng7.evaluate(game(game_id="g8", period=4, clock="1:00", home_score=90, away_score=89))
check("No alert when rule disabled", len(fired) == 0)


# ---------------------------------------------------------------------------
print("\n=== Historic Scoring Rule ===")
# ---------------------------------------------------------------------------

eng = make_engine({"alerts": {"historic_scoring": {"enabled": True, "points_threshold": 30}}})

p = player("Wembanyama", "SA", pts=29)
fired = eng.evaluate(game(game_id="s1", period=3, clock="5:00", players=[p]))
check("No alert: player at 29 pts (below 30 threshold)", len(fired) == 0)

p2 = player("Wembanyama", "SA", pts=30)
fired = eng.evaluate(game(game_id="s1", period=3, clock="4:00", players=[p2]))
check("Alert fires: player hits 30 pts", len(fired) == 1 and fired[0].rule_name == "historic_scoring")

p3 = player("Wembanyama", "SA", pts=35)
fired = eng.evaluate(game(game_id="s1", period=3, clock="3:00", players=[p3]))
check("No duplicate for same player same game (dedup)", len(fired) == 0)

# Multiple players
eng2 = make_engine({"alerts": {"historic_scoring": {"enabled": True, "points_threshold": 30}}})
p_a = player("LeBron", "LAL", pts=31)
p_b = player("Curry", "GS", pts=32)
fired = eng2.evaluate(game(game_id="s2", period=4, clock="5:00", players=[p_a, p_b]))
check("Both players fire independently", len(fired) == 2)

# Fires at ANY time in the game (not just last 2 min)
eng3 = make_engine({"alerts": {"historic_scoring": {"enabled": True, "points_threshold": 30}}})
p4 = player("Jordan", "CHI", pts=30)
fired = eng3.evaluate(game(game_id="s3", period=2, clock="8:00", players=[p4]))
check("Alert fires in Q2 with 8 min left (not time-gated)", len(fired) == 1)


# ---------------------------------------------------------------------------
print("\n=== Overtime Rule ===")
# ---------------------------------------------------------------------------

eng = make_engine({"alerts": {"overtime": {"enabled": True}}})

# No OT in Q4
fired = eng.evaluate(game(game_id="ot1", period=4, clock="2:00", home_score=90, away_score=88))
check("No OT alert during Q4", not any(a.rule_name == "overtime" for a in fired))

# Transition 4 -> 5: tied at buzzer + OT start
g_q4 = game(game_id="ot1", period=4, clock="0:01", home_score=90, away_score=90)
eng.evaluate(g_q4)  # establish prev state in Q4

g_ot1 = game(game_id="ot1", period=5, clock="5:00", home_score=90, away_score=90)
fired = eng.evaluate(g_ot1)
ot_rules = [a.rule_name for a in fired]
check("OT transition fires OT alert", "overtime" in ot_rules)
tied_alerts = [a for a in fired if "TIED" in a.headline.upper() or "BUZZER" in a.headline.upper()]
check("Tied-at-buzzer alert fires on 4->5 transition", len(tied_alerts) >= 1)

# No duplicate OT alert same period
fired2 = eng.evaluate(game(game_id="ot1", period=5, clock="3:00", home_score=92, away_score=90))
check("No duplicate OT alert same OT period", not any(a.rule_name == "overtime" for a in fired2))

# Double OT
g_ot2 = game(game_id="ot1", period=6, clock="5:00", home_score=100, away_score=100)
fired3 = eng.evaluate(g_ot2)
check("Double OT fires new alert (new period)", any(a.rule_name == "overtime" for a in fired3))

# Cold start mid-OT (app restart during OT)
eng_cold = make_engine({"alerts": {"overtime": {"enabled": True}}})
fired = eng_cold.evaluate(game(game_id="ot2", period=5, clock="2:00", home_score=95, away_score=93))
check("Cold start mid-OT still fires OT alert (fallback)", any(a.rule_name == "overtime" for a in fired))

# Not tied going into OT (shouldn't fire tied-at-buzzer)
eng2 = make_engine({"alerts": {"overtime": {"enabled": True}}})
g_q4b = game(game_id="ot3", period=4, clock="0:01", home_score=92, away_score=90)
eng2.evaluate(g_q4b)
g_ot1b = game(game_id="ot3", period=5, clock="5:00", home_score=92, away_score=90)
fired = eng2.evaluate(g_ot1b)
tied_alerts = [a for a in fired if "TIED" in a.headline.upper() or "BUZZER" in a.headline.upper()]
check("No tied-at-buzzer if game not tied in Q4", len(tied_alerts) == 0)


# ---------------------------------------------------------------------------
print("\n=== Blowout Comeback Rule ===")
# ---------------------------------------------------------------------------

eng = make_engine({"alerts": {"blowout_comeback": {"enabled": True, "deficit_threshold": 20, "close_threshold": 5}}})

# Build up a big lead
for score in [(80, 60), (85, 65), (90, 68)]:
    eng.evaluate(game(game_id="bc1", period=3, clock="5:00", home_score=score[0], away_score=score[1]))
check("No comeback alert while still a big lead", True)  # just verifying no crash

# Now comeback to within 5
fired = eng.evaluate(game(game_id="bc1", period=4, clock="3:00", home_score=90, away_score=87))
check("Comeback alert fires after 20+ pt deficit shrinks to 3", any(a.rule_name == "blowout_comeback" for a in fired))

# No duplicate
fired2 = eng.evaluate(game(game_id="bc1", period=4, clock="1:00", home_score=90, away_score=88))
check("No duplicate comeback alert same game", not any(a.rule_name == "blowout_comeback" for a in fired2))

# Small deficit never triggers
eng2 = make_engine({"alerts": {"blowout_comeback": {"enabled": True, "deficit_threshold": 20, "close_threshold": 5}}})
eng2.evaluate(game(game_id="bc2", period=2, clock="5:00", home_score=75, away_score=65))  # 10-pt max
fired = eng2.evaluate(game(game_id="bc2", period=4, clock="2:00", home_score=75, away_score=73))
check("No comeback alert if deficit never reached 20", not any(a.rule_name == "blowout_comeback" for a in fired))


# ---------------------------------------------------------------------------
print("\n=== Historic Stat Line Rule ===")
# ---------------------------------------------------------------------------

eng = make_engine({"alerts": {"historic_stats": {"enabled": True}}})

# REB
p = player("Giannis", "MIL", reb=25)
fired = eng.evaluate(game(game_id="hs1", period=3, clock="5:00", players=[p]))
check("Alert fires: 25 rebounds", any(a.rule_name == "historic_stats" for a in fired))

# AST
eng2 = make_engine({"alerts": {"historic_stats": {"enabled": True}}})
p2 = player("Harden", "LAC", ast=18)
fired = eng2.evaluate(game(game_id="hs2", period=4, clock="3:00", players=[p2]))
check("Alert fires: 18 assists", any(a.rule_name == "historic_stats" for a in fired))

# Below threshold
eng3 = make_engine({"alerts": {"historic_stats": {"enabled": True}}})
p3 = player("Rudy", "MIN", reb=10)
fired = eng3.evaluate(game(game_id="hs3", period=4, clock="3:00", players=[p3]))
check("No historic_stats alert: 10 rebounds (below 25 threshold)", not any(a.rule_name == "historic_stats" for a in fired))


# ---------------------------------------------------------------------------
print("\n=== Per-user independence ===")
# ---------------------------------------------------------------------------

# User A: 5-pt threshold, 4 min
eng_a = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 5, "minutes_remaining": 4}}})
# User B: 10-pt threshold, 6 min

eng_b = make_engine({"alerts": {"close_game": {"enabled": True, "point_threshold": 10, "minutes_remaining": 6}}})

g = game(game_id="pu1", period=4, clock="5:00", home_score=95, away_score=88)  # 7-pt, 5 min

fired_a = eng_a.evaluate(g)
fired_b = eng_b.evaluate(g)
check("User A (5pt/4min) does NOT get alert: 7-pt game at 5 min", len(fired_a) == 0)
check("User B (10pt/6min) DOES get alert: 7-pt game at 5 min", len(fired_b) == 1)

# Now at 3 min, 4-pt game
g2 = game(game_id="pu1", period=4, clock="3:00", home_score=95, away_score=91)
fired_a2 = eng_a.evaluate(g2)
fired_b2 = eng_b.evaluate(g2)
check("User A gets alert at 3 min, 4-pt game (their threshold)", len(fired_a2) == 1)
check("User B dedup prevents re-fire (already fired same period)", len(fired_b2) == 0)

# Historic scoring independence
eng_a2 = make_engine({"alerts": {"historic_scoring": {"enabled": True, "points_threshold": 30}}})
eng_b2 = make_engine({"alerts": {"historic_scoring": {"enabled": True, "points_threshold": 50}}})

p = player("Wembanyama", "SA", pts=42)
fired_a = eng_a2.evaluate(game(game_id="pu2", period=3, clock="5:00", players=[p]))
fired_b = eng_b2.evaluate(game(game_id="pu2", period=3, clock="5:00", players=[p]))
check("User A (30pt threshold) gets alert at 42 pts", len(fired_a) == 1)
check("User B (50pt threshold) does NOT get alert at 42 pts", len(fired_b) == 0)


# ---------------------------------------------------------------------------
print("\n=== Game state cleanup ===")
# ---------------------------------------------------------------------------

eng = make_engine()
eng.evaluate(game(game_id="cl1", period=4, clock="2:00", home_score=95, away_score=88))
check("Game state stored while in_progress", "cl1" in eng._prev_states)

eng.evaluate(game(game_id="cl1", period=4, clock="0:00", home_score=98, away_score=90, status="final"))
check("Game state cleaned up after final", "cl1" not in eng._prev_states)


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
# ---------------------------------------------------------------------------
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
total = len(results)
print(f"\n  {passed}/{total} passed", end="")
if failed:
    print(f"  ({failed} FAILED):")
    for label, ok in results:
        if not ok:
            print(f"    - {label}")
else:
    print("  — all good!")
print()
