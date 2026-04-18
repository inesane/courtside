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
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_configs (
                user_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
        """)


def get_or_create_user(user_id: str | None) -> str:
    with _connect() as c:
        if user_id:
            row = c.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if row:
                return str(row["id"])
        new_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO users (id, created_at) VALUES (?, ?)",
            (new_id, datetime.now(timezone.utc).isoformat()),
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


def get_all_user_configs() -> list[dict[str, Any]]:
    """Return all users that have saved a config — used for per-user notification routing."""
    with _connect() as c:
        rows = c.execute("SELECT user_id, config_json FROM user_configs").fetchall()
        return [
            {"user_id": r["user_id"], "config": json.loads(r["config_json"])}
            for r in rows
        ]
