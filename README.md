# Fuzzy Search Hosting

Production-grade multi-database fuzzy search platform built with Flask + SQLite + RapidFuzz.

**Key capabilities:**
- MySQL → SQLite incremental sync with per-database isolation and crash-resume
- Fuzzy search (RapidFuzz blend + composite ranking) per isolated DB or globally
- Persistent engine disk cache — 80–95% faster cold starts
- Query result cache (Redis / in-memory LRU) with hit/miss tracking
- In-memory TTL autocomplete cache (30s, 500-entry LRU)
- Incremental in-memory index updates — no full rebuild on small sync batches
- Per-source priority boosting in global search
- Comprehensive metrics: P50/P95/P99 latency, cache hit rates, rebuild counters
- Autocomplete, synonyms, image search, click/popularity ranking, analytics

---

## 1. High-Level Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                       Flask Application (app.py)                   │
│  ┌───────────┐   ┌──────────────┐   ┌───────────────────────────┐  │
│  │ Search UI │   │ Synonym Mgmt │   │  Multi-DB Sync Control    │  │
│  └─────┬─────┘   └──────────────┘   └──────────────┬────────────┘  │
│        │                                             │               │
│  ┌─────▼─────────────────────────────────────────────▼───────────┐  │
│  │              routes/search_routes.py  (API)                    │  │
│  │  • latency tracking (time.perf_counter)                        │  │
│  │  • query cache hit/miss → modules/metrics.py                   │  │
│  │  • autocomplete latency → modules/metrics.py                   │  │
│  └────────────────────────────┬───────────────────────────────────┘  │
│                                │                                      │
│  ┌─────────────────────────────▼────────────────────────────────┐    │
│  │              modules/fuzzy_search.py                          │    │
│  │                                                               │    │
│  │  get_engine(db_id)           get_global_engine()             │    │
│  │       │                              │                        │    │
│  │  Isolated Engine              Global Engine                  │    │
│  │  (one per source DB)       (all DBs merged; source priority) │    │
│  │       │                              │                        │    │
│  │  ┌────▼──────────────────────────────▼──────┐                │    │
│  │  │          Disk Cache Layer                 │                │    │
│  │  │     modules/cache_manager.py              │                │    │
│  │  │  cache/db_1/engine.pkl.gz                 │                │    │
│  │  │  cache/db_2/engine.pkl.gz                 │                │    │
│  │  │  cache/global/engine.pkl.gz               │                │    │
│  │  └───────────────────────────────────────────┘                │    │
│  │                                                               │    │
│  │  update_products_incremental(ids) — merge subset, no rebuild  │    │
│  │  remove_products(ids)             — drop from in-memory index │    │
│  └───────────────────────────────────────────────────────────────┘    │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │  modules/query result cache: modules/cache.py (Redis/memory)  │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │  modules/autocomplete.py — TTL cache (30s, 500-entry LRU)     │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │  modules/metrics.py — SearchMetrics singleton                  │   │
│  │  • disk cache hits/misses  • query cache hits/misses           │   │
│  │  • search latency P50/P95/P99  • autocomplete latency          │   │
│  │  • rebuild count + duration percentiles                        │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │  modules/sync_manager.py  ←  MySQL source DBs                 │   │
│  │  (incremental sync, crash-resume, per-DB isolation)           │   │
│  │  → rebuilds engines + clears query & autocomplete caches      │   │
│  └────────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
                              │ SQLite  db/local.db
```

---

## 2. Scoring & Ranking

### Fuzzy blend (per product)
```
blend = 0.5 × token_set_ratio + 0.3 × WRatio + 0.2 × partial_ratio
```
Scored against both normalised and raw text; higher wins.

### Boost rules (applied on top of blend, capped at 100)
| Condition | Boost |
|-----------|-------|
| Exact match (normalised) | +20 |
| Starts-with match | +10 |
| Query is substring of raw name | +10 |

### Composite ranking (relevance-first)
```
gap = best_fuzzy_in_results − this_product_fuzzy

if gap > 10:          final = fuzzy + source_priority          # clear winner
else:                  final = 0.85×fuzzy + 0.10×popularity
                             + 0.05×click_rate + source_priority  # tie band
