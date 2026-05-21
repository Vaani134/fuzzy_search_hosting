"""
modules/sync_manager.py
-----------------------
Production-safe multi-database incremental sync engine.

Architecture overview
=====================
  connected_databases   — one row per MySQL/ERP source (managed by db_manager.py)
  sync_jobs             — one row per sync run; carries the stop_requested flag
  sync_checkpoints      — one row per (database, table); enables crash-resume
  product_metrics       — aggregated sales stats; replaces full transaction sync

Sub-modules used
================
  sync_normalization  — per-table row cleaners; converts NULL → safe defaults
  sync_errors         — persists row-level failures to sync_errors table
  sync_live_logs      — in-memory ring-buffer log stream per database
  sync_checkpoints    — SQLite checkpoint CRUD with per-table status tracking

Sync modes
==========
  full=True   Deletes this source's existing rows then re-imports everything.
  full=False  Incremental: dual-key cursor covers rows newer than last_sync_at.
  full=None   Auto-detect: full on first ever sync, incremental thereafter.

Enterprise incremental cursor
=============================
  Single-column cursor (WHERE id > last_id) misses rows that share an
  updated_at boundary with the resume point.  This engine uses a dual-key
  cursor that is both safe and gap-free:

    WHERE (updated_at > %s OR (updated_at = %s AND id > %s))
    ORDER BY updated_at, id
    LIMIT BATCH_SIZE

  The cursor advances by recording (updated_at, id) from the last row of
  every batch.  A fresh MySQL connection is opened per batch to prevent
  WinError 10054 idle-timeout disconnects on long syncs.

Row-level error isolation
=========================
  Rows are upserted one at a time inside try/except blocks.  A single bad
  row (NULL in a NOT NULL column, type mismatch, FK violation) is logged
  to sync_errors and skipped — it never aborts the batch or the sync run.

Stop mechanism
==============
  POST /api/database/<id>/stop  sets stop_requested = 1 in sync_jobs.
  The sync loop calls _is_stop_requested() before every batch.
  When stop is detected the current checkpoint is saved and the loop exits.
  The thread terminates naturally — no force-kill, no orphaned connections.

Index rebuild
=============
  The in-memory FuzzySearchEngine is rebuilt ONLY after a SUCCESSFUL full
  sync completion.  Partial, failed, and incremental syncs do NOT trigger a
  rebuild to avoid exposing incomplete data to live search queries.
"""

import copy
import sqlite3
import threading
import sys
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SYNC_BATCH_SIZE
from db.database import get_connection

# Sub-module imports
from modules.sync_normalization import normalize_row
from modules.sync_errors import log_row_error, get_recent_errors, get_error_count
from modules.sync_live_logs import append_log, get_logs, clear_logs
from modules.sync_checkpoints import (
    get_checkpoint,
    save_checkpoint,
    clear_checkpoints,
    get_all_checkpoints,
)

try:
    import pymysql
    PYMYSQL_AVAILABLE = True
except ImportError:
    PYMYSQL_AVAILABLE = False


# ── Tables synced from MySQL (transactions excluded — use product_metrics) ─────
CORE_SYNC_TABLES = ["brands", "categories", "product_group", "products"]

# ── Columns fetched from MySQL per table (must match SQLite schema exactly) ────
TABLE_COLUMNS: Dict[str, list] = {
    "brands": [
        "id", "business_id", "name", "description",
        "created_by", "deleted_at", "created_at", "updated_at",
    ],
    "categories": [
        "id", "name", "business_id", "short_code", "parent_id",
        "created_by", "category_type", "description", "slug",
        "deleted_at", "created_at", "updated_at",
    ],
    "product_group": [
        "id", "name", "created_by", "created_at", "updated_at",
    ],
    "products": [
        "id", "name", "item_code", "business_id", "type",
        "brand_id", "category_id", "sub_category_id",
        "sku", "sku2", "sku3", "barcode_type",
        "enable_stock", "alert_quantity", "weight",
        "image", "main_image", "product_description",
        "product_custom_field1", "product_custom_field2",
        "product_custom_field3", "product_custom_field4",
        "srp", "sales_price", "is_inactive", "not_for_selling",
        "out_of_stock", "aisle", "rack", "shelf", "bin",
        "qty_box", "case_qty", "master_case_qty", "ml",
        "product_group_id", "group_variation_name", "note",
        "created_by", "created_at", "updated_at",
        # synced_at is NOT in TABLE_COLUMNS — it's injected by _upsert_row()
    ],
}


