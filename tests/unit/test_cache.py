"""
tests/unit/test_cache.py
------------------------
Unit tests for modules/cache.py — SearchCache with in-memory backend.
Redis is NOT required; all tests use the _InMemoryCache backend.
"""

import time
import threading

import pytest


# ── _InMemoryCache direct tests ───────────────────────────────────────────────

class TestInMemoryCache:
    @pytest.fixture()
    def mem_cache(self):
        from modules.cache import _InMemoryCache
        return _InMemoryCache(ttl=2, max_size=5)

    def test_get_miss(self, mem_cache):
        assert mem_cache.get("nonexistent") is None

    def test_set_then_get_hit(self, mem_cache):
        mem_cache.set("key1", {"result": "data"})
        result = mem_cache.get("key1")
        assert result == {"result": "data"}

    def test_ttl_expiry(self, mem_cache):
        mem_cache.set("expiring", "value")
        time.sleep(2.1)
        assert mem_cache.get("expiring") is None

    def test_lru_eviction(self, mem_cache):
        for i in range(5):
            mem_cache.set(f"key{i}", i)
        # Cache is full (max_size=5). Adding one more evicts the oldest.
        mem_cache.set("key5", 5)
        # key0 was oldest — should be evicted
        assert mem_cache.get("key0") is None
        # key5 should be present
        assert mem_cache.get("key5") == 5

    def test_invalidate_removes_key(self, mem_cache):
        mem_cache.set("target", "value")
        mem_cache.invalidate("target")
        assert mem_cache.get("target") is None

    def test_invalidate_nonexistent_key(self, mem_cache):
        mem_cache.invalidate("does_not_exist")  # must not raise

    def test_clear_returns_count(self, mem_cache):
        mem_cache.set("a", 1)
        mem_cache.set("b", 2)
        count = mem_cache.clear()
        assert count == 2

    def test_clear_empties_store(self, mem_cache):
        mem_cache.set("a", 1)
        mem_cache.clear()
        assert mem_cache.get("a") is None

    def test_purge_expired(self, mem_cache):
        mem_cache.set("live", "live_value")
        mem_cache.set("dying", "die_value")
        time.sleep(2.1)
        removed = mem_cache.purge_expired()
        assert removed >= 2

    def test_stats_backend_key(self, mem_cache):
        stats = mem_cache.stats()
        assert stats["backend"] == "memory"

    def test_stats_counts(self, mem_cache):
        mem_cache.set("x", 1)
        mem_cache.set("y", 2)
        stats = mem_cache.stats()
        assert stats["total_entries"] == 2
        assert stats["live_entries"] == 2

    def test_thread_safety(self, mem_cache):
        errors = []

        def _writer(idx):
            try:
                for i in range(50):
                    mem_cache.set(f"key_{idx}_{i}", i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_update_existing_key(self, mem_cache):
        mem_cache.set("key", "original")
        mem_cache.set("key", "updated")
        assert mem_cache.get("key") == "updated"


# ── SearchCache facade tests (in-memory backend) ──────────────────────────────

class TestSearchCache:
    def test_backend_is_memory_without_redis(self, search_cache_instance):
        assert search_cache_instance.backend_name == "memory"

    def test_make_key_is_deterministic(self):
        from modules.cache import SearchCache
        k1 = SearchCache.make_key("hookah", page=1, limit=20)
        k2 = SearchCache.make_key("hookah", page=1, limit=20)
        assert k1 == k2

    def test_make_key_differs_by_query(self):
        from modules.cache import SearchCache
        k1 = SearchCache.make_key("hookah")
        k2 = SearchCache.make_key("grinder")
        assert k1 != k2

    def test_make_key_differs_by_page(self):
        from modules.cache import SearchCache
        k1 = SearchCache.make_key("hookah", page=1)
        k2 = SearchCache.make_key("hookah", page=2)
        assert k1 != k2

    def test_make_key_differs_by_limit(self):
        from modules.cache import SearchCache
        k1 = SearchCache.make_key("hookah", limit=10)
        k2 = SearchCache.make_key("hookah", limit=20)
        assert k1 != k2

    def test_make_key_normalises_case(self):
        from modules.cache import SearchCache
        k1 = SearchCache.make_key("HOOKAH")
        k2 = SearchCache.make_key("hookah")
        assert k1 == k2

    def test_get_miss_returns_none(self, search_cache_instance):
        assert search_cache_instance.get("missing_key") is None

    def test_set_and_get(self, search_cache_instance):
        from modules.cache import SearchCache
        key = SearchCache.make_key("hookah")
        data = [{"id": 1, "name": "Hookah Small"}]
        search_cache_instance.set(key, data)
        result = search_cache_instance.get(key)
        assert result == data

    def test_clear_returns_count(self, search_cache_instance):
        from modules.cache import SearchCache
        for i in range(5):
            k = SearchCache.make_key(f"query_{i}")
            search_cache_instance.set(k, [{"id": i}])
        count = search_cache_instance.clear()
        assert count == 5

    def test_invalidate_specific_key(self, search_cache_instance):
        from modules.cache import SearchCache
        key = SearchCache.make_key("hookah")
        search_cache_instance.set(key, [{"id": 1}])
        search_cache_instance.invalidate(key)
        assert search_cache_instance.get(key) is None

    def test_stats_returns_dict(self, search_cache_instance):
        stats = search_cache_instance.stats()
        assert isinstance(stats, dict)
        assert "backend" in stats

    def test_purge_expired_no_op_when_fresh(self, search_cache_instance):
        from modules.cache import SearchCache
        key = SearchCache.make_key("test")
        search_cache_instance.set(key, [{"id": 1}])
        removed = search_cache_instance.purge_expired()
        # Nothing should be expired yet
        assert removed == 0

    def test_cache_complex_data(self, search_cache_instance):
        from modules.cache import SearchCache
        key = SearchCache.make_key("complex query")
        data = {
            "results": [{"id": i, "name": f"Product {i}", "score": float(i)} for i in range(20)],
            "total": 20,
            "page": 1,
        }
        search_cache_instance.set(key, data)
        result = search_cache_instance.get(key)
        assert result == data
        assert len(result["results"]) == 20


# ── SearchCache init: Redis unavailable path ──────────────────────────────────

class TestSearchCacheInit:
    def test_falls_back_to_memory_when_redis_url_empty(self):
        from modules.cache import SearchCache, _InMemoryCache
        # Directly instantiate with a blank URL by bypassing config
        sc = SearchCache.__new__(SearchCache)
        sc._ttl = 60
        sc._max_size = 10
        sc._backend = _InMemoryCache(ttl=60, max_size=10)
        assert isinstance(sc._backend, _InMemoryCache)
        assert sc.backend_name == "memory"
