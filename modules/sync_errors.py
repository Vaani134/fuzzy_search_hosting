"""
modules/sync_errors.py
----------------------
Row-level sync failure logging.

One row is written to `sync_errors` per failing MySQL row during upsert.
Failures here must NEVER propagate — the sync loop catches them and continues.
"""

from __future__ import annotations

import json
import os
import sys
import traceback as _tb
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.database import get_connection


# Caps on stored payload sizes to keep the DB lean
_MAX_PAYLOAD_BYTES = 8_192   # 8 KB
_MAX_TB_BYTES      = 4_096   # 4 KB


def log_row_error(
    db_id:         int,
    table:         str,
    source_row_id: Optional[int],
    exc:           Exception,
    raw_row:       Optional[Dict[str, Any]] = None,
) -> None:
    """
    Persist one row-level sync failure to the sync_errors table.

    Never raises — if the DB write itself fails we print to stderr and return.
    """
    try:
        error_message = str(exc)[:1024]
        tb_str        = _tb.format_exc()

        raw_payload: Optional[str] = None
        if raw_row is not None:
            try:
                raw_payload = json.dumps(
                    {k: _json_safe(v) for k, v in raw_row.items()},
                    ensure_ascii=False,
                )[:_MAX_PAYLOAD_BYTES]
            except Exception:
                raw_payload = None

        tb_truncated = tb_str[:_MAX_TB_BYTES] if tb_str else None

        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO sync_errors
                    (database_id, table_name, source_row_id, error_message,
                     raw_payload, traceback)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (db_id, table, source_row_id, error_message,
                 raw_payload, tb_truncated),
            )
            conn.commit()
        finally:
            conn.close()

    except Exception:
        # Absolute last resort — never let error logging crash the sync
        print(
            f"[SYNC_ERRORS] Failed to log error for db={db_id} table={table}: "
            f"{_tb.format_exc()}",
            file=sys.stderr,
        )


def get_recent_errors(db_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """Return the most recent *limit* error rows for *db_id*, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, database_id, table_name, source_row_id,
                   error_message, raw_payload, traceback, created_at
              FROM sync_errors
             WHERE database_id = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (db_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        print(f"[SYNC_ERRORS] get_recent_errors failed: {_tb.format_exc()}", file=sys.stderr)
        return []
    finally:
        conn.close()


def get_error_detail(error_id: int) -> Optional[Dict[str, Any]]:
    """Return a single sync_errors row by primary key, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sync_errors WHERE id = ?", (error_id,)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        print(f"[SYNC_ERRORS] get_error_detail failed: {_tb.format_exc()}", file=sys.stderr)
        return None
    finally:
        conn.close()


def clear_errors(db_id: int) -> int:
    """Delete all sync_errors rows for *db_id*.  Returns deleted row count."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM sync_errors WHERE database_id = ?", (db_id,)
        )
        conn.commit()
        return cur.rowcount
    except Exception:
        print(f"[SYNC_ERRORS] clear_errors failed: {_tb.format_exc()}", file=sys.stderr)
        return 0
    finally:
        conn.close()


def get_error_count(db_id: int) -> int:
    """Fast count of sync_errors rows for *db_id*."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sync_errors WHERE database_id = ?", (db_id,)
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _json_safe(value: Any) -> Any:
    """Convert non-JSON-serializable types to something safe."""
    from decimal import Decimal
    from datetime import datetime, date
    if isinstance(value, (Decimal,)):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