# ── Live state (in-memory, per database_id) ────────────────────────────────────
_state_lock  = threading.Lock()
_live_states: dict = {}
_ID_SCOPE_MULTIPLIER = 1_000_000_000


def _scoped_id(source_db_id: int, source_id: Optional[int]) -> int:
    """Map source-local integer IDs into a globally unique SQLite ID space."""
    if source_id is None:
        raise ValueError("Cannot scope a null source id.")
    return int(source_db_id) * _ID_SCOPE_MULTIPLIER + int(source_id)


def _scope_foreign_keys(table: str, clean_row: dict, source_db_id: int) -> dict:
    """
    Rewrite PK/FK IDs so each connected database has isolated key-space.
    """
    scoped = dict(clean_row)
    if scoped.get("id") is not None:
        scoped["id"] = _scoped_id(source_db_id, scoped["id"])

    if table == "products":
        for fk_col in ("brand_id", "category_id", "sub_category_id", "product_group_id"):
            if scoped.get(fk_col) is not None:
                scoped[fk_col] = _scoped_id(source_db_id, scoped[fk_col])
    return scoped


def _init_live_state(db_id: int, mode: str, job_id: int) -> None:
    with _state_lock:
        _live_states[db_id] = {
            "db_id":       db_id,
            "running":     True,
            "started_at":  datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "mode":        mode,
            "job_id":      job_id,
            "rows_synced": 0,
            "rows_skipped": 0,
            "rows_error":  0,
            "tables": {
                t: {
                    "status":      "pending",
                    "rows_synced": 0,
                    "rows_skipped": 0,
                    "rows_error":  0,
                    "batch_count": 0,
                    "error":       None,
                    "started_at":  None,
                    "finished_at": None,
                }
                for t in CORE_SYNC_TABLES + ["product_metrics"]
            },
        }


def _update_live_state(db_id: int, **kwargs) -> None:
    with _state_lock:
        if db_id in _live_states:
            _live_states[db_id].update(kwargs)


def _update_table_live(db_id: int, table: str, **kwargs) -> None:
    with _state_lock:
        state  = _live_states.get(db_id, {})
        tables = state.setdefault("tables", {})
        if table not in tables:
            tables[table] = {
                "status": "pending", "rows_synced": 0,
                "rows_skipped": 0, "rows_error": 0,
                "batch_count": 0, "error": None,
                "started_at": None, "finished_at": None,
            }
        tables[table].update(kwargs)
        # Propagate totals to top-level counters
        totals = {"rows_synced": 0, "rows_skipped": 0, "rows_error": 0}
        for t in tables.values():
            for key in totals:
                totals[key] += t.get(key, 0)
        state.update(totals)


def get_live_state(db_id: int) -> dict:
    """Return a deep-copy of the live sync state for one database."""
    with _state_lock:
        return copy.deepcopy(_live_states.get(db_id, {"db_id": db_id, "running": False}))


def get_all_live_states() -> dict:
    """Return live states for all known databases."""
    with _state_lock:
        return copy.deepcopy(_live_states)


# ── MySQL helpers ──────────────────────────────────────────────────────────────

def _open_mysql(cfg: dict):
    """Open and return a fresh pymysql connection from a config dict."""
    if not PYMYSQL_AVAILABLE:
        raise RuntimeError("pymysql is not installed.  Run: pip install pymysql")
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=300,
        write_timeout=300,
    )


# ── Sync job management ────────────────────────────────────────────────────────