```

`source_priority` is the per-DB boost from `SEARCH_SOURCE_PRIORITY` config and is only applied in global search mode. This ensures popular products never outscore clearly more relevant ones, while equally good matches from a preferred source float to the top.

---

## 3. Disk Cache Layer

Persistent gzip+pickle cache for the in-memory search index.

### Directory layout
```
cache/
  global/
    engine.pkl.gz    ← serialised FuzzySearchEngine state
    metadata.json    ← version, product count, checksum, built_at
  db_1/
    engine.pkl.gz
    metadata.json
  db_2/
    engine.pkl.gz
    metadata.json
```

### 7-gate validation (all must pass to use cache)
| Gate | Check |
|------|-------|
| 1 | metadata.json is readable and valid JSON |
| 2 | `cache_version` matches `CACHE_VERSION` env var |
| 3 | `schema_version` matches internal `_SCHEMA_VERSION` |
| 4 | Age < `CACHE_MAX_AGE` seconds (default 7 days) |
| 5 | Product count in SQLite matches `product_count` in metadata |
| 6 | engine.pkl.gz file exists |
| 7 | SHA-256 checksum of engine.pkl.gz matches `checksum` in metadata |

### Atomic writes
Cache files are written to a `.tmp` suffix first, then renamed via `os.replace()`. This prevents a crash mid-write from corrupting the live cache file.

### Startup cleanup
On startup, `AUTO_CLEANUP=true` (default) calls `cleanup_stale_tmp_files(CACHE_DIR)` to remove any orphaned `.tmp` files left by a previous interrupted write.

---

## 4. Query Result Cache

Short-term in-memory (or Redis) cache for search API responses.

- **Backend**: Redis when `REDIS_URL` is set and reachable; falls back to in-memory dict automatically.
- **Key**: `hash(query + filters + page + limit + sort + db_id)`
- **Invalidated**: automatically cleared after every successful sync via `search_cache.clear()`.
- **Metrics**: every cache access increments `search_metrics.record_query_cache_hit/miss()`.

---

## 5. Autocomplete Cache

In-process TTL cache inside `modules/autocomplete.py`.

- **Structure**: `OrderedDict` with `(normalised_query, limit, source_db_id)` keys → `(results, expiry)` values.
- **TTL**: 30 seconds (configurable via `_CACHE_TTL`).
- **Capacity**: 500 entries; LRU eviction when full (oldest entry popped from front).
- **Invalidated**: `invalidate_autocomplete_cache()` is called by `sync_manager._trigger_index_rebuild()` after every successful sync.
- **Thread-safe**: single `threading.Lock` guards all reads and writes.

---

## 6. Incremental Index Updates

Instead of a full SQLite rebuild, individual products can be merged into the live index:

```python
engine = get_engine(source_db_id=1)
engine.update_products_incremental([scoped_id_1, scoped_id_2])  # fetch + merge
engine.remove_products([scoped_id_3])                            # drop from index
```

After `update_products_incremental`, popularity and click signals are re-normalised across the entire in-memory corpus — no DB query needed.

### Incremental update lifecycle (7 steps)

1. **Fetch outside lock** — new product dicts loaded from SQLite (I/O outside critical section).
2. **CoW snapshot** — shallow copy of `_items`, `_raw_strings`, `_normalized_strings` lists.
3. **Merge into copies** — existing IDs updated in-place within the copy; new IDs appended.
4. **Re-normalise signals** — popularity and click-rate min-max normalised across the merged corpus.
5. **Atomic swap** — `self._items = merged_items` (and the other two lists) under the lock; all three references updated together.
6. **Compaction counter** — increments `_incremental_update_count`; when it reaches `FULL_REBUILD_AFTER_N_INCREMENTALS`, the counter resets and a compaction is scheduled.
7. **Background compaction** — `_compaction_rebuild()` runs in a daemon thread: calls `rebuild()` which builds a fresh index from SQLite, then atomically swaps it in and resets the counter.

### Periodic compaction rebuild

After `N` incremental updates the engine spawns a background `_compaction_rebuild()` thread that performs a full `rebuild()`. This reclaims memory fragmentation from accumulated incremental merges without blocking search.

```bash
export FULL_REBUILD_AFTER_N_INCREMENTALS=500   # 0 = disable compaction
```

The compaction counter and threshold are visible in `GET /api/cache/stats` under `engines`:
```json
"db_1": {
  "mode": "isolated",
  "total_products": 42000,
  "incremental_update_count": 127,
  "compaction_threshold": 500
}
```

---

## 7. Copy-on-Write Concurrency Model

The in-memory index (`_items`, `_raw_strings`, `_normalized_strings`) uses a **copy-on-write (CoW)** strategy:

- **Writers** (incremental update, remove, compaction rebuild) create **new list objects**, populate them, then atomically swap the reference under `_lock`.
- **Readers** (search threads) acquire `_lock` only to read the current list references, then immediately release it and hold their own snapshot — searching against an immutable list that will never be mutated under them.
- This makes search **effectively lock-free** after the initial snapshot acquisition: concurrent writes never block concurrent reads.

```
writer thread:                   reader thread:
─────────────────────────────    ──────────────────────────────────
merged = list(self._items)       with self._lock:
# ... populate merged ...            items = self._items   ← snapshot
with self._lock:                 # lock released
    self._items = merged         for item in items:        ← safe, immutable view
                                     score(item, query)
