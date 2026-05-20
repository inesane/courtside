# Courtside

Real-time sports notification system that alerts you when games get exciting — close finishes, historic performances, overtime, comeback alerts, and career milestones. Sign in with Google, configure your alerts, and get notified via Discord, Telegram, or browser push.

## Live App

**[courtside.fly.dev](https://courtside.fly.dev)** — hosted on Fly.io, no setup required. Sign in with Google to get started.

## Features

- **Live scoreboard** — all today's games with real-time scores in the web UI
- **Smart alerts** — close games, historic scoring, big stat lines, overtime, comebacks
- **GOAT Tracker** — career milestone alerts for all-time greats
- **Multiple notification channels** — in-app bell, Discord webhook, Telegram bot, browser push (Android/desktop)
- **Per-user configuration** — each user has their own thresholds, team filters, and notification setup
- **Smart polling** — sleeps when no games are on, wakes before tipoff, polls every 30s during live games
- **Multi-user** — anyone can sign in with Google and get independent alerts based on their own config

## Quick Start (local)

```bash
pip install -r requirements.txt
python3 webapp.py
```

Open the URL shown in the terminal. The local version skips Google OAuth — configure alerts and start monitoring directly.

## Alert Types

| Alert | What it detects | Default threshold |
|-------|----------------|-------------------|
| Close Game | Tight score in 4th quarter / OT | Within 5 pts, last 4 min |
| Historic Scoring | Player on a big scoring night | 50+ pts |
| Historic Stats | Huge rebounding, assists, steals, blocks | 25+ reb, 18+ ast, 7+ stl, 8+ blk |
| Blowout Comeback | Team erasing a big deficit | Was down 20+, now within 5 |
| Overtime | Game goes to OT | Any OT — alerts at tipoff and "Tied at the Buzzer" |
| GOAT Tracker | Career milestones approaching or broken | Per-milestone |

All thresholds are configurable per user in the web UI.

## Notification Channels

| Channel | Platform | Notes |
|---------|----------|-------|
| In-App Bell | All | Notifications in the web UI |
| Discord | All | Webhook URL |
| Telegram | All | Bot token + chat ID — recommended for iPhone |
| Browser Push | Android, Desktop | PWA push notifications |

> **iPhone users:** Use Telegram. Browser push notifications are blocked by Apple's push service for PWAs.

## GOAT Tracker

Checks career milestones once per day after all games go final. Uses per-game averages from ESPN to determine if a record could realistically be broken next game — no spam. Sent milestones are stored in the database and never re-sent, even after restarts.

Currently tracking **LeBron James** across points, assists, rebounds, steals, three-pointers, free throws, and games played.

Add more players by editing `milestones.json` with their ESPN ID and the milestones to watch.

## Smart Polling

| State | Interval |
|-------|----------|
| Games live | Every 30 seconds |
| Games scheduled, hours away | Sleeps until 5 min before tipoff |
| All games finished / no games today | Checks back every 30 minutes |

ESPN's API updates roughly every 30-60 seconds during live games, so total latency from event to notification is typically 30-90 seconds.

## Architecture

Each user gets their own independent `AlertEngine` with their own rules (built from their saved config), their own deduplication state, and their own game state tracking. The poll loop evaluates every user independently every 30 seconds — no shared state between users.

## Project Structure

```
├── webapp.py              # Web UI + monitor (main entrypoint)
├── database.py            # SQLite — users, configs, push subscriptions, milestones
├── alerts/
│   ├── engine.py          # AlertEngine with per-game deduplication
│   ├── milestones.py      # GOAT Tracker
│   └── nba/rules.py       # NBA alert rules
├── sports/
│   ├── espn.py            # ESPN API client
│   └── nba/provider.py    # NBA scoreboard + box score parsing
├── notifications/         # Discord, Telegram, desktop, console notifiers
├── milestones.json        # Player milestone definitions
├── Dockerfile
└── fly.toml               # Fly.io deployment config
```

## Deployment (Fly.io)

```bash
curl -L https://fly.io/install.sh | sh
fly auth login
fly launch          # detects fly.toml + Dockerfile automatically
fly volumes create courtside_data --region iad --size 1
fly secrets set SECRET_KEY="..." GOOGLE_CLIENT_ID="..." GOOGLE_CLIENT_SECRET="..."
fly secrets set VAPID_PRIVATE_KEY="..." VAPID_PUBLIC_KEY="..."
fly deploy
```

Useful commands:
```bash
fly logs            # live logs
fly ssh console     # SSH into the container
fly status          # check app health
```

## Data Source

ESPN's public scoreboard and summary APIs. No API key required.
