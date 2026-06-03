"""
tests/conftest.py
-----------------
Shared fixtures for the entire test suite.

All tests run against an isolated temporary SQLite database and cache
directory so the real application data is never touched.

Environment variables are forced at module-import time (before any
project module reads config.py) by setting them at the top of this file.
"""

import gzip
import json
import os
import pickle
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Generator, List, Dict
from unittest.mock import MagicMock, patch

import pytest

# ── Point every config read to test paths BEFORE project imports ────────────
_TEST_SESSION_DIR = tempfile.mkdtemp(prefix="fzsearch_test_")
_TEST_DB_PATH     = os.path.join(_TEST_SESSION_DIR, "test.db")
_TEST_CACHE_DIR   = os.path.join(_TEST_SESSION_DIR, "cache")

os.environ["SQLITE_PATH"]       = _TEST_DB_PATH
os.environ["CACHE_DIR"]         = _TEST_CACHE_DIR
os.environ["REDIS_URL"]         = ""          # disable Redis for all tests
os.environ["FLASK_ENV"]         = "development"
os.environ["SECRET_KEY"]        = "test-secret-key-not-for-production"
os.environ["CACHE_ENABLED"]     = "true"
os.environ["CACHE_VERSION"]     = "1"
os.environ["CACHE_MAX_AGE"]     = "604800"
os.environ["METRICS_LATENCY_WINDOW"] = "200"
os.environ["FULL_REBUILD_AFTER_N_INCREMENTALS"] = "0"   # disable auto-compaction
os.environ["MAX_QUERY_CACHE_ENTRIES"]     = "100"
os.environ["MAX_AUTOCOMPLETE_CACHE_ENTRIES"] = "50"
os.environ["MAX_DISK_CACHE_MB"]  = "0"
os.environ["IMAGE_BASE_URL"]    = "https://test.example.com"

# ── Project root on the path ─────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# ── Schema path ──────────────────────────────────────────────────────────────
_SCHEMA_PATH = os.path.join(_PROJECT_ROOT, "db", "schema.sql")


# ── Sample product catalogue ─────────────────────────────────────────────────

SAMPLE_PRODUCTS: List[Dict] = [
    {"id": 1,  "name": "Hookah Small Glass",        "sku": "HK-001", "brand_id": 1, "category_id": 1, "source_db_id": 1, "is_inactive": 0},
    {"id": 2,  "name": "Hookah Large Glass",         "sku": "HK-002", "brand_id": 1, "category_id": 1, "source_db_id": 1, "is_inactive": 0},
    {"id": 3,  "name": "Tobacco Menthol 50g",        "sku": "TB-001", "brand_id": 2, "category_id": 2, "source_db_id": 1, "is_inactive": 0},
    {"id": 4,  "name": "Charcoal Quick Light 100pc", "sku": "CH-001", "brand_id": 3, "category_id": 3, "source_db_id": 1, "is_inactive": 0},
    {"id": 5,  "name": "Grinder Metal 4-Part",       "sku": "GR-001", "brand_id": 4, "category_id": 4, "source_db_id": 1, "is_inactive": 0},
    {"id": 6,  "name": "Lighter Windproof Torch",    "sku": "LT-001", "brand_id": 5, "category_id": 5, "source_db_id": 1, "is_inactive": 0},
    {"id": 7,  "name": "Rolling Paper King Size",    "sku": "RP-001", "brand_id": 2, "category_id": 6, "source_db_id": 1, "is_inactive": 0},
    {"id": 8,  "name": "Blunt Wrap Vanilla",         "sku": "BW-001", "brand_id": 2, "category_id": 6, "source_db_id": 1, "is_inactive": 0},
    {"id": 9,  "name": "Vape Pen Starter Kit",       "sku": "VP-001", "brand_id": 6, "category_id": 7, "source_db_id": 1, "is_inactive": 0},
    {"id": 10, "name": "E-Cigarette Disposable",     "sku": "EC-001", "brand_id": 6, "category_id": 7, "source_db_id": 1, "is_inactive": 0},
    {"id": 11, "name": "Ashtray Ceramic Square",     "sku": "AS-001", "brand_id": 3, "category_id": 8, "source_db_id": 1, "is_inactive": 0},
    {"id": 12, "name": "Filter Cigarette Tips 100x", "sku": "FI-001", "brand_id": 2, "category_id": 6, "source_db_id": 1, "is_inactive": 0},
    {"id": 13, "name": "Hookah Charcoal Chimney",    "sku": "HC-001", "brand_id": 1, "category_id": 3, "source_db_id": 1, "is_inactive": 0},
    {"id": 14, "name": "Glass Bong Bubbler 10 inch", "sku": "GB-001", "brand_id": 7, "category_id": 9, "source_db_id": 1, "is_inactive": 0},
    {"id": 15, "name": "Inactive Product Old",       "sku": "IN-001", "brand_id": 1, "category_id": 1, "source_db_id": 1, "is_inactive": 1},
    # DB-2 products
    {"id": 16, "name": "Hookah Premium Set DB2",     "sku": "HK2-001", "brand_id": 1, "category_id": 1, "source_db_id": 2, "is_inactive": 0},
    {"id": 17, "name": "Tobacco Black Cherry DB2",   "sku": "TB2-001", "brand_id": 2, "category_id": 2, "source_db_id": 2, "is_inactive": 0},
]

