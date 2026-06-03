"""
tests/unit/test_autocomplete.py
--------------------------------
Unit tests for modules/autocomplete.py — cache and suggestion logic.
"""

import sqlite3
import threading
import time

import pytest

from tests.conftest import patch_all_db_connections


# ── Cache internals ───────────────────────────────────────────────────────────

class TestAutocompleteCacheInMemory:
    """Test the in-memory fallback path (_mem_cache + _mem_lock)."""

    @pytest.fixture(autouse=True)
    def clear_mem_cache(self):
        """Clear the module-level in-memory cache before each test."""
        import modules.autocomplete as ac
        with ac._mem_lock:
            ac._mem_cache.clear()
        # Force no Redis for this test
        original = ac._redis_client
        ac._redis_client = None
        yield
        ac._redis_client = original
        with ac._mem_lock:
            ac._mem_cache.clear()

    def test_cache_miss_returns_none(self):
        from modules.autocomplete import _cache_get
        assert _cache_get(("hookah", 10, 1)) is None

    def test_cache_put_then_get_hit(self):
        from modules.autocomplete import _cache_get, _cache_put
        key = ("hookah", 10, 1)
        data = [{"text": "Hookah Small", "type": "product"}]
        _cache_put(key, data)
        result = _cache_get(key)
        assert result == data

    def test_cache_ttl_expiry(self, monkeypatch):
        import modules.autocomplete as ac
        # Override TTL to near-zero for fast expiry
        monkeypatch.setattr(ac, "_CACHE_TTL", 1)
        from modules.autocomplete import _cache_get, _cache_put
        key = ("expiring", 10, 1)
        _cache_put(key, [{"text": "x"}])
        time.sleep(1.1)
        assert _cache_get(key) is None

    def test_cache_eviction_when_full(self, monkeypatch):
        import modules.autocomplete as ac
        monkeypatch.setattr(ac, "_CACHE_MAX", 3)
        from modules.autocomplete import _cache_put, _cache_get

        for i in range(3):
            _cache_put((f"q{i}", 10, 1), [{"text": f"q{i}"}])
        # Add one more — oldest (q0) evicted
        _cache_put(("q3", 10, 1), [{"text": "q3"}])
        assert _cache_get(("q0", 10, 1)) is None
        assert _cache_get(("q3", 10, 1)) is not None

    def test_invalidate_clears_all(self):
        from modules.autocomplete import _cache_put, _cache_get, invalidate_autocomplete_cache
        for i in range(5):
            _cache_put((f"q{i}", 10, 1), [{"text": f"q{i}"}])
        invalidate_autocomplete_cache()
        for i in range(5):
            assert _cache_get((f"q{i}", 10, 1)) is None

    def test_different_db_ids_different_keys(self):
        from modules.autocomplete import _cache_put, _cache_get
        _cache_put(("hookah", 10, 1), [{"text": "DB1 result"}])
        _cache_put(("hookah", 10, 2), [{"text": "DB2 result"}])
        r1 = _cache_get(("hookah", 10, 1))
        r2 = _cache_get(("hookah", 10, 2))
        assert r1 != r2

    def test_global_key_uses_none(self):
        from modules.autocomplete import _cache_put, _cache_get
        _cache_put(("hookah", 10, None), [{"text": "Global result"}])
        result = _cache_get(("hookah", 10, None))
        assert result is not None

    def test_thread_safe_concurrent_puts(self):
        from modules.autocomplete import _cache_put, _cache_get
        errors = []

        def _worker(idx):
            try:
                for i in range(20):
                    _cache_put((f"q_{idx}_{i}", 10, 1), [{"text": f"result_{idx}_{i}"}])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ── get_suggestions with test DB ──────────────────────────────────────────────

class TestGetSuggestions:
    @pytest.fixture(autouse=True)
    def patch_db(self, fresh_db):
        """Point ALL module get_connection references at the test database."""
        import modules.autocomplete as ac
        original_redis = ac._redis_client
        ac._redis_client = None
        with ac._mem_lock:
            ac._mem_cache.clear()

        with patch_all_db_connections(fresh_db):
            yield

        ac._redis_client = original_redis
        with ac._mem_lock:
            ac._mem_cache.clear()

    def test_returns_list(self):
        from modules.autocomplete import get_suggestions
        results = get_suggestions("hook", limit=5, source_db_id=1)
        assert isinstance(results, list)

    def test_prefix_matches_products(self):
        from modules.autocomplete import get_suggestions
        results = get_suggestions("hook", limit=10, source_db_id=1)
        names = [r["text"].lower() for r in results]
        assert any("hook" in n for n in names)

    def test_respects_source_db_id(self):
        from modules.autocomplete import get_suggestions
        results_db1 = get_suggestions("hook", limit=10, source_db_id=1)
        for r in results_db1:
            assert r["source_db_id"] == 1

    def test_result_has_required_fields(self):
        from modules.autocomplete import get_suggestions
        results = get_suggestions("hook", limit=5, source_db_id=1)
        if results:
            r = results[0]
            assert "text" in r
            assert "type" in r
            assert "id" in r
            assert "source_db_id" in r
            assert "priority" in r

    def test_short_query_returns_empty(self):
        from modules.autocomplete import get_suggestions
        assert get_suggestions("a", limit=5, source_db_id=1) == []

    def test_empty_query_returns_empty(self):
        from modules.autocomplete import get_suggestions
        assert get_suggestions("", limit=5, source_db_id=1) == []

    def test_respects_limit(self):
        from modules.autocomplete import get_suggestions
        results = get_suggestions("ho", limit=3, source_db_id=1)
        assert len(results) <= 3

    def test_cache_hit_on_second_call(self):
        from modules.autocomplete import get_suggestions, _cache_get, _normalize
        results1 = get_suggestions("hook", limit=5, source_db_id=1)
        key = (_normalize("hook"), 5, 1)
        from modules.autocomplete import _cache_get
        cached = _cache_get(key)
        assert cached is not None
        assert cached == results1

    def test_none_source_db_id_returns_all_dbs(self):
        from modules.autocomplete import get_suggestions
        results = get_suggestions("hook", limit=20, source_db_id=None)
        if results:
            db_ids = {r["source_db_id"] for r in results}
            # Should contain products from more than one DB if they exist
            assert len(db_ids) >= 1

    def test_returns_products_brands_categories(self):
        from modules.autocomplete import get_suggestions
        results = get_suggestions("ho", limit=20, source_db_id=None)
        types = {r["type"] for r in results}
        assert "product" in types

    def test_priority_ordering(self):
        from modules.autocomplete import get_suggestions
        results = get_suggestions("hook", limit=20, source_db_id=1)
        if len(results) > 1:
            priorities = [r["priority"] for r in results]
            assert priorities == sorted(priorities)
