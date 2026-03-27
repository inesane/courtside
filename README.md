# Courtside

Real-time sports notification system that alerts you when games get exciting — close finishes, historic performances, overtime, and more. Supports NBA, NCAA Basketball (March Madness), NFL, and Soccer (Premier League, Champions League, La Liga, MLS).

## Features

- **Multi-sport support** — NBA, March Madness, NFL, and Soccer with sport-specific alert rules
- **Live scoreboard** — see today's games across all sports with real-time scores
- **Smart alerts** — close games, historic scoring, big stat lines, upsets, late goals, and more
- **Player tracker** — search and follow any player across any sport for in-game performance alerts
- **GOAT Tracker** — career milestone tracking (e.g. LeBron approaching all-time records)
- **Multiple channels** — in-app bell, Discord, Telegram, desktop notifications, console
- **Configurable** — toggle each alert type, set thresholds, filter by team per sport
- **Smart polling** — sleeps when no games are on, wakes up before tipoff, polls every 30s during live games
- **Web UI** — configure everything from one page with sport tabs

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy the example config
cp config.example.yaml config.yaml

# Start the web UI (monitor starts automatically)
python3 webapp.py
```

Open the URL shown in the terminal (e.g., `http://localhost:5050`) to:
1. Enable the sports you want to follow
2. Pick your teams per sport
3. Toggle which alerts you want
4. Search and track specific players
5. Set up Discord/Telegram if desired

## Supported Sports

### NBA
| Alert | What it detects | Default |
|-------|----------------|---------|
| Close Game | Tight score in Q4 / OT | Within 5 pts, last 4 min |
| Historic Scoring | Player hits a massive point total | 50+ pts |
| Historic Stats | Huge rebounds, assists, steals, or blocks | 25+ reb, 18+ ast, 7+ stl, 8+ blk |
| Blowout Comeback | Team erasing a big deficit | Down 20+, now within 5 |
| Overtime | Game goes to OT | Any OT |
| GOAT Tracker | Career milestones approaching | Per-game average based |

### March Madness (NCAA Basketball)
| Alert | What it detects | Default |
|-------|----------------|---------|
| Close Game | Tight score in 2nd half / OT | Within 5 pts, last 4 min |
| Upset Alert | Lower seed beating a higher seed late | 5+ seed difference |
| Historic Scoring | Player hits a massive point total | 40+ pts |
| Historic Stats | Huge stat lines | Same as NBA |
| Overtime | Game goes to OT | Any OT |

### NFL
| Alert | What it detects | Default |
|-------|----------------|---------|
| Close Game | Within one score in Q4 | Within 7 pts, last 4 min |
| QB on Fire | Quarterback with huge passing game | 4+ TDs or 400+ yards |
| Rushing Explosion | Player with monster rushing game | 150+ yards or 3+ TDs |
| Blowout Comeback | Team erasing a big deficit | Down 17+, now within 7 |
| Overtime | Game goes to OT | Any OT |

### Soccer (EPL, Champions League, La Liga, MLS)
| Alert | What it detects | Default |
|-------|----------------|---------|
| Late Goal | Goal scored near the end | 80th minute+ |
| Equalizer | Team ties the match late | 75th minute+ |
| Comeback | Team recovers from 2+ goal deficit | Tied or leading |
| Red Card | Player receives a red card | Any |
| Extra Time | Match goes to extra time | Any |

All thresholds are configurable in the web UI.

## Player Tracker

Search for any player by name and track them across all sports. When a tracked player has a big moment during a live game, you get notified.

- **Basketball**: 30+ points, double-doubles, triple-doubles
- **Football**: 300+ passing yards, 3+ TD passes, 150+ rushing yards
- **Soccer**: Goals, assists, hat tricks

## GOAT Tracker

Tracks career milestones for players approaching all-time records. Checks once per day after games go final and only alerts when a record could realistically be broken in the next game (based on per-game averages from ESPN). Each milestone is only alerted once.

Currently tracking **LeBron James**:
- Games played (Robert Parish's record)
- Points (45,000 career milestone)
- Assists (Jason Kidd, Chris Paul)
- Rebounds (Tim Duncan)
- Steals (Gary Payton, Michael Jordan, Jason Kidd)
- Three-pointers (Klay Thompson, Ray Allen)
- Free throws (Karl Malone's record)

Player milestones are defined in `milestones.json`.

## Notification Channels

- **In-App (Bell)** — notifications in the bell icon panel in the web UI
- **Console** — colored output in the terminal
- **Desktop** — OS-level push notifications via `plyer`
- **Discord** — send to a channel via webhook URL
- **Telegram** — send via Bot API (token + chat ID)

## Smart Polling

| State | Polling interval |
|-------|-----------------|
| Games are live | Every 30 seconds |
| Games scheduled, hours away | Sleeps until 5 min before tipoff |
| All games finished / no games today | Checks back every 30 minutes |

## Project Structure

```
├── webapp.py              # Web UI + built-in monitor
├── config.example.yaml    # Example configuration
├── milestones.json        # LeBron career milestone definitions
├── requirements.txt
├── Dockerfile             # Docker deployment
├── sports/
│   ├── base.py            # GameState, PlayerStats, SportProvider
│   ├── espn.py            # ESPN API client (shared across sports)
│   ├── registry.py        # Sport registry (teams, rules, metadata)
│   ├── nba/provider.py    # NBA game + box score parsing
│   ├── ncaab/provider.py  # NCAA basketball parsing
│   ├── nfl/provider.py    # NFL parsing
│   └── soccer/provider.py # Soccer parsing (multi-league)
├── alerts/
│   ├── base.py            # Alert, AlertRule
│   ├── engine.py          # Alert engine with deduplication
│   ├── milestones.py      # GOAT Tracker milestone checker
│   ├── player_tracking.py # In-game player performance alerts
│   ├── nba/rules.py       # NBA alert rules
│   ├── ncaab/rules.py     # NCAA alert rules (upset detection)
│   ├── nfl/rules.py       # NFL alert rules (QB, rushing)
│   └── soccer/rules.py    # Soccer alert rules (late goals, red cards)
└── notifications/
    ├── console.py         # Terminal output
    ├── desktop.py         # OS notifications
    ├── discord.py         # Discord webhook
    └── telegram.py        # Telegram bot
```

## Data Source

Uses ESPN's public scoreboard, summary, and athlete APIs. No API key required.