SAMPLE_BRANDS: List[Dict] = [
    {"id": 1, "name": "HookahKing",   "business_id": 1},
    {"id": 2, "name": "TobaccoPro",   "business_id": 1},
    {"id": 3, "name": "CoalMaster",   "business_id": 1},
    {"id": 4, "name": "GrindCraft",   "business_id": 1},
    {"id": 5, "name": "FlameX",       "business_id": 1},
    {"id": 6, "name": "VapeCloud",    "business_id": 1},
    {"id": 7, "name": "GlassWorks",   "business_id": 1},
]

SAMPLE_CATEGORIES: List[Dict] = [
    {"id": 1, "name": "Hookahs",          "business_id": 1, "parent_id": 0},
    {"id": 2, "name": "Tobacco",          "business_id": 1, "parent_id": 0},
    {"id": 3, "name": "Charcoal",         "business_id": 1, "parent_id": 0},
    {"id": 4, "name": "Grinders",         "business_id": 1, "parent_id": 0},
    {"id": 5, "name": "Lighters",         "business_id": 1, "parent_id": 0},
    {"id": 6, "name": "Rolling Supplies", "business_id": 1, "parent_id": 0},
    {"id": 7, "name": "Vaping",           "business_id": 1, "parent_id": 0},
    {"id": 8, "name": "Accessories",      "business_id": 1, "parent_id": 0},
    {"id": 9, "name": "Glass",            "business_id": 1, "parent_id": 0},
]


# ── DB connection patcher ────────────────────────────────────────────────────
# All project modules do `from db.database import get_connection`, which creates
# a local name binding.  Patching only db.database.get_connection doesn't
# redirect those already-imported references.  This helper patches every module.

def _make_conn_factory(db_path: str):
    def _conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn
    return _conn


def patch_all_db_connections(db_path: str):
    """
    Return a context manager that redirects all project DB connections to
    *db_path* and restores them on exit.
    """
    import contextlib
    import db.database as _db_mod

    @contextlib.contextmanager
    def _ctx():
        factory = _make_conn_factory(db_path)

        # Modules that hold their own reference via `from db.database import`
        import modules.autocomplete as _ac
        import modules.fuzzy_search as _fs

        originals = {
            "_db_mod": _db_mod.get_connection,
            "_ac":     getattr(_ac, "get_connection", None),
            "_fs":     getattr(_fs, "get_connection", None),
        }

        # db.database.get_connection: all lazy `from db.database import get_connection`
        # calls pick this up automatically (cache_manager._get_product_count uses lazy import).
        _db_mod.get_connection = factory
        _ac.get_connection     = factory
        _fs.get_connection     = factory

        try:
            yield
        finally:
            _db_mod.get_connection = originals["_db_mod"]
            if originals["_ac"] is not None:
                _ac.get_connection = originals["_ac"]
            if originals["_fs"] is not None:
                _fs.get_connection = originals["_fs"]

    return _ctx()


# ── Low-level DB helpers ──────────────────────────────────────────────────────

