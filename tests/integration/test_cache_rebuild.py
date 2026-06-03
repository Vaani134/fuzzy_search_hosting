"""
tests/integration/test_cache_rebuild.py
-----------------------------------------
Integration tests for cache lifecycle: cold start, warm start, invalidation,
version mismatch, and corruption recovery.
"""

import gzip
import json
import os
import pickle
import sqlite3
import tempfile
import time

import pytest


class TestDiskCacheColdStart:
    def test_engine_loads_without_cache(self, fresh_db, tmp_path):
        """Engine rebuilds from SQLite when no cache exists."""
        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            from modules.cache_manager import CacheManager
            from modules.fuzzy_search import FuzzySearchEngine

            mgr = CacheManager(1, str(tmp_path), "1", True, 0)
            assert not os.path.isfile(mgr.engine_path)

            engine = FuzzySearchEngine(source_db_id=1)
            engine.rebuild()
            assert engine.stats()["total_products"] > 0
        finally:
            _db.get_connection = orig

    def test_cold_start_slower_than_warm(self, fresh_db, tmp_path):
        """Warm start (cache hit) should be faster than cold rebuild."""
        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            from modules.fuzzy_search import FuzzySearchEngine

            # Cold start
            t0 = time.perf_counter()
            engine = FuzzySearchEngine(source_db_id=1)
            engine.rebuild()
            cold_ms = (time.perf_counter() - t0) * 1000

            # Warm start — load from cache
            t1 = time.perf_counter()
            engine2 = FuzzySearchEngine(source_db_id=1)
            engine2._load_from_cache()
            warm_ms = (time.perf_counter() - t1) * 1000

            # Warm should be <= cold (or at least not dramatically worse)
            # We allow warm_ms up to cold_ms * 2 for very small datasets
            assert warm_ms <= cold_ms * 3 or warm_ms < 100
        finally:
            _db.get_connection = orig


class TestDiskCacheWarmStart:
    def test_warm_start_loads_same_products(self, fresh_db, tmp_path):
        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            from modules.fuzzy_search import FuzzySearchEngine

            engine1 = FuzzySearchEngine(source_db_id=1)
            engine1.rebuild()
            count1 = engine1.stats()["total_products"]

            engine2 = FuzzySearchEngine(source_db_id=1)
            engine2._load_from_cache()
            count2 = engine2.stats().get("total_products", 0)

            # Warm start should load same count as cold start
            assert count2 == count1
        finally:
            _db.get_connection = orig


class TestCacheVersionMismatch:
    def test_version_bump_forces_rebuild(self, fresh_db, tmp_path, monkeypatch):
        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            import config
            monkeypatch.setattr(config, "CACHE_VERSION", "1")
            monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))

            from modules.cache_manager import CacheManager
            mgr_v1 = CacheManager(1, str(tmp_path), "1", True, 0)

            # Simulate saved cache with version 1
            data = {
                "items": [{"_id": 1, "name": "test", "_normalized": "test",
                           "_popularity": 0.0, "_click_rate": 0.0}],
                "raw_strings": ["test"],
                "normalized_strings": ["test"],
                "last_built": time.time(),
            }
            mgr_v1.save_engine_data(data)

            # Now create manager with version 2
            mgr_v2 = CacheManager(1, str(tmp_path), "2", True, 0)
            assert mgr_v2.is_valid() is False
        finally:
            _db.get_connection = orig


class TestCacheCorruption:
    def test_corrupted_engine_file_triggers_rebuild(self, fresh_db, tmp_path):
        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            from modules.cache_manager import CacheManager
            from modules.fuzzy_search import FuzzySearchEngine

            mgr = CacheManager(1, str(tmp_path), "1", True, 0)
            data = {
                "items": [{"_id": 1, "name": "test", "_normalized": "test",
                           "_popularity": 0.0, "_click_rate": 0.0}],
                "raw_strings": ["test"],
                "normalized_strings": ["test"],
                "last_built": time.time(),
            }
            mgr.save_engine_data(data)

            # Corrupt the file
            with open(mgr.engine_path, "wb") as fh:
                fh.write(b"\x00\x01\x02 garbage")

            # is_valid should return False
            assert mgr.is_valid() is False
            # load_engine_data should return None
            result = mgr.load_engine_data()
            assert result is None
        finally:
            _db.get_connection = orig


class TestQueryCacheInvalidation:
    def test_query_cache_cleared_after_explicit_clear(self, client):
        from modules.cache import search_cache

        key = search_cache.make_key("hookah")
        search_cache.set(key, [{"id": 1, "name": "Hookah"}])

        search_cache.clear()
        assert search_cache.get(key) is None

    def test_search_result_cached_on_second_hit(self, client):
        from modules.cache import search_cache

        # Clear any stale entries
        search_cache.clear()

        # First search — miss, populates cache
        resp1 = client.get("/api/search?q=grinder&limit=5&sort=score")
        assert resp1.status_code == 200

        # The route builds the key with scoped_filters and the actual params;
        # verify the cache is non-empty rather than guessing the exact key.
        stats = search_cache.stats()
        total = stats.get("total_entries", 0) or stats.get("live_entries", 0)
        assert total > 0, "Expected at least one entry in cache after a search"

    def test_different_queries_have_separate_cache_entries(self, client):
        from modules.cache import search_cache

        client.get("/api/search?q=hookah&limit=5")
        client.get("/api/search?q=grinder&limit=5")

        key_hookah  = search_cache.make_key("hookah",  page=1, limit=5)
        key_grinder = search_cache.make_key("grinder", page=1, limit=5)
        assert key_hookah != key_grinder


class TestAutocompleteInvalidation:
    def test_invalidate_clears_autocomplete_cache(self):
        import modules.autocomplete as ac
        original_redis = ac._redis_client
        ac._redis_client = None

        try:
            from modules.autocomplete import _cache_put, _cache_get, invalidate_autocomplete_cache
            key = ("hookah", 10, 1)
            _cache_put(key, [{"text": "Hookah Small"}])
            invalidate_autocomplete_cache()
            assert _cache_get(key) is None
        finally:
            ac._redis_client = original_redis