```

### Signal writes on shared dicts
Unchanged product dicts may be referenced by both the old and new list snapshots. Signal fields (`_popularity`, `_click_rate`) are updated as atomic dict `__setitem__` operations (CPython GIL). Concurrent reads see either the old or new float — never a torn value.

### Lock type
`FuzzySearchEngine._lock` is a `threading.RLock` (re-entrant). This allows `rebuild()` (which acquires the lock for the final swap) to be called from within `_compaction_rebuild()` without deadlocking, even if the same thread previously acquired the lock.

---

## 8. Metrics & Observability

`GET /api/cache/stats` returns a comprehensive JSON snapshot:

```json
{
  "query_cache": {
    "hits": 1240, "misses": 88, "hit_rate_pct": 93.4, "size": 52
  },
  "disk_cache": {
    "global": {"cached": true, "product_count": 60000, "built_at": "...", "size_bytes": 12000000},
    "db_1":   {"cached": true, "product_count": 42000, "built_at": "...", "size_bytes": 8500000},
    "db_2":   {"cached": true, "product_count": 18000, "built_at": "...", "size_bytes": 3800000}
  },
  "engines": {
    "db_1":   {
      "mode": "isolated", "total_products": 42000, "last_built": 1716400000.0,
      "incremental_update_count": 127, "compaction_threshold": 500
    },
    "global": {
      "mode": "global", "total_products": 60000, "last_built": 1716400001.2,
      "incremental_update_count": 0, "compaction_threshold": 500
    }
  },
  "metrics": {
    "uptime_seconds": 3600.0,
    "disk_cache":  {"hits": 3, "misses": 0, "hit_rate_pct": 100.0},
    "query_cache": {"hits": 1240, "misses": 88, "hit_rate_pct": 93.4},
    "searches": {
      "total": 1328, "zero_results": 12,
      "latency_p50_ms": 4.2, "latency_p95_ms": 18.7, "latency_p99_ms": 45.1,
      "latency_avg_ms": 6.1, "latency_samples": 1000
    },
    "autocomplete": {
      "latency_p50_ms": 0.8, "latency_p95_ms": 3.2,
      "latency_p99_ms": 9.1, "latency_avg_ms": 1.1, "latency_samples": 800
    },
    "rebuilds": {
      "count": 3, "last_ms": 4210.0,
      "duration_p50_ms": 3800.0, "duration_p95_ms": 5200.0,
      "duration_p99_ms": 5200.0, "duration_avg_ms": 4070.0, "duration_samples": 3
    }
  }
}
```

### Metrics rolling windows
| Window | Max samples | Percentiles |
|--------|-------------|-------------|
| Search latency | 1000 (configurable via `METRICS_LATENCY_WINDOW`) | P50, P95, P99, avg |
| Autocomplete latency | 1000 | P50, P95, P99, avg |
| Rebuild duration | 100 | P50, P95, P99, avg |

---

## 9. Sync Lifecycle

```
POST /api/database/<id>/sync
         │
         ▼
  sync_manager.sync_database_background()
         │
         ▼
  ┌──────────────────────────────────────────────────────┐
  │  1. Create sync_jobs row (status=running)            │
  │  2. Detect full vs incremental vs crash-resume       │
  │  3. Sync brands → categories → product_group         │
  │     → products  (dual-key cursor, batched)           │
  │  4. Sync product_metrics (aggregate from MySQL)      │
  │  5. Mark job completed, update last_sync_at          │
  │  6. Rebuild isolated engine (get_engine(db_id))      │
  │  7. Rebuild global engine   (rebuild_global_index()) │
  │  8. Clear query result cache (search_cache.clear())  │
  │  9. Clear autocomplete cache (invalidate_...)        │
  └──────────────────────────────────────────────────────┘
