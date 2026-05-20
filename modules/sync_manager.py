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

Sync modes
==========
  full=True   Deletes this source's existing rows, then re-imports everything.
  full=False  Incremental: only rows whose updated_at > last_sync_at are fetched.
  full=None   Auto-detect: full if the database has never been synced, else incremental.

Batch / cursor strategy
=======================
  Uses WHERE id > last_id ORDER BY id LIMIT BATCH_SIZE (cursor-based pagination).
  A fresh MySQL connection is opened per batch to prevent idle-timeout errors.
  SQLite is committed after every batch so partial progress survives a crash.
  Checkpoint is updated after each commit so a resumed sync skips processed rows.

Stop mechanism
==============
  POST /api/database/<id>/stop  sets stop_requested = 1 in sync_jobs.
  The sync loop calls _is_stop_requested() after every batch.
  When stop is detected the current checkpoint is saved and the loop exits.
  The thread terminates naturally — no force-kill, no orphaned connections.

Index rebuild
=============
  The in-memory FuzzySearchEngine is rebuilt ONLY after a SUCCESSFUL full-sync
  completion.  Partial, failed, and incremental syncs do NOT trigger a rebuild
  to avoid exposing incomplete data to live search queries.

WAL mode
========
  Enabled in db/database.py's get_connection() — all connections inherit it.
  Reduces lock contention during concurrent sync + search + analytics writes.
"""

import sqlite3
import threading
import copy
import sys
import os
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SYNC_BATCH_SIZE
from db.database import get_connection

try:
    import pymysql
    PYMYSQL_AVAILABLE = True
except ImportError:
    PYMYSQL_AVAILABLE = False


# ── Tables synced from MySQL (transactions excluded — use product_metrics) ─────
CORE_SYNC_TABLES = ["brands", "categories", "product_group", "products"]

# ── Columns fetched from MySQL per table (must match SQLite schema exactly) ────
TABLE_COLUMNS = {
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
        "created_by", "created_at", "updated_at", "synced_at",
    ],
}


# ── Live state (in-memory, per database_id) ────────────────────────────────────
# Provides real-time progress for the polling frontend without a DB round-trip.
_state_lock = threading.Lock()
_live_states: dict = {}   # {db_id: {...}}


def _init_live_state(db_id: int, mode: str, job_id: int) -> None:
    with _state_lock:
        _live_states[db_id] = {
            "db_id":       db_id,
            "running":     True,
            "started_at":  datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "mode":        mode,
            "job_id":      job_id,
            "tables": {
                t: {"status": "pending", "rows": 0, "error": None,
                    "started_at": None, "finished_at": None}
                for t in CORE_SYNC_TABLES + ["product_metrics"]
            },
        }


def _update_live_state(db_id: int, **kwargs) -> None:
    with _state_lock:
        if db_id in _live_states:
            _live_states[db_id].update(kwargs)


def _update_table_live(db_id: int, table: str, **kwargs) -> None:
    with _state_lock:
        state = _live_states.get(db_id, {})
        tables = state.setdefault("tables", {})
        if table not in tables:
            tables[table] = {"status": "pending", "rows": 0, "error": None}
        tables[table].update(kwargs)


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


def _sanitize(value):
    """Convert MySQL-specific types to SQLite-safe equivalents."""
    import decimal
    import datetime as _dt
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# ── Sync job management ────────────────────────────────────────────────────────

def _create_job(db_id: int) -> int:
    """Insert a pending sync_jobs row. Returns the new job id."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO sync_jobs (database_id, status, created_at)
            VALUES (?, 'pending', ?)
            """,
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
    """
    Check whether the API has requested a graceful stop for this job.
    Called after every batch — no force-kill is ever needed.
    """
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
            """
            SELECT * FROM sync_jobs
            WHERE database_id = ? AND status = 'running'
            ORDER BY id DESC LIMIT 1
            """,
            (db_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def request_stop(db_id: int) -> dict:
    """
    Request a graceful stop for the running sync on this database.
    Sets stop_requested = 1 — the sync loop will exit after its current batch.
    Returns {"ok": bool, "message": str}.
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


# ── Checkpoint management ──────────────────────────────────────────────────────

