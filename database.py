"""SQLite-backed user identity and per-user configuration storage."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("DB_PATH", "courtside.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                google_id TEXT UNIQUE,
                email TEXT,
                name TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_configs (
                user_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sent_milestone_alerts (
                alert_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        # Migrate existing DBs that predate google_id/email/name columns
        existing = {row[1] for row in c.execute("PRAGMA table_info(users)")}
        for col, definition in [
            ("google_id", "TEXT UNIQUE"),
            ("email", "TEXT"),
            ("name", "TEXT"),
        ]:
            if col not in existing:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")


def get_or_create_google_user(google_id: str, email: str, name: str) -> str:
    """Look up a user by Google ID, creating one if this is their first login."""
    with _connect() as c:
        row = c.execute("SELECT id FROM users WHERE google_id = ?", (google_id,)).fetchone()
        if row:
            c.execute(
                "UPDATE users SET email = ?, name = ? WHERE google_id = ?",
                (email, name, google_id),
            )
            return str(row["id"])
        new_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO users (id, google_id, email, name, created_at) VALUES (?, ?, ?, ?, ?)",
            (new_id, google_id, email, name, datetime.now(timezone.utc).isoformat()),
        )
        return new_id


def load_user_config(user_id: str) -> dict[str, Any]:
    with _connect() as c:
        row = c.execute(
            "SELECT config_json FROM user_configs WHERE user_id = ?", (user_id,)
        ).fetchone()
        return json.loads(row["config_json"]) if row else {}


def save_user_config(user_id: str, config: dict[str, Any]) -> None:
    with _connect() as c:
        c.execute(
            """INSERT INTO user_configs (user_id, config_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   config_json = excluded.config_json,
                   updated_at = excluded.updated_at""",
            (user_id, json.dumps(config), datetime.now(timezone.utc).isoformat()),
        )


def save_push_subscription(user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
    with _connect() as c:
        c.execute(
            """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                   user_id = excluded.user_id,
                   p256dh = excluded.p256dh,
                   auth = excluded.auth""",
            (user_id, endpoint, p256dh, auth, datetime.now(timezone.utc).isoformat()),
        )


def delete_push_subscription(endpoint: str) -> None:
    with _connect() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def get_push_subscriptions_for_user(user_id: str) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [{"endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}} for r in rows]


def get_all_push_subscriptions() -> list[dict]:
    with _connect() as c:
        rows = c.execute("SELECT user_id, endpoint, p256dh, auth FROM push_subscriptions").fetchall()
        return [{"user_id": r["user_id"], "endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}} for r in rows]


def milestone_already_sent(alert_key: str) -> bool:
    with _connect() as c:
        row = c.execute("SELECT 1 FROM sent_milestone_alerts WHERE alert_key = ?", (alert_key,)).fetchone()
        return row is not None


def mark_milestone_sent(alert_key: str) -> None:
    with _connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO sent_milestone_alerts (alert_key, sent_at) VALUES (?, ?)",
            (alert_key, datetime.now(timezone.utc).isoformat()),
        )


def get_all_user_configs() -> list[dict[str, Any]]:
    """Return all users that have saved a config — used for per-user notification routing."""
    with _connect() as c:
        rows = c.execute("SELECT user_id, config_json FROM user_configs").fetchall()
        return [
            {"user_id": r["user_id"], "config": json.loads(r["config_json"])}
            for r in rows
        ]