```

Steps 6–9 only run on a **successful** sync. Partial, stopped, or failed syncs do not rebuild — the existing index continues serving queries uninterrupted.

---

## 10. Cold-Start Sequence

```
app.py starts
    │
    ├── init_db()                          — ensure SQLite schema exists
    ├── seed_primary_database_from_settings()
    ├── _cleanup_stale_running_sync_jobs()
    ├── cleanup_stale_tmp_files(CACHE_DIR)  — AUTO_CLEANUP (removes .tmp leftovers)
    ├── reload_synonyms()
    │
    └── get_engine(source_db_id=1)
            │
            ├── _load_from_cache()
            │       │
            │       ├── CACHE_ENABLED=false? → return False
            │       ├── 7-gate validation fails? → miss (record_disk_cache_miss)
            │       ├── PASS → load pkl.gz → record_disk_cache_hit → return True
            │       └── Exception → miss (record_disk_cache_miss) → return False
            │
            └── (on cache miss) rebuild()
                    │
                    ├── _load_products_from_db()   (full table scan)
                    ├── normalise signals (min-max)
                    ├── build raw + normalised string arrays
                    ├── record_rebuild(duration_ms)
                    └── _save_to_cache()           (atomic write to .tmp → rename)
```

**Typical startup times (42k products):**
| Scenario | Time |
|----------|------|
| Warm cache (valid pkl.gz) | ~0.3 s |
| Cold start (SQLite rebuild) | ~4–6 s |
| Cache invalidated after sync | 4–6 s (one-time, then warm) |

---

## 11. Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_ENABLED` | `true` | Enable/disable disk cache |
| `CACHE_DIR` | `cache/` | Directory for engine cache files |
| `CACHE_COMPRESSION` | `true` | Gzip compress engine cache |
| `CACHE_VERSION` | `1` | Bump to force global cache invalidation |
| `CACHE_MAX_AGE` | `604800` (7 days) | Max cache age in seconds; `0` = no limit |
| `AUTO_CLEANUP` | `true` | Remove orphaned `.tmp` files on startup |
| `METRICS_LATENCY_WINDOW` | `1000` | Rolling window size for latency percentiles |
| `SEARCH_SOURCE_PRIORITY` | `{}` | JSON map of `db_id → float` priority boost |
| `REDIS_URL` | `` | Redis URL for query cache; empty = in-memory |
| `REDIS_KEY_PREFIX` | `fzsearch:` | Key prefix for Redis cache entries |
| `SEARCH_MIN_SCORE` | `35.0` | Minimum composite score to include in results |
| `SEARCH_DEFAULT_K` | `20` | Default result count |
| `SEARCH_MAX_K` | `100` | Hard cap on result count |
| `SYNC_BATCH_SIZE` | `2000` | Rows per MySQL fetch batch during sync |
| `FULL_REBUILD_AFTER_N_INCREMENTALS` | `500` | Trigger background compaction rebuild after N incremental updates; `0` = disabled |
| `MAX_QUERY_CACHE_ENTRIES` | `1000` | Max entries in the in-memory query result cache (LRU eviction) |
| `MAX_AUTOCOMPLETE_CACHE_ENTRIES` | `500` | Max entries in the in-memory autocomplete cache (LRU eviction) |
| `MAX_DISK_CACHE_MB` | `500` | Max total size of disk cache in MB; oldest slots removed when exceeded; `0` = no limit |

### SEARCH_SOURCE_PRIORITY example
```bash
export SEARCH_SOURCE_PRIORITY='{"1": 5.0, "2": 0.0}'
```
This boosts DB-1 results by 5 points in global search scoring. Only applied in global mode (`?db_id=all`); isolated per-DB searches are unaffected.

---

## 12. API Reference

### Search
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/search?q=...` | Paginated fuzzy search |
| GET | `/api/search?q=...&db_id=all` | Global search across all DBs |
| GET | `/api/autocomplete?q=...` | Autocomplete suggestions (TTL cached) |
| GET | `/api/search/history` | Recent search queries |
| GET | `/api/search/top` | Most-searched queries |
| GET | `/api/search/zero-results` | Queries returning no results |
| GET | `/api/search/trending` | Queries trending in last N hours |
| POST | `/api/search/rebuild` | Rebuild in-memory index |

### Cache & Metrics
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/cache/stats` | Full cache + metrics snapshot |
| POST | `/api/cache/clear` | Flush query result cache |