def _create_job(db_id: int) -> int:
    """Insert a pending sync_jobs row.  Returns the new job id."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO sync_jobs (database_id, status, created_at) VALUES (?, 'pending', ?)",
            (db_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _update_job(job_id: int, **fields) -> None:
    """Patch one or more fields on a sync_jobs row."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE sync_jobs SET {set_clause} WHERE id = ?",
            (*fields.values(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _is_stop_requested(job_id: int) -> bool:
    """Check whether the API has requested a graceful stop for this job."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT stop_requested FROM sync_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return bool(row and row["stop_requested"])
    finally:
        conn.close()


def get_active_job(db_id: int) -> Optional[dict]:
    """Return the currently-running sync_job for a database, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sync_jobs WHERE database_id = ? AND status = 'running' "
            "ORDER BY id DESC LIMIT 1",
            (db_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def request_stop(db_id: int) -> dict:
    """
    Request a graceful stop for the running sync on this database.
    Sets stop_requested = 1 — the sync loop will exit after its current batch.
    """
    job = get_active_job(db_id)
    if not job:
        return {"ok": False, "message": "No active sync job for this database."}

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE sync_jobs SET stop_requested = 1 WHERE id = ?", (job["id"],)
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "message": f"Stop requested for sync job {job['id']}."}


# ── Sync log helper ────────────────────────────────────────────────────────────

def _log_sync(
    sqlite_conn: sqlite3.Connection,
    table: str,
    count: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Append one row to sync_log (used by the existing history endpoint)."""
    sqlite_conn.execute(
        "INSERT INTO sync_log (table_name, last_synced, records_synced, status, error_msg) "
        "VALUES (?, ?, ?, ?, ?)",
        (table, datetime.now(timezone.utc).isoformat(), count, status, error),
    )


# ── Row-level upsert ───────────────────────────────────────────────────────────

def _upsert_row(
    sqlite_conn: sqlite3.Connection,
    table: str,
    clean_row: dict,
    source_db_id: int,
    now: str,
) -> None:
    """
    Insert-or-update a single pre-normalized row.

    Includes source_db_id and (for products) synced_at set to wall-clock time.
    Raises on SQLite error — caller is responsible for catching per-row.
    """
    cols = list(TABLE_COLUMNS[table])

    # For products inject synced_at at write time (not from ERP)
    extra_cols: list = ["source_db_id"]
    extra_vals: list = [source_db_id]
    if table == "products":
        extra_cols.append("synced_at")
        extra_vals.append(now)

    all_cols      = cols + extra_cols
    placeholders  = ", ".join("?" * len(all_cols))
    col_names     = ", ".join(all_cols)
    update_cols   = [c for c in all_cols if c != "id"]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)

    sql = (
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
    )

    clean_row = _scope_foreign_keys(table, clean_row, source_db_id)
    values = [clean_row.get(c) for c in cols] + extra_vals
    sqlite_conn.execute(sql, values)


def _upsert_batch_isolated(
    db_id: int,
    sqlite_conn: sqlite3.Connection,
    table: str,
    raw_rows: list,
    source_db_id: int,
) -> Tuple[int, int, int]:
    """
    Upsert a batch of raw MySQL rows with full row-level error isolation.

    Each row is:
      1. Normalized via normalize_row() (NULL-safe, type-safe).
      2. Upserted individually inside try/except.
      3. On failure: logged to sync_errors, counted as skipped.

    Returns (synced, skipped, errors) counts.
    """
    synced = skipped = errors = 0
    now    = datetime.now(timezone.utc).isoformat()

    for raw_row in raw_rows:
        source_id = raw_row.get("id")
        try:
            clean = normalize_row(table, raw_row)
            if clean is None:
                # normalize_row() returned None — already printed traceback
                skipped += 1
                log_row_error(
                    db_id, table, source_id,
                    ValueError("normalize_row returned None"),
                    raw_row,
                )
                continue

            _upsert_row(sqlite_conn, table, clean, source_db_id, now)
            synced += 1

        except Exception as exc:
            errors  += 1
            skipped += 1
            log_row_error(db_id, table, source_id, exc, raw_row)

    return synced, skipped, errors


# ── Single-table sync ──────────────────────────────────────────────────────────

def _sync_table(
    mysql_cfg: dict,
    db_id: int,
    job_id: int,
    table: str,
    full: bool,
    since: Optional[str],
    checkpoint: Optional[dict],
) -> dict:
    """
    Sync one MySQL table into SQLite using a dual-key enterprise cursor.

    Cursor strategy (enterprise incremental):
      ORDER BY updated_at, id — stable deterministic ordering.
      WHERE (updated_at > %s OR (updated_at = %s AND id > %s))
      → gap-free even when multiple rows share the same updated_at value.
      → Fresh MySQL connection per batch to prevent WinError 10054.

    For full sync the cursor degenerates to WHERE id > %s ORDER BY id
    because we don't need updated_at filtering.

    Row-level isolation:
      Each row is upserted inside try/except via _upsert_batch_isolated().
      One bad row never aborts the batch; it is logged to sync_errors.

    Checkpoint:
      Saved after every committed batch.  If the job is stopped mid-table
      the next run resumes from last_processed_id / last_processed_updated_at.
    """
    result: dict = {
        "table":        table,
        "rows_synced":  0,
        "rows_skipped": 0,
        "rows_error":   0,
        "batch_count":  0,
        "status":       "ok",
        "error":        None,
    }
    sqlite_conn     = None
    last_id         = 0      # safe defaults so except block can always reference them
    last_updated_at = None
    table_start     = datetime.now(timezone.utc)

    _update_table_live(db_id, table, status="running",
                       started_at=table_start.isoformat())
    append_log(db_id, "INFO",
               f"[{table}] Starting {'full' if full else 'incremental'} sync",
               table=table)

    try:
        sqlite_conn = get_connection()
        sqlite_conn.execute("PRAGMA foreign_keys = OFF")

        # ── Full sync: wipe this source's rows before reimporting ─────────────
        if full:
            sqlite_conn.execute(
                f"DELETE FROM {table} WHERE source_db_id = ?", (db_id,)
            )
            sqlite_conn.commit()
            last_id         = 0
            last_updated_at = None
            append_log(db_id, "INFO",
                       f"[{table}] Full sync: deleted existing rows for source_db_id={db_id}",
                       table=table)
        else:
            if checkpoint and checkpoint["last_processed_id"]:
                last_id         = checkpoint["last_processed_id"]
                last_updated_at = checkpoint["last_processed_updated_at"]
                append_log(db_id, "INFO",
                           f"[{table}] Resuming from checkpoint id={last_id} "
                           f"updated_at={last_updated_at}",
                           table=table)
            else:
                last_id         = 0
                last_updated_at = since
                append_log(db_id, "INFO",
                           f"[{table}] Incremental sync since={since}",
                           table=table)

        cols     = TABLE_COLUMNS[table]
        col_list = ", ".join(f"`{c}`" for c in cols)
        total_synced = total_skipped = total_errors = batch_num = 0

        # Mark checkpoint as running so a crash mid-table is visible
        save_checkpoint(db_id, table, last_id, last_updated_at, status="running")

        while True:
            # ── Stop check ────────────────────────────────────────────────────
            if _is_stop_requested(job_id):
                save_checkpoint(db_id, table, last_id, last_updated_at,
                                status="stopped")
                append_log(db_id, "WARNING",
                           f"[{table}] Stop requested — halting after batch {batch_num}",
                           table=table)
                result["status"] = "stopped"
                _update_table_live(
                    db_id, table, status="stopped",
                    rows_synced=total_synced, rows_skipped=total_skipped,
                    rows_error=total_errors, batch_count=batch_num,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                return result

            # ── Build MySQL query ──────────────────────────────────────────────
            if full:
                # Full sync: simple id cursor, ORDER BY id
                query = (
                    f"SELECT {col_list} FROM `{table}` "
                    f"WHERE id > %s ORDER BY id LIMIT %s"
                )
                params = (last_id, SYNC_BATCH_SIZE)
            elif last_updated_at:
                # Enterprise dual-key cursor: no-gap incremental
                query = (
                    f"SELECT {col_list} FROM `{table}` "
                    f"WHERE (updated_at > %s OR (updated_at = %s AND id > %s)) "
                    f"ORDER BY updated_at, id LIMIT %s"
                )
                params = (last_updated_at, last_updated_at, last_id, SYNC_BATCH_SIZE)
            else:
                # Incremental but no since timestamp — fall back to full cursor
                query = (
                    f"SELECT {col_list} FROM `{table}` "
                    f"WHERE id > %s ORDER BY id LIMIT %s"
                )
                params = (last_id, SYNC_BATCH_SIZE)

            # ── Fetch batch (fresh connection → no idle-timeout) ───────────────
            mysql_conn = _open_mysql(mysql_cfg)
            try:
                with mysql_conn.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            finally:
                mysql_conn.close()

            if not rows:
                break

            batch_num += 1

            # ── Row-level isolated upsert ──────────────────────────────────────
            synced, skipped, errors = _upsert_batch_isolated(
                db_id, sqlite_conn, table, rows, source_db_id=db_id
            )
            total_synced  += synced
            total_skipped += skipped
            total_errors  += errors

            # Advance dual-key cursor from last row
            last_row        = rows[-1]
            last_id         = last_row["id"]
            last_updated_at = last_row.get("updated_at")

            # ── Commit + save checkpoint ───────────────────────────────────────
            sqlite_conn.commit()
            save_checkpoint(db_id, table, last_id, last_updated_at,
                            status="running")

            _update_table_live(
                db_id, table,
                rows_synced=total_synced, rows_skipped=total_skipped,
                rows_error=total_errors, batch_count=batch_num,
            )
            _update_job(job_id, current_table=table)

            elapsed = (datetime.now(timezone.utc) - table_start).seconds
            append_log(db_id, "DEBUG",
                       f"[{table}] Batch {batch_num}: "
                       f"+{synced} synced, {skipped} skipped, {errors} errors "
                       f"| total={total_synced} | cursor id={last_id} "
                       f"| elapsed={elapsed}s",
                       table=table, batch=batch_num)

        # ── Table complete ─────────────────────────────────────────────────────
        _log_sync(sqlite_conn, table, total_synced, "ok")
        sqlite_conn.commit()
        save_checkpoint(db_id, table, last_id, last_updated_at, status="completed")

        elapsed = (datetime.now(timezone.utc) - table_start).seconds
        result.update(rows_synced=total_synced, rows_skipped=total_skipped,
                      rows_error=total_errors, batch_count=batch_num)

        _update_table_live(
            db_id, table, status="ok",
            rows_synced=total_synced, rows_skipped=total_skipped,
            rows_error=total_errors, batch_count=batch_num,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        append_log(
            db_id, "INFO",
            f"[{table}] Done: {total_synced} synced, {total_skipped} skipped, "
            f"{total_errors} errors, {batch_num} batches, {elapsed}s elapsed",
            table=table,
        )

    except Exception as exc:
        result["status"] = "error"
        result["error"]  = str(exc)
        save_checkpoint(db_id, table, last_id, last_updated_at, status="failed")
        _update_table_live(
            db_id, table, status="error", error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        append_log(db_id, "ERROR", f"[{table}] FATAL: {exc}", table=table)
        if sqlite_conn:
            try:
                _log_sync(sqlite_conn, table, result.get("rows_synced", 0),
                          "error", str(exc))
                sqlite_conn.commit()
            except Exception:
                pass

    finally:
        if sqlite_conn:
            try:
                sqlite_conn.execute("PRAGMA foreign_keys = ON")
                sqlite_conn.close()
            except Exception:
                pass

    return result


# ── Product metrics aggregation ────────────────────────────────────────────────

def _sync_product_metrics(mysql_cfg: dict, db_id: int, job_id: int) -> dict:
    """
    Aggregate per-product sales metrics from MySQL in one query.

    Replaces the expensive full sync of transactions + transaction_sell_lines.
    Metrics (sales_count, popularity_score, last_sold_at) are upserted into
    product_metrics with ON CONFLICT on (product_id, source_db_id).
    """
    result: dict = {
        "table":        "product_metrics",
        "rows_synced":  0,
        "rows_skipped": 0,
        "rows_error":   0,
        "batch_count":  1,
        "status":       "ok",
        "error":        None,
    }
    start = datetime.now(timezone.utc)

    _update_table_live(db_id, "product_metrics", status="running",
                       started_at=start.isoformat())
    append_log(db_id, "INFO", "[product_metrics] Starting aggregation query",
               table="product_metrics")

    try:
        if _is_stop_requested(job_id):
            result["status"] = "stopped"
            _update_table_live(db_id, "product_metrics", status="stopped",
                               finished_at=datetime.now(timezone.utc).isoformat())
            append_log(db_id, "WARNING",
                       "[product_metrics] Skipped — stop requested",
                       table="product_metrics")
            return result

        mysql_conn = _open_mysql(mysql_cfg)
        try:
            with mysql_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        tsl.product_id                  AS product_id,
                        COUNT(DISTINCT t.id)            AS sales_count,
                        SUM(tsl.quantity)               AS total_qty,
                        MAX(t.transaction_date)         AS last_sold_at
                    FROM transaction_sell_lines tsl
                    INNER JOIN transactions t
                        ON t.id = tsl.transaction_id
                       AND t.type   = 'sell'
                       AND t.status = 'final'
                    GROUP BY tsl.product_id
                    """
                )
                rows = cur.fetchall()
        finally:
            mysql_conn.close()

        append_log(db_id, "INFO",
                   f"[product_metrics] Aggregation returned {len(rows)} products",
                   table="product_metrics")

        if not rows:
            _update_table_live(db_id, "product_metrics", status="ok", rows_synced=0,
                               finished_at=datetime.now(timezone.utc).isoformat())
            return result

        max_qty = max(float(r.get("total_qty") or 0) for r in rows) or 1.0
        now     = datetime.now(timezone.utc).isoformat()
        synced = skipped = errors = 0

        sqlite_conn = get_connection()
        try:
            for r in rows:
                try:
                    pid       = _scoped_id(db_id, int(r["product_id"]))
                    s_count   = int(r.get("sales_count") or 0)
                    pop_score = round(float(r.get("total_qty") or 0) / max_qty, 4)
                    last_sold = r.get("last_sold_at")
                    if hasattr(last_sold, "isoformat"):
                        last_sold = last_sold.isoformat()

                    sqlite_conn.execute(
                        """
                        INSERT INTO product_metrics
                            (product_id, source_db_id, sales_count,
                             popularity_score, last_sold_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(product_id, source_db_id) DO UPDATE SET
                            sales_count      = excluded.sales_count,
                            popularity_score = excluded.popularity_score,
                            last_sold_at     = excluded.last_sold_at,
                            updated_at       = excluded.updated_at
                        """,
                        (pid, db_id, s_count, pop_score, last_sold, now),
                    )
                    synced += 1
                except Exception as exc:
                    errors  += 1
                    skipped += 1
                    log_row_error(db_id, "product_metrics",
                                  r.get("product_id"), exc, dict(r))

            sqlite_conn.commit()
        finally:
            sqlite_conn.close()

        elapsed = (datetime.now(timezone.utc) - start).seconds
        result.update(rows_synced=synced, rows_skipped=skipped, rows_error=errors)
        _update_table_live(
            db_id, "product_metrics", status="ok",
            rows_synced=synced, rows_skipped=skipped, rows_error=errors,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        append_log(
            db_id, "INFO",
            f"[product_metrics] Done: {synced} upserted, {skipped} skipped, "
            f"{errors} errors, {elapsed}s elapsed",
            table="product_metrics",
        )

    except Exception as exc:
        result["status"] = "error"
        result["error"]  = str(exc)
        _update_table_live(db_id, "product_metrics", status="error", error=str(exc),
                           finished_at=datetime.now(timezone.utc).isoformat())
        append_log(db_id, "ERROR",
                   f"[product_metrics] FATAL: {exc}", table="product_metrics")

    return result


# ── Main entry points ──────────────────────────────────────────────────────────

def _check_is_resuming(db_id: int) -> bool:
    """
    Return True when the last sync job for *db_id* ended in 'stopped' or 'failed'.

    This distinguishes a crash/stop-resume from a fresh incremental:
      - stopped / failed  → resume: respect existing checkpoints, skip completed tables
      - completed / never → fresh:  ignore old checkpoints, run all tables from 'since'
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM sync_jobs "
            "WHERE database_id = ? AND status NOT IN ('pending', 'running') "
            "ORDER BY id DESC LIMIT 1",
            (db_id,),
        ).fetchone()
        return bool(row and row["status"] in ("stopped", "failed"))
    except Exception:
        return False
    finally:
        conn.close()


def sync_database(db_id: int, full: bool = None) -> dict:
    """
    Sync one connected database into SQLite.

    Sequence:
      1. Resolve sync mode (full / incremental / auto-detect).
      2. Create a sync_jobs row and update connected_databases.sync_status.
      3. Sync core tables in dependency order: brands → categories →
         product_group → products.
      4. After each batch, check stop_requested — exit cleanly if set.
      5. Sync product_metrics (aggregate query, no raw transactions).
      6. On success: mark job completed, update last_sync_at.
      7. Rebuild in-memory search index ONLY on successful FULL sync.

    Returns a result dict:
      {"status": "ok"|"error"|"stopped", "db_id": int, "tables": [...]}
    """
    from modules.db_manager import get_database, get_mysql_config, update_sync_status

    db = get_database(db_id)
    if not db:
        raise ValueError(f"No connected database with id={db_id}.")

    if get_active_job(db_id):
        raise RuntimeError(
            f"Database {db_id} already has a running sync job. "
            "Use POST /api/database/<id>/stop to cancel it first."
        )

    if full is None:
        full = (db["last_sync_at"] is None)

    mode  = "full" if full else "incremental"
    since = None if full else db["last_sync_at"]

    job_id = _create_job(db_id)
    clear_logs(db_id)
    _init_live_state(db_id, mode, job_id)
    update_sync_status(db_id, "running")
    _update_job(
        job_id,
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    sync_start = datetime.now(timezone.utc)
    append_log(db_id, "INFO",
               f"Sync starting | mode={mode} | since={since or 'beginning'} "
               f"| job_id={job_id}")
    print(f"\n{'='*55}")
    print(f"[DB {db_id}] {mode.upper()} sync starting — {datetime.now()}")
    print(f"  since={since or 'beginning'}  job_id={job_id}")
    print(f"{'='*55}")

    # Determine resume vs fresh before entering the try block
    _is_resuming = False

    try:
        if full:
            clear_checkpoints(db_id)
            append_log(db_id, "INFO", "Full sync: checkpoints cleared")
        else:
            # Decide whether to resume from checkpoints or start fresh.
            # Only crash-resume (last job stopped/failed) should use checkpoints.
            # A fresh incremental after a completed sync must fetch rows from
            # 'since' — NOT skip tables because their old checkpoint says "completed".
            _is_resuming = _check_is_resuming(db_id)
            if _is_resuming:
                append_log(db_id, "INFO",
                           "Resuming stopped/failed sync — checkpoints active")
            else:
                # Clear stale checkpoints so every table runs from 'since' cursor.
                clear_checkpoints(db_id)
                append_log(db_id, "INFO",
                           f"Fresh incremental sync | since={since or 'beginning'}")

        mysql_cfg    = get_mysql_config(db_id)
        all_results: list = []

        for i, table in enumerate(CORE_SYNC_TABLES):
            if _is_stop_requested(job_id):
                append_log(db_id, "WARNING",
                           f"Stop requested before {table} — halting.")
                print(f"  [DB {db_id}] Stop requested before {table} — halting.")
                break

            _update_job(
                job_id,
                current_table=table,
                progress=int(i / len(CORE_SYNC_TABLES) * 90),
            )

            # Load checkpoint only when genuinely resuming a stopped/failed run.
            # For full sync or fresh incremental, ckpt=None forces _sync_table to
            # use the row-0 / 'since' cursor rather than an old checkpoint position.
            ckpt = get_checkpoint(db_id, table) if _is_resuming else None

            # Skip already-completed tables ONLY during crash-resume.
            # A fresh incremental must run every table even if old checkpoints say done.
            if _is_resuming and ckpt and ckpt.get("status") == "completed":
                now_ts = datetime.now(timezone.utc).isoformat()
                append_log(db_id, "INFO",
                           f"[{table}] Already completed in previous run — skipping",
                           table=table)
                all_results.append({
                    "table": table, "rows_synced": 0,
                    "rows_skipped": 0, "rows_error": 0,
                    "batch_count": 0, "status": "ok", "error": None,
                    "skipped_reason": "already_completed",
                })
                _update_table_live(
                    db_id, table,
                    status="skipped",
                    rows_synced=0, rows_skipped=0, rows_error=0,
                    batch_count=0,
                    started_at=now_ts,
                    finished_at=now_ts,
                )
                continue

            res = _sync_table(
                mysql_cfg=mysql_cfg,
                db_id=db_id,
                job_id=job_id,
                table=table,
                full=full,
                since=since,
                checkpoint=ckpt,
            )
            all_results.append(res)

            icon = {"ok": "✓", "stopped": "⏹", "error": "✗"}.get(res["status"], "?")
            print(f"  {icon} [DB {db_id}][{table}]: "
                  f"{res['rows_synced']} synced, {res.get('rows_error', 0)} errors "
                  f"— {res['status']}")

            if res["status"] in ("stopped", "error"):
                break

        was_stopped = (
            any(r["status"] == "stopped" for r in all_results)
            or _is_stop_requested(job_id)
        )
        had_error = any(r["status"] == "error" for r in all_results)

        if not was_stopped and not had_error:
            _update_job(job_id, current_table="product_metrics", progress=92)
            metrics = _sync_product_metrics(mysql_cfg, db_id, job_id)
            all_results.append(metrics)
            icon = "✓" if metrics["status"] == "ok" else "✗"
            print(f"  {icon} [DB {db_id}][product_metrics]: "
                  f"{metrics['rows_synced']} rows — {metrics['status']}")
            if metrics["status"] == "error":
                had_error = True

        was_stopped = (
            any(r["status"] == "stopped" for r in all_results)
            or _is_stop_requested(job_id)
        )
        had_error = any(r["status"] == "error" for r in all_results)

        now = datetime.now(timezone.utc).isoformat()
        elapsed = (datetime.now(timezone.utc) - sync_start).seconds

        if was_stopped:
            final = "stopped"
            _update_job(job_id, status="stopped", finished_at=now)
            update_sync_status(db_id, "stopped")
        elif had_error:
            final = "error"
            _update_job(job_id, status="failed", finished_at=now)
            update_sync_status(db_id, "error")
        else:
            final = "ok"
            _update_job(job_id, status="completed", progress=100, finished_at=now)
            update_sync_status(db_id, "ok", last_sync_at=now)

        _update_live_state(db_id, running=False, finished_at=now)

        total_synced  = sum(r.get("rows_synced", 0) for r in all_results)
        total_errors  = sum(r.get("rows_error", 0) for r in all_results)
        total_skipped = sum(r.get("rows_skipped", 0) for r in all_results)

        append_log(
            db_id,
            "INFO" if final == "ok" else "WARNING",
            f"Sync {final} | {total_synced} rows synced, "
            f"{total_skipped} skipped, {total_errors} errors | {elapsed}s total",
        )
        print(f"\n[DB {db_id}] Sync {final} — "
              f"{total_synced} synced, {total_errors} errors, {elapsed}s")

        if final == "ok":
            _trigger_index_rebuild(db_id)
            append_log(db_id, "INFO",
                       "Search indexes rebuilt (db + global) and cache cleared.")

        return {"status": final, "db_id": db_id, "tables": all_results}

    except Exception as exc:
        now = datetime.now(timezone.utc).isoformat()
        _update_job(job_id, status="failed", error_msg=str(exc), finished_at=now)
        update_sync_status(db_id, "error")
        _update_live_state(db_id, running=False, finished_at=now)
        append_log(db_id, "ERROR", f"Sync FAILED: {exc}")
        print(f"\n[DB {db_id}] Sync FAILED: {exc}")
        raise


def sync_database_background(
    db_id: int,
    full: Optional[bool] = None,
    callback=None,
) -> threading.Thread:
    """
    Run sync_database in a daemon thread and return immediately.
    Calls callback(result) on completion if provided.
    """
    def _run():
        try:
            result = sync_database(db_id, full=full)
            if callback:
                callback(result)
        except Exception as exc:
            print(f"[sync_manager] Background sync failed db_id={db_id}: {exc}")

    t = threading.Thread(target=_run, daemon=True, name=f"sync-db-{db_id}")
    t.start()
    return t


# ── Index rebuild trigger ──────────────────────────────────────────────────────

def _trigger_index_rebuild(db_id: int) -> None:
    """Rebuild in-memory search engines (isolated + global) and clear cache."""
    try:
        from modules.fuzzy_search import get_engine, rebuild_global_index
        from modules.cache import search_cache
        engine = get_engine(source_db_id=db_id)
        engine.rebuild()
        rebuild_global_index()
        search_cache.clear()
        print(
            f"[sync_manager] Search indexes rebuilt for db_id={db_id} and global; cache cleared."
        )
    except Exception as exc:
        print(f"[sync_manager] Index rebuild warning: {exc}")


# ── Status helpers ─────────────────────────────────────────────────────────────

def get_database_status(db_id: int) -> dict:
    """
    Return a combined status dict for one database:
      live        — in-memory real-time state
      last_job    — most recent sync_jobs row
      table_status — latest sync_log entry per table
      checkpoints — current sync_checkpoints rows
      error_count — number of unresolved sync_errors rows
    """
    live = get_live_state(db_id)

    conn = get_connection()
    try:
        last_job = conn.execute(
            "SELECT * FROM sync_jobs WHERE database_id = ? ORDER BY id DESC LIMIT 1",
            (db_id,),
        ).fetchone()

        log_rows = conn.execute(
            """
            SELECT s.*
            FROM sync_log s
            INNER JOIN (
                SELECT table_name, MAX(id) AS max_id
                FROM sync_log GROUP BY table_name
            ) latest ON s.table_name = latest.table_name AND s.id = latest.max_id
            ORDER BY s.last_synced DESC
            """,
        ).fetchall()
    finally:
        conn.close()

    return {
        "live":         live,
        "last_job":     dict(last_job) if last_job else None,
        "table_status": [dict(r) for r in log_rows],
        "checkpoints":  get_all_checkpoints(db_id),
        "error_count":  get_error_count(db_id),
    }


# ── Live log / error accessors (called by app.py endpoints) ───────────────────

def get_sync_logs(db_id: int, since_ts: Optional[str] = None) -> list:
    """Return buffered sync log entries for *db_id*."""
    return get_logs(db_id, since_ts=since_ts)


def get_sync_errors(db_id: int, limit: int = 100) -> list:
    """Return recent sync_errors rows for *db_id*."""
    return get_recent_errors(db_id, limit=limit)