def _create_test_db(db_path: str) -> None:
    """Create schema + all migrations and insert sample data into *db_path*."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(schema_sql)

    # Apply the migrations that live in db/database.py (Migration 7 adds
    # source_db_id to products/brands/categories/product_group).
    for tbl in ("products", "brands", "categories", "product_group"):
        try:
            conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN source_db_id INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # Seed connected_databases
    conn.execute(
        """INSERT OR IGNORE INTO connected_databases
           (id, name, host, port, username, password, database_name, sync_status)
           VALUES (1,'Test DB 1','localhost',3306,'root','','test_db1','ok'),
                  (2,'Test DB 2','localhost',3306,'root','','test_db2','ok')"""
    )

    # Seed brands
    for b in SAMPLE_BRANDS:
        conn.execute(
            "INSERT OR IGNORE INTO brands (id, name, business_id, created_by) VALUES (?,?,?,0)",
            (b["id"], b["name"], b["business_id"]),
        )

    # Seed categories
    for c in SAMPLE_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories (id, name, business_id, parent_id, created_by) VALUES (?,?,?,?,0)",
            (c["id"], c["name"], c["business_id"], c["parent_id"]),
        )

    # Seed products — use scoped_id as primary key (source_db_id * 1e9 + id)
    for p in SAMPLE_PRODUCTS:
        scoped = p["source_db_id"] * 1_000_000_000 + p["id"]
        conn.execute(
            """INSERT OR IGNORE INTO products
               (id, name, sku, brand_id, category_id, business_id,
                is_inactive, not_for_selling, out_of_stock,
                enable_stock, ml, created_by, source_db_id)
               VALUES (?,?,?,?,?,1,?,0,0,0,0.0,0,?)""",
            (
                scoped, p["name"], p["sku"],
                p.get("brand_id"), p.get("category_id"),
                p["is_inactive"], p["source_db_id"],
            ),
        )

    conn.commit()
    conn.close()


# ── Session-scoped fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def test_session_dir():
    """Return the session temp dir; clean up after the session."""
    os.makedirs(_TEST_CACHE_DIR, exist_ok=True)
    yield _TEST_SESSION_DIR
    shutil.rmtree(_TEST_SESSION_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def test_db_path(test_session_dir) -> str:
    """Session-scoped SQLite test database (shared, read-heavy tests)."""
    _create_test_db(_TEST_DB_PATH)
    return _TEST_DB_PATH


@pytest.fixture(scope="session")
def test_cache_dir(test_session_dir) -> str:
    return _TEST_CACHE_DIR


# ── Function-scoped DB fixture ────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path) -> str:
    """A clean SQLite database for tests that mutate data."""
    db_path = str(tmp_path / "fresh.db")
    _create_test_db(db_path)
    return db_path


# ── Flask app + test client ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def flask_app(test_db_path, test_cache_dir):
    """Session-scoped Flask application pointing at the test DB."""
    import importlib
    # Reload config so it picks up the env vars we set above
    import config
    importlib.reload(config)

    # Patch get_connection globally before importing app
    import db.database as _db
    original_get_conn = _db.get_connection

    def _test_get_connection():
        conn = sqlite3.connect(test_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    _db.get_connection = _test_get_connection

    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    flask_app_module.app.config["WTF_CSRF_ENABLED"] = False

    yield flask_app_module.app

    _db.get_connection = original_get_conn


@pytest.fixture()
def client(flask_app):
    """A fresh Flask test client per test function."""
    with flask_app.test_client() as c:
        yield c


# ── Engine fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def populated_engine(test_db_path, test_cache_dir):
    """
    A FuzzySearchEngine pre-loaded with SAMPLE_PRODUCTS (db_id=1).
    Rebuilt once per session for performance.
    """
    with patch_all_db_connections(test_db_path):
        from modules.fuzzy_search import FuzzySearchEngine
        engine = FuzzySearchEngine(source_db_id=1)
        engine.rebuild()
        yield engine


@pytest.fixture()
def fresh_engine(fresh_db):
    """A FuzzySearchEngine rebuilt per test (allows mutation)."""
    with patch_all_db_connections(fresh_db):
        from modules.fuzzy_search import FuzzySearchEngine
        engine = FuzzySearchEngine(source_db_id=1)
        engine.rebuild()
        yield engine


# ── Cache manager fixtures ────────────────────────────────────────────────────

@pytest.fixture()
def cache_manager(tmp_path, monkeypatch):
    """
    A CacheManager pointed at a temp directory.
    _get_product_count is patched to return the count from sample_engine_data
    (1 item) so is_valid() passes the product-count gate in tests.
    """
    import modules.cache_manager as _cm
    monkeypatch.setattr(_cm, "_get_product_count", lambda db_id: 1)
    from modules.cache_manager import CacheManager
    return CacheManager(
        source_db_id=1,
        cache_dir=str(tmp_path),
        cache_version="1",
        compression=True,
        max_age=0,
    )


@pytest.fixture()
def search_cache_instance():
    """A fresh in-memory SearchCache (no Redis)."""
    from modules.cache import SearchCache, _InMemoryCache
    sc = SearchCache.__new__(SearchCache)
    sc._ttl = 60
    sc._max_size = 100
    sc._backend = _InMemoryCache(ttl=60, max_size=100)
    return sc


# ── Metrics fixture ───────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_metrics():
    """A freshly reset SearchMetrics instance."""
    from modules.metrics import SearchMetrics
    return SearchMetrics(latency_window=100)


# ── Sample engine data (for cache_manager tests) ─────────────────────────────

@pytest.fixture()
def sample_engine_data():
    """Minimal engine data dict that CacheManager.save_engine_data expects."""
    items = [
        {
            "_id": 1_000_000_001,
            "name": "Hookah Small Glass",
            "_normalized": "hookah small glass",
            "_popularity": 50.0,
            "_click_rate": 10.0,
            "source_db_id": 1,
        }
    ]
    return {
        "items": items,
        "raw_strings": ["Hookah Small Glass"],
        "normalized_strings": ["hookah small glass"],
        "last_built": time.time(),
    }
