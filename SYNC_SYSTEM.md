# Multi-Database Sync System — Complete Technical Reference

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [UI Buttons & What They Do](#2-ui-buttons--what-they-do)
3. [Sync Modes](#3-sync-modes)
4. [Step-by-Step Sync Flow](#4-step-by-step-sync-flow)
5. [Enterprise Incremental Cursor](#5-enterprise-incremental-cursor)
6. [Row-Level Error Isolation](#6-row-level-error-isolation)
7. [Crash-Resume via Checkpoints](#7-crash-resume-via-checkpoints)
8. [Graceful Stop Mechanism](#8-graceful-stop-mechanism)
9. [Live Log Streaming](#9-live-log-streaming)
10. [Product Metrics Aggregation](#10-product-metrics-aggregation)
11. [Index Rebuild Rules](#11-index-rebuild-rules)
12. [SQLite Tables Reference](#12-sqlite-tables-reference)
13. [API Endpoints Reference](#13-api-endpoints-reference)
14. [Module File Map](#14-module-file-map)
15. [Data Flow Diagram](#15-data-flow-diagram)

---

## 1. Architecture Overview

The system syncs data from one or more **MySQL/ERP source databases** into a local **SQLite cache**. The SQLite cache powers fuzzy search, autocomplete, and analytics — all reads hit SQLite, never MySQL directly.

```
MySQL (ERP)  ──→  sync_manager.py  ──→  SQLite (local cache)
                      │
                      ├── sync_normalization.py   (NULL-safe row cleaning)
                      ├── sync_errors.py          (row failure log)
                      ├── sync_live_logs.py       (in-memory log stream)
                      └── sync_checkpoints.py     (crash-resume state)
```

**Key design decisions:**

| Decision | Why |
|---|---|
| SQLite WAL mode | Concurrent reads during sync without blocking search |
| Batch size 1000–2000 rows | Balances memory and throughput |
| Per-row upsert with try/except | One bad row never aborts the entire batch |
| Fresh MySQL connection per batch | Prevents WinError 10054 idle-timeout on long syncs |
| `source_db_id` on all synced tables | Multiple ERP databases can coexist in the same SQLite file |
| `ON CONFLICT(id) DO UPDATE` | Idempotent: re-running sync never creates duplicates |

---

## 2. UI Buttons & What They Do

The sync UI lives at `/sync`. All buttons operate on the **selected database** from the dropdown at the top.

### Database Selector (dropdown)

```
Sync target: [ My ERP — 192.168.1.10/erp_db  ▼ ]   🟢 Last: May 21, 14:32
```

- Populated by `GET /api/databases`
- Shows all connected ERP databases
- Status pill shows: 🔘 Never / 🔵 Running / 🟢 OK / 🔴 Error / 🟡 Stopped
- Switching databases resets the log cursor (`_lastLogTs = null`) so the log panel starts fresh

---

### Button 1 — Run Full Sync (green)

**What it does:**
1. Calls `POST /api/database/<id>/sync` with body `{"full": true}`
2. Deletes ALL existing rows for this `source_db_id` from: `brands`, `categories`, `product_group`, `products`
3. Reimports every row from MySQL, starting from id=0
4. After a **successful** full sync: rebuilds the in-memory fuzzy search index and clears the search cache

**When to use:** First time connecting a new ERP, or after a major schema/data change in MySQL.

**Button state rules:**
- Disabled while any sync is running
- Re-enabled immediately after sync finishes/stops/errors

---

### Button 2 — Run Incremental (yellow)

**What it does:**
1. Calls `POST /api/database/<id>/sync` with body `{"full": false}`
2. Only fetches rows where `updated_at > last_sync_at` from MySQL
3. Uses a **dual-key cursor** to guarantee no gaps (see [Section 5](#5-enterprise-incremental-cursor))
4. Skips tables that were already marked `completed` in a previous partial run
5. Does **not** rebuild the search index (only full sync does that)

**When to use:** Daily/hourly refresh. Much faster than full sync. Safe to run any time.

---

### Button 3 — Stop Sync (red)

**What it does:**
1. Calls `POST /api/database/<id>/stop`
2. Sets `stop_requested = 1` in the `sync_jobs` table
3. The sync thread checks this flag **before every batch**
4. When detected: saves the current checkpoint and exits the loop cleanly
5. No force-kill, no orphaned connections, no data corruption

**What happens next:** The checkpoint records exactly where the sync stopped. The next incremental sync **resumes from that row** automatically, skipping already-completed tables.

**Button state rules:**
- Only enabled when a sync is running
- Disabled at all other times

---

### Button 4 — Rebuild Search Index (blue)

**What it does:**
1. Calls `POST /api/search/rebuild`
2. Drops the in-memory `FuzzySearchEngine` and rebuilds it from SQLite
3. Clears the search cache
4. Shows before/after product count and cache entries cleared

**When to use:**
- After any manual data edit in SQLite
- After an incremental sync (which does not auto-rebuild)
- If search results feel stale

**This is separate from sync.** Sync writes to SQLite. Rebuild loads SQLite into memory.

---

### Log Console — Clear button

Clears the visual display only. Does **not** affect server-side log buffer. Sets `_lastLogTs = null` so next poll fetches all buffered entries again.

### Error Panel — Refresh button

Manually re-fetches `GET /api/database/<id>/errors`. The panel auto-appears after any sync that produced row errors.

### Sync History — Refresh button

Manually re-fetches `GET /api/sync/history`. Auto-refreshes after each sync completes.

---

## 3. Sync Modes

| Mode | Trigger | What gets fetched | Clears existing rows? | Rebuilds index? |
|---|---|---|---|---|
| **Full** | `full=true` | All rows, id > 0 | Yes — this source's rows only | Yes, on success |
| **Incremental** | `full=false` | Rows with `updated_at > last_sync_at` | No | No |
| **Auto-detect** | `full=null` (default) | Full on first ever sync, incremental thereafter | Only on first run | Only on first run |

Auto-detect logic (in `sync_database()`):
```python
if full is None:
    full = (db["last_sync_at"] is None)  # True = never synced before
```

---

## 4. Step-by-Step Sync Flow

When you click **Run Full Sync** or **Run Incremental**, this is exactly what happens:

```
1. POST /api/database/<id>/sync  received by Flask
   └── Checks: database exists? sync already running?
   └── Calls sync_database_background(db_id, full=...)
       └── Spawns daemon thread: Thread(target=_run, daemon=True)
       └── Returns HTTP 200 immediately — browser doesn't wait

2. Background thread calls sync_database(db_id, full)
   ├── Resolve mode (full / incremental / auto-detect)
   ├── Create sync_jobs row  → status='pending'
   ├── Clear in-memory log buffer for this db_id
   ├── Init live state dict (_live_states[db_id])
   ├── Update connected_databases.sync_status = 'running'
   ├── Update sync_jobs → status='running', started_at=now
   │
   ├── If full=True: DELETE FROM <table> WHERE source_db_id = ?
   ├── If full=True: clear_checkpoints(db_id)
   │
   ├── For each table in [brands, categories, product_group, products]:
   │   ├── Check stop_requested → exit if set
   │   ├── Check checkpoint status → skip if already 'completed' (incremental only)
   │   └── Call _sync_table(...)
   │       ├── save_checkpoint(..., status='running')
   │       ├── LOOP:
   │       │   ├── Check stop_requested → save checkpoint 'stopped', return
   │       │   ├── Build MySQL query (full cursor or dual-key cursor)
   │       │   ├── Open fresh MySQL connection
   │       │   ├── Fetch batch (up to SYNC_BATCH_SIZE rows)
   │       │   ├── Close MySQL connection
   │       │   ├── If no rows → break (table done)
   │       │   ├── _upsert_batch_isolated() → per-row normalize + upsert
   │       │   ├── sqlite_conn.commit()
   │       │   ├── save_checkpoint(last_id, last_updated_at, 'running')
   │       │   └── Update live state counters
   │       └── save_checkpoint(..., status='completed')
   │
   ├── If no stop/error: call _sync_product_metrics(...)
   │   └── One aggregate GROUP BY query → upsert product_metrics
   │
   ├── Finalize:
   │   ├── stopped → sync_jobs.status='stopped', connected_databases.sync_status='stopped'
   │   ├── error   → sync_jobs.status='failed',  connected_databases.sync_status='error'
   │   └── ok      → sync_jobs.status='completed', last_sync_at=now
   │
   └── If ok AND full: _trigger_index_rebuild()
       ├── engine.rebuild()
       └── search_cache.clear()
```

---

## 5. Enterprise Incremental Cursor

### The Problem with a Simple Cursor

A naive cursor like `WHERE id > last_id ORDER BY id` has a fatal gap when rows share the same `updated_at` value at a batch boundary:

```
Batch 1 ends at: id=1000, updated_at='2026-05-21 10:00:00'
                                          ↑
Rows 1001–1005 also have updated_at='2026-05-21 10:00:00'

Next batch: WHERE id > 1000  →  gets rows 1001–1005 ✓
But if you use: WHERE updated_at > '10:00:00'  →  SKIPS rows 1001–1005 ✗
```

### The Dual-Key Cursor (used in this system)

For **incremental sync** with a known `last_sync_at`:

```sql
SELECT {cols} FROM `{table}`
WHERE (updated_at > %s OR (updated_at = %s AND id > %s))
ORDER BY updated_at, id
LIMIT {SYNC_BATCH_SIZE}
```

Parameters: `(last_updated_at, last_updated_at, last_id, batch_size)`

After each batch, the cursor advances:
```python
last_row        = rows[-1]
last_id         = last_row["id"]
last_updated_at = last_row.get("updated_at")
```

This guarantees:
- Every row is fetched exactly once
- No gaps even when many rows share the same `updated_at`
- Safe to resume after crash: checkpoint stores `(last_id, last_updated_at)`

For **full sync** (no timestamp filter):
```sql
SELECT {cols} FROM `{table}`
WHERE id > %s
ORDER BY id
LIMIT {SYNC_BATCH_SIZE}
```

---

## 6. Row-Level Error Isolation

### Old approach (broken)

```python
cursor.executemany(INSERT_SQL, rows)  # One NULL violation → entire batch rolls back
```

### New approach (this system)

```python
def _upsert_batch_isolated(db_id, sqlite_conn, table, raw_rows, source_db_id):
    synced = skipped = errors = 0
    now = datetime.now(timezone.utc).isoformat()

    for raw_row in raw_rows:
        source_id = raw_row.get("id")
        try:
            clean = normalize_row(table, raw_row)   # Step 1: NULL-safe cleaning
            if clean is None:
                skipped += 1
                log_row_error(...)                  # Log & skip
                continue

            _upsert_row(sqlite_conn, table, clean, source_db_id, now)  # Step 2: upsert
            synced += 1

        except Exception as exc:
            errors  += 1
            skipped += 1
            log_row_error(db_id, table, source_id, exc, raw_row)       # Log & continue

    return synced, skipped, errors
```

**Effect:** If row #5 in a batch of 1000 has a NULL in a NOT NULL column, rows 1–4 and 6–1000 still sync. Row #5 is logged to `sync_errors` for debugging.

### NULL normalization (sync_normalization.py)

The ERP sends `NULL` for columns declared `NOT NULL DEFAULT ''` in SQLite. The normalizer intercepts this:

```python
# Products — NOT NULL columns with safe defaults
"sku":           clean_nullable_string(row.get("sku"),  ""),
"sku2":          clean_nullable_string(row.get("sku2"), ""),   # ← was causing crashes
"sku3":          clean_nullable_string(row.get("sku3"), ""),
"name":          clean_nullable_string(row.get("name"), "Unknown Product"),
"enable_stock":  clean_nullable_bool(row.get("enable_stock"), 0),
"is_inactive":   clean_nullable_bool(row.get("is_inactive"),  0),
"ml":            clean_nullable_number(row.get("ml"), 0.0),
"created_by":    clean_nullable_int(row.get("created_by"), 0),
```

The cleaners also handle:
- `bytes` → decode UTF-8
- `Decimal` → `float`
- `datetime` → ISO-8601 string
- Zero datetime (`0000-00-00`) → `None`

---

## 7. Crash-Resume via Checkpoints

### What is saved

After **every committed batch**, the system saves:

```sql
INSERT INTO sync_checkpoints
    (database_id, table_name, last_processed_id, last_processed_updated_at, status, updated_at)
VALUES (?, ?, ?, ?, 'running', CURRENT_TIMESTAMP)
ON CONFLICT(database_id, table_name) DO UPDATE SET ...
```

### Status values

| Status | Meaning |
|---|---|
| `running` | Table is mid-sync (set before the loop starts) |
| `completed` | Table finished successfully |
| `stopped` | Sync was gracefully stopped mid-table |
| `failed` | Table hit a fatal exception |

### Resume logic (incremental only)

When an incremental sync starts, for each table:

```python
ckpt = get_checkpoint(db_id, table)

if ckpt and ckpt["status"] == "completed":
    # Already finished in a previous run — skip entirely
    append_log(..., f"[{table}] Already completed — skipping")
    continue

# Otherwise, resume from where we left off:
last_id         = ckpt["last_processed_id"]        # Row cursor
last_updated_at = ckpt["last_processed_updated_at"] # Timestamp cursor
```

**Scenario: Sync crashes mid-products after brands/categories/product_group finish**

Next incremental run:
- `brands` → status=`completed` → **skipped**
- `categories` → status=`completed` → **skipped**
- `product_group` → status=`completed` → **skipped**
- `products` → status=`running` (crash) → **resumes from last checkpoint id**

---

## 8. Graceful Stop Mechanism

```
User clicks Stop
  └── POST /api/database/<id>/stop
      └── request_stop(db_id)
          └── UPDATE sync_jobs SET stop_requested = 1 WHERE id = <job_id>

Background sync thread (every batch):
  └── _is_stop_requested(job_id)
      └── SELECT stop_requested FROM sync_jobs WHERE id = ?
      └── If True:
          ├── save_checkpoint(db_id, table, last_id, last_updated_at, status='stopped')
          ├── append_log(..., 'Stop requested — halting after batch N')
          ├── Update live state → status='stopped'
          └── return result  ← thread exits naturally
```

The thread never gets force-killed. The MySQL connection closes cleanly. SQLite is committed up to the last batch. No data is lost.

---

## 9. Live Log Streaming

### Server side (sync_live_logs.py)

```python
_store: Dict[int, deque]  # One deque per db_id, maxlen=1000
_lock:  threading.Lock    # One global lock for thread safety

def append_log(db_id, level, message, **extra):
    entry = {"ts": UTC_ISO, "level": level.upper(), "message": message, **extra}
    with _lock:
        _get_deque(db_id).append(entry)

def get_logs(db_id, since_ts=None):
    with _lock:
        entries = list(_get_deque(db_id))
    if since_ts:
        return [e for e in entries if e["ts"] > since_ts]  # lexicographic ISO-8601 comparison
    return entries
```

### API endpoint

```
GET /api/database/<id>/logs              → all buffered entries (up to 1000)
GET /api/database/<id>/logs?since=<ts>  → only entries after that timestamp
```

### Browser-side polling (incremental)

```javascript
let _lastLogTs = null;   // tracks newest timestamp received
let _logLines  = [];     // buffer of rendered HTML lines (max 2000)

async function pollLogs() {
  const url = _lastLogTs
    ? `/api/database/${_selectedDbId}/logs?since=${encodeURIComponent(_lastLogTs)}`
    : `/api/database/${_selectedDbId}/logs`;

  const data = await fetch(url).then(r => r.json());
  if (data.logs?.length) appendLogEntries(data.logs);
}

function appendLogEntries(entries) {
  entries.forEach(e => {
    const line = `<span class="log-${e.level}">[${time}] [${level}] ${escHtml(e.message)}</span>`;
    _logLines.push(line);
    if (_logLines.length > 2000) _logLines.shift();
  });
  _lastLogTs = entries[entries.length - 1].ts;  // advance cursor
  console_.innerHTML = _logLines.slice(-500).join("<br>");
}
```

### Log colors

| Level | Color |
|---|---|
| DEBUG | `#64748b` (gray) |
| INFO | `#94a3b8` (light gray) |
| WARNING | `#fbbf24` (amber) |
| ERROR | `#f87171` (red) |

### Poll intervals

| State | Progress poll | Log poll |
|---|---|---|
| Sync running | 2.5 seconds | 2.5 seconds |
| Sync idle | 10 seconds | 10 seconds |

---

## 10. Product Metrics Aggregation

Instead of syncing millions of raw transaction rows, a **single GROUP BY query** runs against MySQL:

```sql
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
```

The result is normalized and upserted into `product_metrics`:
- `popularity_score` = `product_qty / max_qty_across_all_products` (0.0–1.0)
- Used in the composite search ranking formula:
  ```
  final_score = 0.7 × fuzzy_score + 0.2 × popularity_score + 0.1 × click_rate
  ```

This runs **only after all core tables succeed** and **only if no stop/error occurred**.

---

## 11. Index Rebuild Rules

| Event | Index rebuilt? | Cache cleared? |
|---|---|---|
| Full sync completes successfully | **Yes** | **Yes** |
| Incremental sync completes | No | No |
| Sync stops (user stopped) | No | No |
| Sync fails with error | No | No |
| User clicks "Rebuild Index" button | Yes | Yes |
| `POST /api/search/rebuild` called | Yes | Yes |

**Why not rebuild on incremental?** Rebuilding takes several seconds and blocks search while running. Incremental syncs are designed to be fast and non-disruptive. For live production, trigger a manual rebuild during off-peak hours.

---

## 12. SQLite Tables Reference

### Core sync tables

| Table | Source | Rows | Key column |
|---|---|---|---|
| `brands` | MySQL brands | ~100s | `id`, `source_db_id` |
| `categories` | MySQL categories | ~100s | `id`, `source_db_id` |
| `product_group` | MySQL product_group | ~10s | `id`, `source_db_id` |
| `products` | MySQL products | ~1000s–100,000s | `id`, `source_db_id` |
| `product_metrics` | MySQL (aggregated) | 1 row per product | `product_id`, `source_db_id` |

### Sync control tables

| Table | Purpose |
|---|---|
| `connected_databases` | One row per ERP source (replaces `db_settings.json`) |
| `sync_jobs` | One row per sync run; carries `stop_requested` flag |
| `sync_checkpoints` | Per-table cursor state for crash-resume |
| `sync_errors` | Row-level failure log (payload + traceback) |
| `sync_log` | Historical record of each table sync (used by history tab) |

### search_history schema summary

```sql
sync_checkpoints:
  database_id               INTEGER  -- FK to connected_databases
  table_name                TEXT
  last_processed_id         INTEGER  -- id of last successfully upserted row
  last_processed_updated_at TEXT     -- updated_at of that row
  status                    TEXT     -- running | completed | stopped | failed

sync_errors:
  database_id   INTEGER
  table_name    TEXT
  source_row_id INTEGER   -- MySQL PK of the failing row
  error_message TEXT
  raw_payload   TEXT      -- JSON snapshot (capped 8KB)
  traceback     TEXT      -- Python traceback (capped 4KB)

sync_jobs:
  database_id    INTEGER
  status         TEXT     -- pending | running | completed | failed | stopped
  stop_requested INTEGER  -- 0 or 1
  current_table  TEXT     -- which table is being synced right now
  progress       INTEGER  -- 0–100 percent estimate
```

---

## 13. API Endpoints Reference

### Database management

| Method | URL | What it does |
|---|---|---|
| GET | `/api/databases` | List all connected databases (passwords masked) |
| POST | `/api/databases` | Add new ERP connection |
| GET | `/api/database/<id>` | Get one database |
| PUT | `/api/database/<id>` | Update credentials |
| DELETE | `/api/database/<id>` | Remove database |
| POST | `/api/database/<id>/test` | Test MySQL connection |

### Sync control

| Method | URL | Body | What it does |
|---|---|---|---|
| POST | `/api/database/<id>/sync` | `{"full": true\|false}` | Start sync (returns immediately) |
| POST | `/api/database/<id>/stop` | — | Set `stop_requested=1` |
| GET | `/api/database/<id>/status` | — | Live state + last job + checkpoints |
| GET | `/api/database/<id>/logs` | `?since=<iso-ts>` | In-memory log buffer |
| GET | `/api/database/<id>/errors` | `?limit=100` | Recent row-level errors |
| GET | `/api/databases/live` | — | Live states for all databases |

### Search & index

| Method | URL | What it does |
|---|---|---|
| GET | `/api/search` | Fuzzy search |
| POST | `/api/search/rebuild` | Rebuild in-memory index + clear cache |
| GET | `/api/sync/history` | Sync log history (legacy) |
| GET | `/api/sync/status` | Latest sync per table (legacy) |

---

## 14. Module File Map

```
modules/
├── sync_manager.py        Main sync engine: sync_database(), _sync_table(),
│                          _upsert_batch_isolated(), request_stop(), get_live_state()
├── sync_normalization.py  Per-table NULL-safe row cleaners
│                          normalize_row(table, raw_row) → clean dict or None
├── sync_errors.py         Row failure persistence
│                          log_row_error(), get_recent_errors(), get_error_count()
├── sync_live_logs.py      In-memory ring buffer per db_id
│                          append_log(), get_logs(since_ts=), clear_logs()
├── sync_checkpoints.py    SQLite checkpoint CRUD
│                          get_checkpoint(), save_checkpoint(), clear_checkpoints()
├── db_manager.py          connected_databases CRUD
│                          list_databases(), add_database(), update_sync_status()
├── fuzzy_search.py        In-memory FuzzySearchEngine, synonym expansion
├── cache.py               Search result LRU cache (redis or in-memory)
├── analytics.py           log_search(), search history queries
├── autocomplete.py        get_suggestions()
└── settings_manager.py    Legacy db_settings.json read/write (backward compat)

db/
├── schema.sql             Full SQLite DDL
└── database.py            init_db(), get_connection(), migrations

routes/
├── search_routes.py       /api/search*, /api/autocomplete, /api/cache/*
├── synonym_routes.py      /api/synonyms*
└── image_search_routes.py /api/image-search

templates/
├── sync.html              Sync management UI (live log, progress table, error panel)
├── settings.html          Database connection management UI
└── ...
```

---

## 15. Data Flow Diagram

```
                          ┌──────────────────────────────────────┐
                          │          Browser (sync.html)          │
                          │                                        │
                          │  [Full Sync]  [Incremental]  [Stop]   │
                          │                                        │
                          │  ┌────────────────────────────────┐   │
                          │  │  Live Progress Table (9 cols)  │   │
                          │  │  Table | Status | Synced | ...  │   │
                          │  └────────────────────────────────┘   │
                          │                                        │
                          │  ┌────────────────────────────────┐   │
                          │  │  Debug Log Console (dark)      │   │
                          │  │  [10:32:01] [INFO] brands done │   │
                          │  └────────────────────────────────┘   │
                          │                                        │
                          │  ┌────────────────────────────────┐   │
                          │  │  Sync Errors Panel (red)       │   │
                          │  │  Table | Row ID | Error | ...  │   │
                          │  └────────────────────────────────┘   │
                          └──────────────┬───────────────────────-┘
                                         │ HTTP (polling every 2.5s)
                    ┌────────────────────▼──────────────────────────┐
                    │               Flask (app.py)                   │
                    │  POST /api/database/<id>/sync                  │
                    │  GET  /api/database/<id>/status  (live state)  │
                    │  GET  /api/database/<id>/logs    (?since=ts)   │
                    │  GET  /api/database/<id>/errors                │
                    └────────────────────┬──────────────────────────┘
                                         │ daemon thread
                    ┌────────────────────▼──────────────────────────┐
                    │           sync_manager.py (background)         │
                    │                                                │
                    │  sync_database(db_id, full)                    │
                    │    │                                           │
                    │    ├── For each table (brands→products):       │
                    │    │     _sync_table()                         │
                    │    │       ├── Fresh MySQL conn per batch      │
                    │    │       ├── Dual-key cursor query           │
                    │    │       ├── normalize_row() per row         │
                    │    │       ├── _upsert_row() per row           │
                    │    │       ├── log_row_error() on failure      │
                    │    │       └── save_checkpoint() after commit  │
                    │    │                                           │
                    │    └── _sync_product_metrics()                 │
                    │          One GROUP BY query → product_metrics  │
                    └──────────────┬──────────────────────────────--┘
                                   │
              ┌────────────────────┴──────────────────────────┐
              │                                                │
   ┌──────────▼──────────┐                      ┌────────────▼─────────────┐
   │    MySQL (ERP)       │                      │     SQLite (local)        │
   │  brands              │                      │  brands                   │
   │  categories          │                      │  categories               │
   │  product_group       │  ──── sync ────→     │  product_group            │
   │  products            │                      │  products                 │
   │  transactions        │  ── aggregate →      │  product_metrics          │
   │  transaction_sell_   │                      │  sync_jobs                │
   │    lines             │                      │  sync_checkpoints         │
   └──────────────────────┘                      │  sync_errors              │
                                                 │  sync_log                 │
                                                 └───────────────────────────┘
```
