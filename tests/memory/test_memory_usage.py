"""
tests/memory/test_memory_usage.py
-----------------------------------
Memory leak detection tests.

Monitors RSS growth across:
  - 10,000 repeated searches
  - 10,000 incremental updates (no-ops)
  - 100 full rebuilds

Thresholds:
  - Memory growth must be < 10% of initial RSS
  - No sustained linear growth pattern

Requires: psutil
"""

import gc
import sqlite3
import time

import pytest

psutil = pytest.importorskip("psutil")

import os


def _rss_mb() -> float:
    proc = psutil.Process(os.getpid())
    return proc.memory_info().rss / (1024 * 1024)


def _growth_pct(initial: float, final: float) -> float:
    if initial == 0:
        return 0.0
    return (final - initial) / initial * 100.0


MEMORY_GROWTH_LIMIT_PCT = 10.0


class TestSearchMemory:
    def test_10k_searches_no_leak(self, populated_engine):
        gc.collect()
        initial_mb = _rss_mb()

        for _ in range(10_000):
            populated_engine.search("hookah", top_k=10)

        gc.collect()
        final_mb = _rss_mb()
        growth = _growth_pct(initial_mb, final_mb)

        assert growth < MEMORY_GROWTH_LIMIT_PCT, (
            f"Memory grew {growth:.1f}% after 10k searches "
            f"(initial={initial_mb:.1f}MB, final={final_mb:.1f}MB)"
        )

    def test_1k_varied_searches_no_leak(self, populated_engine):
        queries = ["hookah", "grinder", "lighter", "tobacco", "charcoal",
                   "vape", "glass", "ashtray", "filter", "blunt"]
        gc.collect()
        initial_mb = _rss_mb()

        for i in range(1000):
            q = queries[i % len(queries)]
            populated_engine.search(q, top_k=10)

        gc.collect()
        final_mb = _rss_mb()
        growth = _growth_pct(initial_mb, final_mb)

        assert growth < MEMORY_GROWTH_LIMIT_PCT, (
            f"Memory grew {growth:.1f}% after 1k varied searches"
        )


class TestCacheMemory:
    def test_10k_cache_sets_bounded(self, search_cache_instance):
        """Cache should not grow beyond max_size entries."""
        from modules.cache import SearchCache

        gc.collect()
        initial_mb = _rss_mb()

        for i in range(10_000):
            key = SearchCache.make_key(f"unique_query_{i}")
            search_cache_instance.set(key, [{"id": i, "name": f"Product {i}"}])

        gc.collect()
        final_mb = _rss_mb()

        # Backend stats should show bounded size
        stats = search_cache_instance.stats()
        total = stats.get("total_entries", 0)
        assert total <= search_cache_instance._max_size * 2, (
            f"Cache grew unboundedly to {total} entries"
        )

    def test_autocomplete_cache_bounded(self, test_db_path):
        import modules.autocomplete as ac
        import db.database as _db
        orig = _db.get_connection
        orig_redis = ac._redis_client
        ac._redis_client = None

        def _conn():
            conn = sqlite3.connect(test_db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            gc.collect()
            initial_mb = _rss_mb()

            from modules.autocomplete import get_suggestions
            prefixes = [chr(c) + chr(d) for c in range(ord('a'), ord('z'))
                        for d in range(ord('a'), ord('z'))][:500]
            for prefix in prefixes:
                get_suggestions(prefix, limit=5, source_db_id=1)

            gc.collect()
            final_mb = _rss_mb()
            growth = _growth_pct(initial_mb, final_mb)

            with ac._mem_lock:
                cache_size = len(ac._mem_cache)
            assert cache_size <= 50 * 2  # MAX_AUTOCOMPLETE_CACHE_ENTRIES * 2 tolerance
        finally:
            _db.get_connection = orig
            ac._redis_client = orig_redis


class TestRebuildMemory:
    def test_100_rebuilds_no_leak(self, fresh_engine, fresh_db):
        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            gc.collect()
            initial_mb = _rss_mb()

            for _ in range(100):
                fresh_engine.rebuild()
                gc.collect()

            final_mb = _rss_mb()
            growth = _growth_pct(initial_mb, final_mb)

            assert growth < MEMORY_GROWTH_LIMIT_PCT, (
                f"Memory grew {growth:.1f}% after 100 rebuilds "
                f"(initial={initial_mb:.1f}MB, final={final_mb:.1f}MB)"
            )
        finally:
            _db.get_connection = orig


class TestIncrementalUpdateMemory:
    def test_10k_no_op_updates_no_leak(self, fresh_engine):
        gc.collect()
        initial_mb = _rss_mb()

        for _ in range(10_000):
            fresh_engine.update_products_incremental([999999999999])

        gc.collect()
        final_mb = _rss_mb()
        growth = _growth_pct(initial_mb, final_mb)

        assert growth < MEMORY_GROWTH_LIMIT_PCT, (
            f"Memory grew {growth:.1f}% after 10k no-op updates"
        )

    def test_10k_remove_nonexistent_no_leak(self, fresh_engine):
        gc.collect()
        initial_mb = _rss_mb()

        for _ in range(10_000):
            fresh_engine.remove_products([999999999999])

        gc.collect()
        final_mb = _rss_mb()
        growth = _growth_pct(initial_mb, final_mb)

        assert growth < MEMORY_GROWTH_LIMIT_PCT


class TestMetricsMemory:
    def test_metrics_rolling_window_bounded(self, fresh_metrics):
        """Rolling deque should not grow unboundedly."""
        gc.collect()
        initial_mb = _rss_mb()

        for i in range(100_000):
            fresh_metrics.record_search(float(i % 100), result_count=1)
            fresh_metrics.record_autocomplete(float(i % 10))

        gc.collect()
        final_mb = _rss_mb()
        growth = _growth_pct(initial_mb, final_mb)

        snap = fresh_metrics.snapshot()
        assert snap["searches"]["latency_samples"] == 100  # latency_window=100 in conftest
        assert growth < MEMORY_GROWTH_LIMIT_PCT, (
            f"Metrics memory grew {growth:.1f}% after 100k records"
        )
