"""
db/database.py
--------------
SQLite connection helper.
Initialises the schema on first run.
"""

import sqlite3
import os
import sys

# Allow imports from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SQLITE_PATH, SCHEMA_PATH


def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection with row_factory set to Row."""
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")   # better concurrent read performance
    # FK checks are enabled per-operation where needed; off by default for sync safety
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def init_db() -> None:
    """
    Create tables from schema.sql if they don't exist yet.
    Also runs any incremental migrations for existing databases
    (e.g. adding the search_history table to older installs).
    """
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = get_connection()
    try:
        conn.executescript(sql)
        conn.commit()
        print(f"[DB] SQLite initialised at {SQLITE_PATH}")
    finally:
        conn.close()

    # ── Incremental migrations ─────────────────────────────────────────────────
    # These are idempotent — safe to run on every startup.
    _run_migrations()


def _run_migrations() -> None:
    """
    Apply schema additions that may be missing from older database files.
    Each migration is wrapped in a try/except so a single failure does not
    prevent the app from starting.
    """
    conn = get_connection()
    try:
        # Migration 1: search_history table (added in v2)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                query        TEXT    NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0,
                timestamp    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_search_history_query "
            "ON search_history(query)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_search_history_timestamp "
            "ON search_history(timestamp)"
        )
        conn.commit()
    except Exception as exc:
        print(f"[DB] Migration warning: {exc}")
    finally:
        conn.close()


def dict_from_row(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)
