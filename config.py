"""
config.py
---------
Central configuration for the Fuzzy Search App.
Edit MySQL credentials and paths here.
"""

import os

# ── MySQL (source) ─────────────────────────────────────────────────────────────
MYSQL_CONFIG = {
    "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "user":     os.getenv("MYSQL_USER",     "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "cloudbbtl_novx"),
    "charset":  "utf8mb4",
}
# ── NOTE ──────────────────────────────────────────────────────────────────────
# Database: cloudbbtl_novx  |  Host: 127.0.0.1  |  User: root  |  Password: ""
# Override any value above via environment variables or a .env file.

# ── SQLite (local cache) ───────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.getenv("SQLITE_PATH") or os.path.join(BASE_DIR, "db", "local.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "db", "schema.sql")

# ── Sync settings ──────────────────────────────────────────────────────────────
# Legacy list (kept for backward-compat with old /api/sync endpoint).
# New code uses CORE_SYNC_TABLES from sync_manager.py.
SYNC_TABLES = [
    "brands",
    "categories",
    "product_group",
    "products",
    "transactions",
    "transaction_sell_lines",
]

# Core tables synced by the new multi-DB engine (sync_manager.py).
# transactions and transaction_sell_lines are intentionally excluded —
# their data is captured via a lightweight aggregate into product_metrics.
CORE_SYNC_TABLES = [
    "brands",
    "categories",
    "product_group",
    "products",
]

# How many rows to fetch per batch during sync.
# 2000 rows ≈ 244 round-trips for 488k products. Fewer connections = less timeout risk.
SYNC_BATCH_SIZE = 2000

# ── Search settings ────────────────────────────────────────────────────────────
SEARCH_MIN_SCORE  = 35.0   # discard results below this score
SEARCH_DEFAULT_K  = 20     # default number of results to return
SEARCH_MAX_K      = 100    # hard cap

# Score thresholds for UI badge colours
SCORE_HIGH   = 70
SCORE_MEDIUM = 50
SCORE_LOW    = 35

# ── Image CDN ─────────────────────────────────────────────────────────────────
# Base URL prepended to relative image paths stored in the database.
# DB stores:  /img/chinahosmall_p1.jpg  or  img/chinahosmall_p1.jpg
# Full URL:   https://novxcloud.com/uploads/img/chinahosmall_p1.jpg
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "https://novxcloud.com")

# ── Flask ──────────────────────────────────────────────────────────────────────
# SECRET_KEY is REQUIRED for production. Use a strong random value:
#   python -c "import secrets; print(secrets.token_hex(32))"
# Set via environment variable: export SECRET_KEY="..."
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # Development fallback only — MUST be overridden in production
    if os.getenv("FLASK_ENV") == "production":
        raise ValueError(
            "SECRET_KEY environment variable is REQUIRED in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    SECRET_KEY = "dev-key-change-in-production"

DEBUG = os.getenv("FLASK_ENV", "production").lower() != "production"
HOST = os.getenv("FLASK_HOST", "0.0.0.0")
PORT = int(os.getenv("FLASK_PORT", "5000"))

# ── Redis (optional search result cache) ──────────────────────────────────────
# When REDIS_URL is set and Redis is reachable, SearchCache uses Redis.
# When Redis is unavailable or REDIS_URL is blank, it falls back to the
# in-memory dict cache automatically — no code changes needed.
# Example: redis://user:password@localhost:6379/0
REDIS_URL = os.getenv("REDIS_URL", "")
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "fzsearch:")

# ── Engine disk cache (persistent index cache) ─────────────────────────────────
# Persists the fuzzy-search engine's in-memory product index to disk so that
# application restarts load instantly from cache instead of rebuilding from
# SQLite from scratch.  Set CACHE_ENABLED=false to disable entirely.
CACHE_ENABLED     = os.getenv("CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
CACHE_DIR         = os.getenv("CACHE_DIR") or os.path.join(BASE_DIR, "cache")
CACHE_COMPRESSION = os.getenv("CACHE_COMPRESSION", "true").lower() in ("1", "true", "yes")
# Increment CACHE_VERSION to force invalidation of all cached engine indexes.
CACHE_VERSION     = os.getenv("CACHE_VERSION", "1")
# Maximum cache age in seconds before it is considered stale.  0 = no age limit.
CACHE_MAX_AGE     = int(os.getenv("CACHE_MAX_AGE", str(86400 * 7)))   # 7 days default
# Clean up orphaned .tmp files and empty cache directories on startup.
AUTO_CLEANUP      = os.getenv("AUTO_CLEANUP", "true").lower() in ("1", "true", "yes")

# ── Metrics ────────────────────────────────────────────────────────────────────
# Number of recent latency samples retained in each rolling percentile window.
METRICS_LATENCY_WINDOW = int(os.getenv("METRICS_LATENCY_WINDOW", "1000"))

# ── Periodic compaction rebuild ────────────────────────────────────────────────
# After this many incremental index updates, a full rebuild() is triggered in
# a background thread to reclaim memory fragmentation.  0 = disabled.
FULL_REBUILD_AFTER_N_INCREMENTALS = int(os.getenv("FULL_REBUILD_AFTER_N_INCREMENTALS", "500"))

# ── Hard cache limits ──────────────────────────────────────────────────────────
# Maximum number of entries in the in-memory query result cache (LRU eviction).
MAX_QUERY_CACHE_ENTRIES = int(os.getenv("MAX_QUERY_CACHE_ENTRIES", "1000"))

# Maximum number of entries in the in-memory autocomplete cache (LRU eviction).
MAX_AUTOCOMPLETE_CACHE_ENTRIES = int(os.getenv("MAX_AUTOCOMPLETE_CACHE_ENTRIES", "500"))

# Maximum total size of all on-disk engine cache files in megabytes.
# When exceeded after a save, the oldest slot(s) are removed until under limit.
# 0 = no disk cache size limit.
MAX_DISK_CACHE_MB = int(os.getenv("MAX_DISK_CACHE_MB", "500"))

# ── Source priority ────────────────────────────────────────────────────────────
# Optional per-database priority boost applied to global search scoring.
# Map source_db_id (int) → priority float.  Higher = ranked earlier.
# Example: {1: 5.0, 2: 0.0}  boosts DB-1 results by 5 points in global ranking.
# Override at runtime by setting SEARCH_SOURCE_PRIORITY as JSON:
#   export SEARCH_SOURCE_PRIORITY='{"1": 5.0}'
import json as _json
_raw_priority = os.getenv("SEARCH_SOURCE_PRIORITY", "{}")
try:
    SEARCH_SOURCE_PRIORITY: dict = {int(k): float(v) for k, v in _json.loads(_raw_priority).items()}
except Exception:
    SEARCH_SOURCE_PRIORITY: dict = {}