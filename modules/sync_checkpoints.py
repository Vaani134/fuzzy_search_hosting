"""
modules/sync_checkpoints.py
----------------------------
Per-table checkpoint persistence for crash-resume incremental sync.

Checkpoint is saved after every committed batch.  If the sync crashes or
is stopped, the next run resumes from `last_processed_id` /
`last_processed_updated_at` rather than row 0.

status values stored in sync_checkpoints.status:
  running   — table is currently being synced
  completed — table finished successfully in the last sync run
  stopped   — sync was gracefully stopped mid-table
  failed    — table encountered a fatal error during sync
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.database import get_connection


def get_checkpoint(db_id: int, table: str) -> Dict[str, Any]:
    """
    Return checkpoint state for (db_id, table).

    Returns a dict with keys:
      last_processed_id         : int  (0 if no checkpoint exists)
      last_processed_updated_at : str | None
      status                    : str  ('completed' / 'running' / 'stopped' / 'failed')
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT last_processed_id, last_processed_updated_at, status
              FROM sync_checkpoints
             WHERE database_id = ? AND table_name = ?
            """,
            (db_id, table),
        ).fetchone()
        if row:
            return {
                "last_processed_id":         row["last_processed_id"] or 0,
                "last_processed_updated_at": row["last_processed_updated_at"],
                "status":                    row["status"] or "completed",
            }
        return {
            "last_processed_id":         0,
            "last_processed_updated_at": None,
            "status":                    "completed",
        }
    except Exception:
        print(f"[CKPT] get_checkpoint failed: {traceback.format_exc()}")
        return {"last_processed_id": 0, "last_processed_updated_at": None, "status": "completed"}
    finally:
        conn.close()


def save_checkpoint(
    db_id:                    int,
    table:                    str,
    last_id:                  int,
    last_updated_at:          Optional[str],
    status:                   str = "running",
) -> None:
    """
    Upsert a checkpoint row for (db_id, table).

    Uses INSERT OR REPLACE so the UNIQUE(database_id, table_name) constraint
    guarantees exactly one row per (db, table) pair.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sync_checkpoints
                (database_id, table_name, last_processed_id,
                 last_processed_updated_at, status, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(database_id, table_name) DO UPDATE SET
                last_processed_id         = excluded.last_processed_id,
                last_processed_updated_at = excluded.last_processed_updated_at,
                status                    = excluded.status,
                updated_at                = CURRENT_TIMESTAMP
            """,
            (db_id, table, last_id, last_updated_at, status),
        )
        conn.commit()
    except Exception:
        print(f"[CKPT] save_checkpoint failed: {traceback.format_exc()}")
    finally:
        conn.close()


def clear_checkpoints(db_id: int) -> None:
    """Delete all checkpoint rows for *db_id* (called before a full sync)."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM sync_checkpoints WHERE database_id = ?", (db_id,)
        )
        conn.commit()
    except Exception:
        print(f"[CKPT] clear_checkpoints failed: {traceback.format_exc()}")
    finally:
        conn.close()


def get_all_checkpoints(db_id: int) -> List[Dict[str, Any]]:
    """Return all checkpoint rows for *db_id* as a list of dicts."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT table_name, last_processed_id,
                   last_processed_updated_at, status, updated_at
              FROM sync_checkpoints
             WHERE database_id = ?
             ORDER BY table_name
            """,
            (db_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        print(f"[CKPT] get_all_checkpoints failed: {traceback.format_exc()}")
        return []
    finally:
        conn.close()