### Sync
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/database/<id>/sync` | Start sync (full or incremental) |
| POST | `/api/database/<id>/stop` | Graceful stop |
| GET | `/api/database/<id>/status` | Live sync state + job history |
| GET | `/api/database/<id>/logs` | Real-time sync log buffer |
| GET | `/api/database/<id>/errors` | Row-level sync errors |

---

## 13. Production Deployment

### Requirements
```
flask
rapidfuzz
pymysql
```
Optional: `redis` (for distributed query cache across workers).

### Environment
```bash
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export FLASK_ENV=production
export REDIS_URL=redis://localhost:6379/0   # optional
export CACHE_ENABLED=true
export AUTO_CLEANUP=true
```

### Thread safety
- `FuzzySearchEngine._lock` is a `threading.RLock` — re-entrant safe; allows `rebuild()` to be called from `_compaction_rebuild()` without deadlock.
- Index reads use CoW snapshots — threads hold their own list reference and release the lock before searching, so writes never block reads.
- `CacheManager` uses per-slot `threading.Lock` — concurrent read/write to the same slot is serialised.
- `SearchMetrics` uses a single `threading.Lock` — all counter increments are O(1).
- `autocomplete._cache_lock` is a `threading.Lock` — OrderedDict mutations are atomic.

### Memory management
| Layer | Bound mechanism |
|-------|----------------|
| Query result cache | `MAX_QUERY_CACHE_ENTRIES` — LRU eviction (oldest-timestamp entry dropped) |
| Autocomplete cache | `MAX_AUTOCOMPLETE_CACHE_ENTRIES` — LRU eviction (front of OrderedDict popped) |
| Disk cache | `MAX_DISK_CACHE_MB` — oldest-slot removal after each successful save |
| Latency windows | `METRICS_LATENCY_WINDOW` — fixed-size `deque(maxlen=N)`, O(1) append/pop |
| In-memory index | Bounded by product count in SQLite; CoW briefly doubles peak RSS during merge |

### Scaling notes
- All caches are **process-local**. Multi-worker deployments (gunicorn) require Redis (`REDIS_URL`) for the query cache; the disk cache and autocomplete cache are per-process but idempotent.
- The disk cache is write-safe across concurrent startups because of atomic `os.replace()` writes.
- Set `METRICS_LATENCY_WINDOW` lower (e.g. `200`) on memory-constrained deployments.
- Compaction rebuilds run in daemon threads — they do not block request handling but will briefly double RSS during the build. Tune `FULL_REBUILD_AFTER_N_INCREMENTALS` to balance memory fragmentation vs. rebuild cost.

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Slow cold starts every restart | Cache disabled or always invalidating | Check `CACHE_ENABLED`, `CACHE_VERSION`, `CACHE_MAX_AGE` |
| Search returns stale products | Query cache not cleared after sync | Verify sync completed successfully (`status: ok`) |
| Autocomplete shows old data | Autocomplete cache not invalidated | Check sync succeeded; TTL expires in 30s anyway |
| `GET /api/cache/stats` shows 0 latency samples | No searches run yet | Normal — percentiles populate after first queries |
| `disk_cache_miss` counter rising | Cache corrupted / schema changed | Bump `CACHE_VERSION` to invalidate all slots |
| `.tmp` files accumulating | Server crashed during cache write | Set `AUTO_CLEANUP=true` and restart |
| Memory grows steadily after many syncs | Index fragmentation from incremental merges | Lower `FULL_REBUILD_AFTER_N_INCREMENTALS` to trigger compaction sooner |
| Disk cache directory growing unbounded | `MAX_DISK_CACHE_MB` not set | Set `MAX_DISK_CACHE_MB=500` (or any limit); oldest slots removed automatically |
| Query cache growing too large | Default `MAX_QUERY_CACHE_ENTRIES=1000` too high | Reduce via env var; in-memory LRU evicts automatically but peak RSS depends on result payload size |
| Compaction rebuild running too frequently | `FULL_REBUILD_AFTER_N_INCREMENTALS` too low | Increase the threshold; each compaction briefly doubles index RSS |
