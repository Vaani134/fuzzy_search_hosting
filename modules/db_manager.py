"""
modules/db_manager.py
---------------------
CRUD operations for the connected_databases table.

Replaces the flat db_settings.json approach for multi-ERP support.
The old settings_manager.py is kept alive for backward compatibility
(existing /api/settings endpoints continue to work unchanged).

Design principles:
- Passwords are stored as-is in SQLite (protect the DB file at the OS level).
- list_databases() masks passwords so they are safe to return in API responses.
- get_mysql_config() returns a pymysql-compatible dict including the password.
- migrate_from_settings_file() is idempotent — safe to call on every startup.
"""

import os
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.database import get_connection


# ── Public read API ────────────────────────────────────────────────────────────

def list_databases() -> list:
    """
    Return all connected databases.
    Passwords are masked — never returned to the frontend.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, name, host, port, username, database_name,
                   last_sync_at, sync_status, created_at, updated_at
            FROM connected_databases
            ORDER BY id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_database(db_id: int) -> Optional[dict]:
    """
    Return a connected database by ID, including the password.
    Used internally by sync code — do NOT expose this directly in APIs.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM connected_databases WHERE id = ?",
            (db_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_database_masked(db_id: int) -> Optional[dict]:
    """Return a connected database with the password masked — safe for API responses."""
    db = get_database(db_id)
    if db:
        db["password"] = "••••••••" if db.get("password") else ""
    return db


# ── Write API ──────────────────────────────────────────────────────────────────

def add_database(
    name: str,
    host: str,
    port: int,
    username: str,
    password: str,
    database_name: str,
) -> int:
    """
    Insert a new connected database.
    Returns the new auto-generated id.
    Raises ValueError if a required field is missing.
    """
    if not all([name.strip(), host.strip(), database_name.strip()]):
        raise ValueError("name, host, and database_name are required.")

    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """
            INSERT INTO connected_databases
                (name, host, port, username, password, database_name,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name.strip(), host.strip(), int(port),
             username.strip(), password,
             database_name.strip(), now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_database(db_id: int, **fields) -> bool:
    """
    Update one or more fields on a connected_database row.
    Accepted fields: name, host, port, username, password, database_name.
    Returns True if a row was actually updated, False if not found.
    """
    _allowed = {"name", "host", "port", "username", "password", "database_name"}
    updates = {k: v for k, v in fields.items() if k in _allowed}
    if not updates:
        return False

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE connected_databases SET {set_clause} WHERE id = ?",
            (*updates.values(), db_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_database(db_id: int) -> bool:
    """
    Delete a connected database by ID.
    Returns True if a row was removed.
    Raises RuntimeError if a sync is currently running for this database.
    """
    conn = get_connection()
    try:
        running = conn.execute(
            "SELECT id FROM sync_jobs WHERE database_id = ? AND status = 'running'",
            (db_id,),
        ).fetchone()
        if running:
            raise RuntimeError(
                f"Cannot delete database {db_id} while a sync is running. "
                "Stop the sync first."
            )
        cur = conn.execute(
            "DELETE FROM connected_databases WHERE id = ?", (db_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_sync_status(
    db_id: int,
    status: str,
    last_sync_at: Optional[str] = None,
) -> None:
    """
    Update sync_status and optionally last_sync_at for a connected database.
    Called by sync_manager at the start and end of each sync run.
    """
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if last_sync_at:
            conn.execute(
                """
                UPDATE connected_databases
                SET sync_status = ?, last_sync_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, last_sync_at, now, db_id),
            )
        else:
            conn.execute(
                """
                UPDATE connected_databases
                SET sync_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, db_id),
            )
        conn.commit()
    finally:
        conn.close()


# ── MySQL config builder ───────────────────────────────────────────────────────

def get_mysql_config(db_id: int) -> dict:
    """
    Return a pymysql-compatible connection dict for the given database ID.
    Raises ValueError if the database does not exist.
    """
    db = get_database(db_id)
    if not db:
        raise ValueError(f"No connected database with id={db_id}.")
    return {
        "host":     db["host"].strip(),
        "port":     int(db["port"]),
        "user":     db["username"].strip(),
        "password": db["password"],
        "database": db["database_name"].strip(),
        "charset":  "utf8mb4",
    }


# ── Connection test ────────────────────────────────────────────────────────────

def test_connection(db_id: int) -> dict:
    """
    Test the MySQL connection for the given database ID.
    Returns {"ok": bool, "message": str, "detail": str}.
    """
    try:
        cfg = get_mysql_config(db_id)
    except ValueError as e:
        return {"ok": False, "message": str(e), "detail": ""}

    try:
        import pymysql
    except ImportError:
        return {
            "ok":      False,
            "message": "pymysql not installed",
            "detail":  "Run: pip install pymysql",
        }

    try:
        conn = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset="utf8mb4",
            connect_timeout=6,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        conn.close()
        return {
            "ok":      True,
            "message": "Connection successful",
            "detail":  f"MySQL {version} — database '{cfg['database']}' accessible.",
        }
    except Exception as exc:
        return {
            "ok":      False,
            "message": "Connection failed",
            "detail":  str(exc),
        }
