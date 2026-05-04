"""
modules/sync.py
---------------
Module 1 — MySQL → SQLite sync.

Key design decisions:
  - Cursor-based pagination (WHERE id > last_id) instead of LIMIT/OFFSET.
    This avoids MySQL connection timeouts on large tables (488k+ rows)
    and is significantly faster because MySQL doesn't scan skipped rows.
  - Fresh MySQL connection opened per batch for large tables so a long-running
    sync never hits the server's wait_timeout.
  - _sanitize() converts decimal.Decimal / datetime / bytes before SQLite insert.
  - Commits every batch so partial progress is preserved on crash.
  - Live SYNC_STATE updated per batch so the frontend can poll progress.
"""

import sqlite3
import sys
import os
import threading
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import MYSQL_CONFIG, SYNC_TABLES, SYNC_BATCH_SIZE
from db.database import get_connection

try:
    import pymysql
    PYMYSQL_AVAILABLE = True
except ImportError:
    PYMYSQL_AVAILABLE = False


# ── Live sync state ────────────────────────────────────────────────────────────
_state_lock = threading.Lock()

SYNC_STATE: dict = {
    "running":     False,
    "started_at":  None,
    "finished_at": None,
    "mode":        None,
    "tables":      {},
}


def _init_state(mode: str):
    with _state_lock:
        SYNC_STATE["running"]     = True
        SYNC_STATE["started_at"]  = datetime.now(timezone.utc).isoformat()
        SYNC_STATE["finished_at"] = None
        SYNC_STATE["mode"]        = mode
        SYNC_STATE["tables"]      = {
            t: {"status": "pending", "rows": 0, "error": None,
                "started_at": None, "finished_at": None}
            for t in SYNC_TABLES
        }


def _update_table_state(table: str, **kwargs):
    with _state_lock:
        if table in SYNC_STATE["tables"]:
            SYNC_STATE["tables"][table].update(kwargs)


def _finish_state():
    with _state_lock:
        SYNC_STATE["running"]     = False
        SYNC_STATE["finished_at"] = datetime.now(timezone.utc).isoformat()


def get_live_state() -> dict:
    """Return a copy of the current sync state (thread-safe)."""
    with _state_lock:
        import copy
        return copy.deepcopy(SYNC_STATE)


# ── Column definitions ─────────────────────────────────────────────────────────
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
    "transactions": [
        "id", "business_id", "location_id", "type", "sub_type",
        "status", "payment_status", "contact_id",
        "invoice_no", "ref_no", "transaction_date",
        "total_before_tax", "tax_amount", "discount_type",
        "discount_amount", "shipping_charges", "final_total",
        "sub_total", "item_qty", "total_qty",
        "additional_notes", "staff_note",
        "is_direct_sale", "is_suspend",
        "delivery_method", "delivery_date",
        "created_by", "created_at", "updated_at",
    ],
    "transaction_sell_lines": [
        "id", "transaction_id", "product_id", "variation_id",
        "quantity", "quantity_returned",
        "unit_price_before_discount", "unit_price",
        "line_discount_type", "line_discount_amount",
        "unit_price_inc_tax", "item_tax", "tax_id",
        "sell_line_note", "purchase_price",
        "out_of_stock", "is_picked", "is_packed",
        "created_at", "updated_at",
    ],
}


# ── MySQL connection ───────────────────────────────────────────────────────────

def _get_mysql_conn():
    """Open and return a fresh PyMySQL connection using current saved settings."""
    if not PYMYSQL_AVAILABLE:
        raise RuntimeError("pymysql is not installed. Run: pip install pymysql")
    try:
        from modules.settings_manager import get_mysql_config
        cfg = get_mysql_config()
    except Exception:
        cfg = MYSQL_CONFIG
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        # Keep connection alive during long syncs
        read_timeout=300,
        write_timeout=300,
    )


# ── Type sanitizer ─────────────────────────────────────────────────────────────

def _sanitize(value):
    """
    Convert MySQL types that SQLite cannot bind:
      decimal.Decimal → float
      datetime / date → ISO string
      bytes           → utf-8 string
    """
    import decimal
    import datetime as dt
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# ── SQLite upsert ──────────────────────────────────────────────────────────────

def _upsert_rows(sqlite_conn: sqlite3.Connection, table: str, rows: list) -> int:
    if not rows:
        return 0
    cols         = TABLE_COLUMNS[table]
    placeholders = ", ".join(["?"] * len(cols))
    col_names    = ", ".join(cols)
    sql  = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
    data = [tuple(_sanitize(row.get(c)) for c in cols) for row in rows]
    sqlite_conn.executemany(sql, data)
    return len(data)


# ── Sync log ───────────────────────────────────────────────────────────────────

