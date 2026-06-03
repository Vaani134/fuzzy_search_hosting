"""
tests/concurrency/test_concurrent_search.py
--------------------------------------------
Concurrency tests: race conditions, data corruption, CoW correctness.

Validates:
  - 50 / 100 concurrent search threads
  - Searches concurrent with rebuild
  - Searches concurrent with incremental updates
  - Searches concurrent with compaction rebuild
  - No crashes, no data corruption, no stale results
"""

import threading
import time
import sqlite3
from collections import Counter

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_threads(target, count: int, timeout: float = 10.0):
    """
    Run *count* threads executing *target()*.
    Returns (errors, results) where errors is a list of exceptions.
    """
    errors  = []
    results = []
    lock    = threading.Lock()

    def _wrapper():
        try:
            r = target()
            with lock:
                results.append(r)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=_wrapper) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    return errors, results


# ── 50 concurrent searches ────────────────────────────────────────────────────

class TestConcurrentSearches:
    def test_50_concurrent_searches_no_errors(self, populated_engine):
        errors, results = _run_threads(
            lambda: populated_engine.search("hookah", top_k=10),
            count=50,
        )
        assert not errors, f"Errors in concurrent search: {errors}"
        assert len(results) == 50

    def test_100_concurrent_searches_no_errors(self, populated_engine):
        errors, results = _run_threads(
            lambda: populated_engine.search("grinder", top_k=10),
            count=100,
        )
        assert not errors, f"Errors in 100 concurrent searches: {errors}"
        assert len(results) == 100

    def test_concurrent_results_are_consistent(self, populated_engine):
        """All threads searching for the same query should get the same results."""
        errors, results = _run_threads(
            lambda: populated_engine.search("hookah", top_k=5),
            count=20,
        )
        assert not errors
        # All result lists should have the same length
        lengths = [len(r) for r in results]
        assert len(set(lengths)) == 1, f"Inconsistent result counts: {lengths}"

    def test_concurrent_zero_result_queries(self, populated_engine):
        errors, results = _run_threads(
            lambda: populated_engine.search("xzqmwvb_zzz", top_k=5),
            count=30,
        )
        assert not errors
        for r in results:
            assert r == []

    def test_mixed_queries_concurrent(self, populated_engine):
        queries = ["hookah", "grinder", "lighter", "tobacco", "charcoal"]

        errors = []
        results = []
        lock = threading.Lock()

        def _run(q):
            try:
                r = populated_engine.search(q, top_k=5)
                with lock:
                    results.append((q, r))
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=_run, args=(q,))
            for _ in range(20)
            for q in queries
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors
        assert len(results) == 100


# ── Searches concurrent with rebuild ─────────────────────────────────────────

class TestSearchDuringRebuild:
    def test_search_during_rebuild_no_crash(self, fresh_engine, fresh_db):
        errors = []
        results = []
        lock = threading.Lock()
        stop = threading.Event()

        def _searcher():
            while not stop.is_set():
                try:
                    r = fresh_engine.search("hookah", top_k=5)
                    with lock:
                        results.append(r)
                except Exception as e:
                    with lock:
                        errors.append(e)
                time.sleep(0.005)

        def _rebuilder():
            import db.database as _db
            orig = _db.get_connection

            def _conn():
                conn = sqlite3.connect(fresh_db)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = OFF")
                return conn

            _db.get_connection = _conn
            try:
                for _ in range(3):
                    fresh_engine.rebuild()
                    time.sleep(0.01)
            finally:
                _db.get_connection = orig

        searchers = [threading.Thread(target=_searcher) for _ in range(5)]
        rebuilder = threading.Thread(target=_rebuilder)

        for s in searchers:
            s.start()
        rebuilder.start()

        rebuilder.join(timeout=10)
        stop.set()
        for s in searchers:
            s.join(timeout=3)

        assert not errors, f"Errors during rebuild+search: {errors}"

    def test_no_stale_references_after_rebuild(self, fresh_engine, fresh_db):
        """After rebuild, engine._items is a new list object (CoW)."""
        items_before = fresh_engine._items

        import db.database as _db
        orig = _db.get_connection

        def _conn():
            conn = sqlite3.connect(fresh_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            return conn

        _db.get_connection = _conn
        try:
            fresh_engine.rebuild()
        finally:
            _db.get_connection = orig

        items_after = fresh_engine._items
        assert items_after is not items_before  # CoW: new list object


# ── Searches concurrent with incremental updates ──────────────────────────────

class TestSearchDuringIncrementalUpdate:
    def test_search_during_incremental_update(self, fresh_engine, fresh_db):
        errors = []
        stop = threading.Event()

        def _searcher():
            while not stop.is_set():
                try:
                    fresh_engine.search("hookah", top_k=5)
                except Exception as e:
                    errors.append(e)
                time.sleep(0.005)

        def _updater():
            import db.database as _db
            orig = _db.get_connection

            def _conn():
                conn = sqlite3.connect(fresh_db)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = OFF")
                return conn

            _db.get_connection = _conn
            try:
                for _ in range(5):
                    # Get a real product id from the index
                    if fresh_engine._items:
                        pid = fresh_engine._items[0]["_id"]
                        fresh_engine.update_products_incremental([pid])
                    time.sleep(0.01)
            finally:
                _db.get_connection = orig

        searchers = [threading.Thread(target=_searcher) for _ in range(5)]
        updater = threading.Thread(target=_updater)

        for s in searchers:
            s.start()
        updater.start()

        updater.join(timeout=5)
        stop.set()
        for s in searchers:
            s.join(timeout=3)

        assert not errors, f"Errors during incremental update+search: {errors}"


# ── Cache concurrency tests ───────────────────────────────────────────────────

class TestCacheConcurrency:
    def test_concurrent_cache_reads_writes(self, search_cache_instance):
        from modules.cache import SearchCache
        errors = []

        def _writer(idx):
            try:
                for i in range(50):
                    key = SearchCache.make_key(f"query_{idx}_{i}")
                    search_cache_instance.set(key, [{"id": i}])
            except Exception as e:
                errors.append(e)

        def _reader(idx):
            try:
                for i in range(50):
                    key = SearchCache.make_key(f"query_{idx}_{i}")
                    search_cache_instance.get(key)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=_writer, args=(t,)) for t in range(5)] +
            [threading.Thread(target=_reader, args=(t,)) for t in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors

    def test_concurrent_cache_clear(self, search_cache_instance):
        from modules.cache import SearchCache
        errors = []

        def _worker():
            try:
                for i in range(20):
                    key = SearchCache.make_key(f"q_{i}")
                    search_cache_instance.set(key, [{"id": i}])
                search_cache_instance.clear()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors


# ── Metrics concurrency ───────────────────────────────────────────────────────

class TestMetricsConcurrency:
    def test_concurrent_metric_recording(self, fresh_metrics):
        errors = []

        def _recorder():
            try:
                for _ in range(200):
                    fresh_metrics.record_search(1.0, result_count=5)
                    fresh_metrics.record_query_cache_hit()
                    fresh_metrics.record_disk_cache_miss()
                    fresh_metrics.record_autocomplete(0.5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_recorder) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        snap = fresh_metrics.snapshot()
        assert snap["searches"]["total"] == 2000
        assert snap["query_cache"]["hits"] == 2000
        assert snap["disk_cache"]["misses"] == 2000
