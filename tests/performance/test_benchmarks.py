"""
tests/performance/test_benchmarks.py
--------------------------------------
pytest-benchmark suite for search, autocomplete, cache, and rebuild.

Run:
    pytest tests/performance/ --benchmark-only --benchmark-json=tests/reports/benchmark_report.json
"""

import sqlite3
import time
import os

import pytest


# ── Engine benchmarks ─────────────────────────────────────────────────────────

class TestSearchBenchmarks:
    def test_bench_single_search(self, benchmark, populated_engine):
        result = benchmark(populated_engine.search, "hookah", top_k=20)
        assert isinstance(result, list)

    def test_bench_fuzzy_typo_search(self, benchmark, populated_engine):
        result = benchmark(populated_engine.search, "hooka", top_k=20)
        assert isinstance(result, list)

    def test_bench_global_search_small(self, benchmark, populated_engine):
        result = benchmark(populated_engine.search, "grinder", top_k=20)
        assert isinstance(result, list)

    def test_bench_search_k1(self, benchmark, populated_engine):
        result = benchmark(populated_engine.search, "hookah", top_k=1)
        assert len(result) <= 1

    def test_bench_search_k100(self, benchmark, populated_engine):
        result = benchmark(populated_engine.search, "hookah", top_k=100)
        assert isinstance(result, list)

    def test_bench_zero_result_query(self, benchmark, populated_engine):
        result = benchmark(populated_engine.search, "xzqmwvb_zzz", top_k=20)
        assert result == []


class TestAutocompleteBenchmarks:
    @pytest.fixture(autouse=True)
    def patch_db(self, test_db_path):
        import db.database as _db
        original = _db.get_connection

        def _conn():
            conn = sqlite3.connect(test_db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        import modules.autocomplete as ac
        orig_redis = ac._redis_client
        ac._redis_client = None
        with ac._mem_lock:
            ac._mem_cache.clear()
        yield
        _db.get_connection = original
        ac._redis_client = orig_redis

    def test_bench_autocomplete_cold(self, benchmark, test_db_path):
        import modules.autocomplete as ac
        with ac._mem_lock:
            ac._mem_cache.clear()

        from modules.autocomplete import get_suggestions

        def _cold():
            with ac._mem_lock:
                ac._mem_cache.clear()
            return get_suggestions("hook", limit=10, source_db_id=1)

        result = benchmark(_cold)
        assert isinstance(result, list)

    def test_bench_autocomplete_warm(self, benchmark, test_db_path):
        from modules.autocomplete import get_suggestions
        get_suggestions("hook", limit=10, source_db_id=1)  # warm cache
        result = benchmark(get_suggestions, "hook", limit=10, source_db_id=1)
        assert isinstance(result, list)


class TestCacheBenchmarks:
    def test_bench_cache_get_miss(self, benchmark, search_cache_instance):
        result = benchmark(search_cache_instance.get, "nonexistent_key_xyz_123")
        assert result is None

    def test_bench_cache_set(self, benchmark, search_cache_instance):
        data = [{"id": i, "name": f"Product {i}"} for i in range(20)]
        from modules.cache import SearchCache
        key = SearchCache.make_key("benchmark_query")
        benchmark(search_cache_instance.set, key, data)

    def test_bench_cache_get_hit(self, benchmark, search_cache_instance):
        from modules.cache import SearchCache
        key = SearchCache.make_key("warm_key")
        data = [{"id": i, "name": f"Product {i}"} for i in range(20)]
        search_cache_instance.set(key, data)
        result = benchmark(search_cache_instance.get, key)
        assert result is not None

    def test_bench_make_key(self, benchmark):
        from modules.cache import SearchCache
        result = benchmark(SearchCache.make_key, "hookah grinder", page=1, limit=20)
        assert isinstance(result, str)


class TestRebuildBenchmarks:
    def test_bench_engine_rebuild(self, benchmark, fresh_db):
        import db.database as _db
        original = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            from modules.fuzzy_search import FuzzySearchEngine
            engine = FuzzySearchEngine(source_db_id=1)
            engine.rebuild()  # initial build

            result = benchmark(engine.rebuild)
        finally:
            _db.get_connection = original


# ── Latency percentile assertion helper ──────────────────────────────────────

class TestLatencyThresholds:
    """
    These tests FAIL if the system exceeds the production readiness thresholds.
    They run a micro-benchmark and check P95/P99 against SLA limits.
    """

    def test_search_p95_under_100ms(self, populated_engine):
        latencies = []
        for _ in range(100):
            t0 = time.perf_counter()
            populated_engine.search("hookah", top_k=20)
            latencies.append((time.perf_counter() - t0) * 1000)

        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        assert p95 < 100.0, f"P95 search latency {p95:.1f}ms exceeds 100ms threshold"

    def test_search_p99_under_250ms(self, populated_engine):
        latencies = []
        for _ in range(100):
            t0 = time.perf_counter()
            populated_engine.search("grinder", top_k=20)
            latencies.append((time.perf_counter() - t0) * 1000)

        latencies.sort()
        p99 = latencies[int(len(latencies) * 0.99)]
        assert p99 < 250.0, f"P99 search latency {p99:.1f}ms exceeds 250ms threshold"

    def test_autocomplete_p95_under_50ms(self, test_db_path):
        import modules.autocomplete as ac
        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(test_db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        orig_redis = ac._redis_client
        ac._redis_client = None

        try:
            from modules.autocomplete import get_suggestions
            get_suggestions("hook", limit=10, source_db_id=1)  # warm

            latencies = []
            for _ in range(100):
                t0 = time.perf_counter()
                get_suggestions("hook", limit=10, source_db_id=1)
                latencies.append((time.perf_counter() - t0) * 1000)

            latencies.sort()
            p95 = latencies[int(len(latencies) * 0.95)]
            assert p95 < 50.0, f"P95 autocomplete latency {p95:.1f}ms exceeds 50ms"
        finally:
            _db.get_connection = orig
            ac._redis_client = orig_redis
