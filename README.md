# Courtside

Missed Bam's 83? Real-time sports notification system that alerts you when games get exciting — close finishes, historic performances, overtime, and more. Currently supports the NBA with a plugin architecture for adding other leagues.

## Features

- **Live scoreboard** — see all today's games with real-time scores in the web UI
- **Smart alerts** — get notified for close games, historic scoring, big stat lines, overtime
- **GOAT Tracker** — track career milestones for all-time greats (e.g. LeBron approaching records)
- **Multiple channels** — in-app bell, Discord, Telegram, desktop notifications, console
- **Configurable** — toggle each alert type, set thresholds, filter by team
- **Smart polling** — sleeps when no games are on, wakes up before tipoff, polls every 30s during live games
- **Web UI** — configure everything and see notifications from one page

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy the example config
cp config.example.yaml config.yaml

# Start the web UI
python3 webapp.py
```

Open the URL shown in the terminal (e.g., `http://localhost:5050`) to:
1. Pick your teams
2. Toggle which notifications you want
3. Set up Discord/Telegram if desired
4. Click the monitor badge to start watching games

## Alert Types

| Alert | What it detects | Default threshold |
|-------|----------------|-------------------|
| Close Game | Tight score in the 4th quarter / OT | Within 5 pts, last 4 min |
| Historic Scoring | Player hits a massive point total | 50+ pts |
| Historic Stats | Huge rebounding, assist, steal, or block numbers | 25+ reb, 18+ ast, 7+ stl, 8+ blk |
| Blowout Comeback | Team erasing a big deficit | Was down 20+, now within 5 |
| Overtime | Game goes to OT | Any OT |
| GOAT Tracker | Career milestones approaching or broken | Configurable per-milestone |

All thresholds are configurable in the web UI.

## GOAT Tracker

Tracks career milestones for players approaching all-time records. Checks once per day after all games go final and alerts when a player is closing in on a milestone.

Currently tracking **LeBron James**:
- Games played (Robert Parish's record)
- All-time assists rankings (Jason Kidd, Chris Paul)
- All-time rebounds rankings (Tim Duncan)
- 45,000 career points milestone

Player milestones are defined in `milestones.json`. To track additional players, add their ESPN ID and the milestones you want to watch.

## Notification Channels

- **In-App (Bell)** — notifications appear in the bell icon panel in the web UI
- **Console** — colored output in the terminal
- **Desktop** — OS-level push notifications via `plyer`
- **Discord** — send to a channel via webhook URL
- **Telegram** — send via Bot API (token + chat ID)

## Smart Polling

The monitor minimizes API calls based on game state:

| State | Polling interval |
|-------|-----------------|
| Games are live | Every 30 seconds |
| Games scheduled, hours away | Sleeps until 5 min before tipoff |
| All games finished / no games today | Checks back every 30 minutes |

## Project Structure

```
├── webapp.py              # Web UI + built-in monitor
├── main.py                # Standalone CLI monitor (optional)
├── config.py              # Config loader
├── config.example.yaml    # Example configuration
├── requirements.txt
├── sports/
│   ├── base.py            # GameState, PlayerStats, SportProvider
│   ├── espn.py            # ESPN API client (shared across sports)
│   └── nba/
│       └── provider.py    # NBA game + box score parsing
├── alerts/
│   ├── base.py            # Alert, AlertRule
│   ├── engine.py          # Alert engine with deduplication
│   ├── milestones.py      # GOAT Tracker milestone checker
│   └── nba/
│       └── rules.py       # NBA-specific alert rules
├── milestones.json        # Player milestone definitions
└── notifications/
    ├── base.py            # Notifier base class
    ├── console.py         # Terminal output
    ├── desktop.py         # OS notifications
    ├── discord.py         # Discord webhook
    └── telegram.py        # Telegram bot
```

## Adding Other Sports

The ESPN API uses the same URL pattern across sports. To add a new league (e.g., NFL):

1. Create `sports/nfl/provider.py` — swap `basketball/nba` for `football/nfl`
2. Create `alerts/nfl/rules.py` — define sport-specific alert rules
3. Register in `webapp.py`

## Data Source

Uses ESPN's public scoreboard and summary APIs. No API key required.