def _log_sync(sqlite_conn: sqlite3.Connection, table: str,
              count: int, status: str, error: Optional[str] = None):
    """Always INSERT a new row — never UPDATE existing rows."""
    sqlite_conn.execute(
        """INSERT INTO sync_log
               (table_name, last_synced, records_synced, status, error_msg)
           VALUES (?, ?, ?, ?, ?)""",
        (table, datetime.now(timezone.utc).isoformat(), count, status, error),
    )


# ── Core sync function ─────────────────────────────────────────────────────────

def sync_table(table: str, full: bool = True, since: Optional[str] = None) -> dict:
    """
    Sync one table from MySQL → SQLite using cursor-based pagination.

    Cursor-based pagination (WHERE id > last_seen_id ORDER BY id)
    instead of LIMIT/OFFSET solves two problems:
      1. MySQL drops long-idle connections (WinError 10054) — we open a
         fresh connection every batch so the connection is never idle long.
      2. OFFSET N forces MySQL to scan N rows before returning results —
         cursor pagination is O(batch_size) regardless of position.
    """
    result = {"table": table, "rows_synced": 0, "status": "ok", "error": None}

    sqlite_conn = None
    _update_table_state(table, status="running",
                        started_at=datetime.now(timezone.utc).isoformat())

    try:
        sqlite_conn = get_connection()
        sqlite_conn.execute("PRAGMA foreign_keys = OFF")

        cols     = TABLE_COLUMNS[table]
        col_list = ", ".join(f"`{c}`" for c in cols)

        if full:
            sqlite_conn.execute(f"DELETE FROM {table}")
            sqlite_conn.commit()

        # ── Cursor-based pagination ────────────────────────────────────────────
        # Each batch opens its own MySQL connection → no idle-timeout issues.
        last_id = 0
        total   = 0

        while True:
            # Build query — cursor pagination or delta
            if since:
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

            # Fresh connection per batch — prevents WinError 10054
            mysql_conn = _get_mysql_conn()
            try:
                with mysql_conn.cursor() as cursor:
                    cursor.execute(query, params)
                    rows = cursor.fetchall()
            finally:
                mysql_conn.close()

            if not rows:
                break

            count   = _upsert_rows(sqlite_conn, table, rows)
            total  += count
            last_id = rows[-1]["id"]   # advance cursor to last seen id

            # Commit every batch — partial progress is preserved on crash
            sqlite_conn.commit()

            _update_table_state(table, rows=total)
            print(f"  [{table}] synced {total} rows (last id={last_id})…")

        _log_sync(sqlite_conn, table, total, "ok")
        sqlite_conn.commit()
        result["rows_synced"] = total
        _update_table_state(table, status="ok", rows=total,
                            finished_at=datetime.now(timezone.utc).isoformat())

    except Exception as exc:
        result["status"] = "error"
        result["error"]  = str(exc)
        _update_table_state(table, status="error", error=str(exc),
                            finished_at=datetime.now(timezone.utc).isoformat())
        try:
            _log_sync(sqlite_conn, table, result["rows_synced"], "error", str(exc))
            sqlite_conn.commit()
        except Exception:
            pass
        print(f"  [ERROR] {table}: {exc}")

    finally:
        try:
            if sqlite_conn:
                sqlite_conn.execute("PRAGMA foreign_keys = ON")
                sqlite_conn.close()
        except Exception:
            pass

    return result


# ── Sync all tables ────────────────────────────────────────────────────────────

def sync_all(full: bool = True) -> list:
    _init_state("full" if full else "delta")
    print(f"\n{'='*50}")
    print(f"Starting {'FULL' if full else 'DELTA'} sync — {datetime.now()}")
    print(f"{'='*50}")

    results = []
    for table in SYNC_TABLES:
        print(f"\n→ Syncing table: {table}")
        res = sync_table(table, full=full)
        results.append(res)
        icon = "✓" if res["status"] == "ok" else "✗"
        print(f"  {icon} {table}: {res['rows_synced']} rows — {res['status']}")

    _finish_state()
    print(f"\n{'='*50}\nSync complete.")
    return results


def sync_all_background(full: bool = True, callback=None):
    """Run sync_all in a background thread. Returns Thread immediately."""
    def _run():
        results = sync_all(full=full)
        if callback:
            try:
                callback(results)
            except Exception as e:
                print(f"[sync callback error] {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ── Status queries ─────────────────────────────────────────────────────────────

def get_sync_status() -> list:
    """Latest sync log entry per table, sorted newest first. Always fresh."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT s.*
            FROM sync_log s
            INNER JOIN (
                SELECT table_name, MAX(id) AS max_id
                FROM sync_log
                GROUP BY table_name
            ) latest ON s.table_name = latest.table_name
                     AND s.id        = latest.max_id
            ORDER BY s.last_synced DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_sync_history(limit: int = 50) -> list:
    """Last `limit` sync log entries across all tables, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