def _get_checkpoint(db_id: int, table: str) -> Optional[dict]:
    """Load the checkpoint for (db_id, table). Returns None when absent."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM sync_checkpoints
            WHERE database_id = ? AND table_name = ?
            """,
            (db_id, table),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_checkpoint(
    db_id: int,
    table: str,
    last_id: int,
    last_updated_at: Optional[str] = None,
) -> None:
    """
    Upsert a checkpoint for (db_id, table).
    Called after every committed batch so a crash or stop can resume cleanly.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sync_checkpoints
                (database_id, table_name, last_processed_id,
                 last_processed_updated_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(database_id, table_name) DO UPDATE SET
                last_processed_id         = excluded.last_processed_id,
                last_processed_updated_at = excluded.last_processed_updated_at,
                updated_at                = excluded.updated_at
            """,
            (db_id, table, last_id, last_updated_at,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_checkpoints(db_id: int) -> None:
    """Remove all checkpoints for a database. Called at the start of a full sync."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM sync_checkpoints WHERE database_id = ?", (db_id,)
        )
        conn.commit()
    finally:
        conn.close()


# ── SQLite UPSERT helper ───────────────────────────────────────────────────────

def _upsert_batch(
    sqlite_conn: sqlite3.Connection,
    table: str,
    rows: list,
    source_db_id: int,
) -> int:
    """
    Insert-or-update a batch of rows into a synced table.

    Includes source_db_id so every row is traceable to its ERP source.
    Conflict is resolved on the primary key (id) — all other columns including
    source_db_id are updated.  This means syncing from a second ERP will
    overwrite a row with the same MySQL id from the first ERP; operators are
    responsible for ensuring non-overlapping id spaces across ERPs.

    The synced_at column (products only) is set to the current UTC time
    on every upsert so we know exactly when the local cache was last refreshed.
    """
    if not rows:
        return 0

    cols = list(TABLE_COLUMNS[table])
    now  = datetime.now(timezone.utc).isoformat()

    # Replace synced_at with wall-clock time instead of the MySQL value
    if "synced_at" in cols:
        synced_idx = cols.index("synced_at")

    all_cols     = cols + ["source_db_id"]
    placeholders = ", ".join("?" * len(all_cols))
    col_names    = ", ".join(all_cols)

    update_cols  = [c for c in all_cols if c != "id"]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)

    sql = (
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
    )

    data = []
    for row in rows:
        values = list(_sanitize(row.get(c)) for c in cols)
        if "synced_at" in cols:
            values[synced_idx] = now
        values.append(source_db_id)
        data.append(tuple(values))

    sqlite_conn.executemany(sql, data)
    return len(data)


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
        """
        INSERT INTO sync_log
            (table_name, last_synced, records_synced, status, error_msg)
        VALUES (?, ?, ?, ?, ?)
        """,
        (table, datetime.now(timezone.utc).isoformat(), count, status, error),
    )


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
    Sync one MySQL table into SQLite using cursor-based pagination.

    Cursor strategy:
      WHERE id > last_id ORDER BY id LIMIT BATCH_SIZE
      → O(batch_size) per fetch regardless of table position, no LIMIT/OFFSET skew.
      → Fresh MySQL connection per batch prevents idle-timeout (WinError 10054).

    Incremental filter (full=False):
      WHERE updated_at >= since AND id > last_id
      → Only rows changed since the last successful sync are transferred.

    Checkpoint:
      Saved after every committed batch.  If the job is stopped mid-table
      the next run resumes from last_processed_id rather than row 0.

    Returns a result dict consumed by sync_database().
    """
    result = {
        "table":       table,
        "rows_synced": 0,
        "status":      "ok",
        "error":       None,
    }
    sqlite_conn = None

    _update_table_live(db_id, table, status="running",
                       started_at=datetime.now(timezone.utc).isoformat())

    try:
        sqlite_conn = get_connection()
        sqlite_conn.execute("PRAGMA foreign_keys = OFF")

        cols     = TABLE_COLUMNS[table]
        col_list = ", ".join(f"`{c}`" for c in cols)

        # ── Full sync: wipe this source's rows before reimporting ─────────────
        # We only delete rows belonging to this source_db_id so data from
        # other connected databases is never affected.
        if full:
            sqlite_conn.execute(
                f"DELETE FROM {table} WHERE source_db_id = ?", (db_id,)
            )
            sqlite_conn.commit()
            last_id         = 0
            last_updated_at = None
        else:
            # Incremental: resume from checkpoint if one exists, else use
            # last_sync_at from the connected_databases row as the floor.
            if checkpoint:
                last_id         = checkpoint["last_processed_id"]
                last_updated_at = checkpoint["last_processed_updated_at"]
            else:
                last_id         = 0
                last_updated_at = since

        total = 0

        while True:
            # ── Stop check (before each batch) ────────────────────────────────
            # Never force-kills the thread — just exits the loop cleanly.
            if _is_stop_requested(job_id):
                _save_checkpoint(db_id, table, last_id, last_updated_at)
                result["status"] = "stopped"
                _update_table_live(
                    db_id, table, status="stopped", rows=total,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                return result

            # ── Build MySQL query ──────────────────────────────────────────────
            # Incremental filter on updated_at only when we have a reference time
            # AND we are not doing a full sync.
            if since and not full:
                query = (
                    f"SELECT {col_list} FROM `{table}` "
                    f"WHERE updated_at >= %s AND id > %s "
                    f"ORDER BY id LIMIT %s"
                )
                params = (since, last_id, SYNC_BATCH_SIZE)
            else:
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
                break   # no more data for this table

            # ── Upsert into SQLite ─────────────────────────────────────────────
            count = _upsert_batch(sqlite_conn, table, rows, source_db_id=db_id)
            total += count

            # Advance cursor to the highest id in this batch
            last_id         = rows[-1]["id"]
            last_updated_at = rows[-1].get("updated_at")

            # ── Commit batch + save checkpoint ─────────────────────────────────
            # Committing here means a crash loses at most one batch of work.
            sqlite_conn.commit()
            _save_checkpoint(db_id, table, last_id, last_updated_at)

            _update_table_live(db_id, table, rows=total)
            _update_job(job_id, current_table=table)

            print(f"  [DB {db_id}][{table}] batch committed — {total} rows "
                  f"(cursor id={last_id})")

        # ── Table complete ─────────────────────────────────────────────────────
        _log_sync(sqlite_conn, table, total, "ok")
        sqlite_conn.commit()

        result["rows_synced"] = total
        _update_table_live(
            db_id, table, status="ok", rows=total,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    except Exception as exc:
        result["status"] = "error"
        result["error"]  = str(exc)
        _update_table_live(
            db_id, table, status="error", error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        if sqlite_conn:
            try:
                _log_sync(sqlite_conn, table, result["rows_synced"], "error", str(exc))
                sqlite_conn.commit()
            except Exception:
                pass
        print(f"  [DB {db_id}][ERROR] {table}: {exc}")

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

    This replaces the expensive full sync of transactions +
    transaction_sell_lines (which can be millions of rows).
    Instead we fetch only the aggregated summary per product.

    Metrics stored:
      sales_count      — number of finalised sell transactions containing this product
      popularity_score — normalised total quantity sold (0.0–1.0 within this source)
      last_sold_at     — date of the most recent completed sale

    The aggregate query runs on the MySQL side (uses its indexes).
    Results are upserted into product_metrics with ON CONFLICT on
    (product_id, source_db_id) so multiple ERP sources never collide.
    """
    result = {
        "table":       "product_metrics",
        "rows_synced": 0,
        "status":      "ok",
        "error":       None,
    }

    _update_table_live(db_id, "product_metrics", status="running",
                       started_at=datetime.now(timezone.utc).isoformat())

    try:
        if _is_stop_requested(job_id):
            result["status"] = "stopped"
            _update_table_live(db_id, "product_metrics", status="stopped",
                               finished_at=datetime.now(timezone.utc).isoformat())
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

        if not rows:
            _update_table_live(db_id, "product_metrics", status="ok", rows=0,
                               finished_at=datetime.now(timezone.utc).isoformat())
            return result

        # Normalise popularity_score to 0.0–1.0 within this source
        max_qty = max(float(r.get("total_qty") or 0) for r in rows) or 1.0
        now     = datetime.now(timezone.utc).isoformat()

        data = [
            (
                int(r["product_id"]),
                db_id,
                int(r.get("sales_count") or 0),
                round(float(r.get("total_qty") or 0) / max_qty, 4),
                _sanitize(r.get("last_sold_at")),
                now,
            )
            for r in rows
        ]

        sqlite_conn = get_connection()
        try:
            sqlite_conn.executemany(
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
                data,
            )
            sqlite_conn.commit()
        finally:
            sqlite_conn.close()

        result["rows_synced"] = len(data)
        _update_table_live(
            db_id, "product_metrics", status="ok", rows=len(data),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    except Exception as exc:
        result["status"] = "error"
        result["error"]  = str(exc)
        _update_table_live(
            db_id, "product_metrics", status="error", error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        print(f"  [DB {db_id}][ERROR] product_metrics: {exc}")

    return result


# ── Main entry points ──────────────────────────────────────────────────────────

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

    # Auto-detect: first-ever sync must be a full sync
    if full is None:
        full = (db["last_sync_at"] is None)

    mode  = "full" if full else "incremental"
    since = None if full else db["last_sync_at"]

    job_id = _create_job(db_id)
    _init_live_state(db_id, mode, job_id)
    update_sync_status(db_id, "running")
    _update_job(
        job_id,
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    print(f"\n{'='*55}")
    print(f"[DB {db_id}] {mode.upper()} sync starting — {datetime.now()}")
    print(f"  since={since or 'beginning'}  job_id={job_id}")
    print(f"{'='*55}")

    try:
        if full:
            _clear_checkpoints(db_id)   # fresh start — discard old resume points

        mysql_cfg  = get_mysql_config(db_id)
        all_results: list = []

        for i, table in enumerate(CORE_SYNC_TABLES):
            if _is_stop_requested(job_id):
                print(f"  [DB {db_id}] Stop requested before {table} — halting.")
                break

            # Progress: 0–90% spread across core tables; 90–100% for metrics
            _update_job(
                job_id,
                current_table=table,
                progress=int(i / len(CORE_SYNC_TABLES) * 90),
            )

            checkpoint = None if full else _get_checkpoint(db_id, table)

            res = _sync_table(
                mysql_cfg=mysql_cfg,
                db_id=db_id,
                job_id=job_id,
                table=table,
                full=full,
                since=since,
                checkpoint=checkpoint,
            )
            all_results.append(res)

            icon = {"ok": "✓", "stopped": "⏹", "error": "✗"}.get(res["status"], "?")
            print(f"  {icon} [DB {db_id}][{table}]: "
                  f"{res['rows_synced']} rows — {res['status']}")

            if res["status"] in ("stopped", "error"):
                break   # propagate stop/error without syncing remaining tables

        # ── Evaluate outcome so far ────────────────────────────────────────────
        was_stopped = (
            any(r["status"] == "stopped" for r in all_results)
            or _is_stop_requested(job_id)
        )
        had_error = any(r["status"] == "error" for r in all_results)

        # ── Product metrics — only when core tables all completed cleanly ──────
        if not was_stopped and not had_error:
            _update_job(job_id, current_table="product_metrics", progress=92)
            metrics = _sync_product_metrics(mysql_cfg, db_id, job_id)
            all_results.append(metrics)
            icon = "✓" if metrics["status"] == "ok" else "✗"
            print(f"  {icon} [DB {db_id}][product_metrics]: "
                  f"{metrics['rows_synced']} rows — {metrics['status']}")
            if metrics["status"] == "error":
                had_error = True

        # ── Recompute final verdict after metrics ──────────────────────────────
        was_stopped = (
            any(r["status"] == "stopped" for r in all_results)
            or _is_stop_requested(job_id)
        )
        had_error = any(r["status"] == "error" for r in all_results)

        now = datetime.now(timezone.utc).isoformat()

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

        print(f"\n[DB {db_id}] Sync {final}.  {'='*38}")

        # ── Rebuild search index after a SUCCESSFUL full sync only ─────────────
        # Never rebuild on partial, stopped, or incremental syncs to avoid
        # serving incomplete data to live search queries.
        if final == "ok" and full:
            _trigger_index_rebuild()

        return {"status": final, "db_id": db_id, "tables": all_results}

    except Exception as exc:
        now = datetime.now(timezone.utc).isoformat()
        _update_job(job_id, status="failed", error_msg=str(exc), finished_at=now)
        update_sync_status(db_id, "error")
        _update_live_state(db_id, running=False, finished_at=now)
        print(f"\n[DB {db_id}] Sync FAILED: {exc}")
        raise


def sync_database_background(
    db_id: int,
    full: bool = None,
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

def _trigger_index_rebuild() -> None:
    """
    Rebuild the in-memory FuzzySearchEngine and clear the search cache
    after a successful full sync.

    Imported lazily to avoid circular imports — fuzzy_search imports config
    which does not depend on sync_manager.
    """
    try:
        from modules.fuzzy_search import get_engine
        from modules.cache import search_cache
        engine = get_engine()
        engine.rebuild()
        search_cache.clear()
        print("[sync_manager] Search index rebuilt and cache cleared after full sync.")
    except Exception as exc:
        print(f"[sync_manager] Index rebuild warning: {exc}")


# ── Status helpers ─────────────────────────────────────────────────────────────

def get_database_status(db_id: int) -> dict:
    """
    Return a combined status dict for one database:
      live   — in-memory real-time state
      job    — most recent sync_jobs row
      tables — latest sync_log entry per table
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
                SELECT table_name, MAX(id) AS max_id FROM sync_log GROUP BY table_name
            ) latest ON s.table_name = latest.table_name AND s.id = latest.max_id
            ORDER BY s.last_synced DESC
            """,
        ).fetchall()

        return {
            "live":        live,
            "last_job":    dict(last_job) if last_job else None,
            "table_status": [dict(r) for r in log_rows],
        }
    finally:
        conn.close()
