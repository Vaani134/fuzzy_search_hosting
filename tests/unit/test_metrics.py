"""
tests/unit/test_metrics.py
--------------------------
Unit tests for modules/metrics.py — SearchMetrics class.
"""

import threading
import time

import pytest


class TestHitRate:
    def test_zero_total_returns_zero(self):
        from modules.metrics import _hit_rate
        assert _hit_rate(0, 0) == 0.0

    def test_all_hits(self):
        from modules.metrics import _hit_rate
        assert _hit_rate(100, 100) == 100.0

    def test_half_hits(self):
        from modules.metrics import _hit_rate
        assert _hit_rate(50, 100) == 50.0

    def test_rounding(self):
        from modules.metrics import _hit_rate
        assert _hit_rate(1, 3) == 33.3


class TestPercentile:
    def test_empty_returns_none(self):
        from modules.metrics import _percentile
        assert _percentile([], 50) is None

    def test_single_element(self):
        from modules.metrics import _percentile
        assert _percentile([42.0], 50) == 42.0

    def test_p50_even_list(self):
        from modules.metrics import _percentile
        data = sorted([10.0, 20.0, 30.0, 40.0])
        result = _percentile(data, 50)
        assert result is not None
        assert 20.0 <= result <= 30.0

    def test_p99_large_list(self):
        from modules.metrics import _percentile
        data = sorted(float(i) for i in range(1, 1001))
        p99 = _percentile(data, 99)
        assert p99 is not None
        assert p99 >= 990.0

    def test_p0_returns_min(self):
        from modules.metrics import _percentile
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(data, 0) == 1.0

    def test_p100_returns_max(self):
        from modules.metrics import _percentile
        data = [1.0, 5.0, 10.0]
        assert _percentile(data, 100) == 10.0


class TestSearchMetrics:
    def test_initial_state(self, fresh_metrics):
        snap = fresh_metrics.snapshot()
        assert snap["disk_cache"]["hits"] == 0
        assert snap["disk_cache"]["misses"] == 0
        assert snap["query_cache"]["hits"] == 0
        assert snap["query_cache"]["misses"] == 0
        assert snap["searches"]["total"] == 0
        assert snap["rebuilds"]["count"] == 0

    def test_record_disk_cache_hit(self, fresh_metrics):
        fresh_metrics.record_disk_cache_hit()
        fresh_metrics.record_disk_cache_hit()
        snap = fresh_metrics.snapshot()
        assert snap["disk_cache"]["hits"] == 2
        assert snap["disk_cache"]["misses"] == 0
        assert snap["disk_cache"]["hit_rate_pct"] == 100.0

    def test_record_disk_cache_miss(self, fresh_metrics):
        fresh_metrics.record_disk_cache_miss()
        snap = fresh_metrics.snapshot()
        assert snap["disk_cache"]["misses"] == 1
        assert snap["disk_cache"]["hit_rate_pct"] == 0.0

    def test_disk_cache_hit_rate_mixed(self, fresh_metrics):
        fresh_metrics.record_disk_cache_hit()
        fresh_metrics.record_disk_cache_miss()
        snap = fresh_metrics.snapshot()
        assert snap["disk_cache"]["hit_rate_pct"] == 50.0

    def test_record_query_cache_hit(self, fresh_metrics):
        fresh_metrics.record_query_cache_hit()
        fresh_metrics.record_query_cache_hit()
        fresh_metrics.record_query_cache_hit()
        snap = fresh_metrics.snapshot()
        assert snap["query_cache"]["hits"] == 3

    def test_record_query_cache_miss(self, fresh_metrics):
        fresh_metrics.record_query_cache_miss()
        snap = fresh_metrics.snapshot()
        assert snap["query_cache"]["misses"] == 1

    def test_record_search_updates_total(self, fresh_metrics):
        fresh_metrics.record_search(5.0, result_count=10)
        snap = fresh_metrics.snapshot()
        assert snap["searches"]["total"] == 1
        assert snap["searches"]["zero_results"] == 0

    def test_record_search_zero_results(self, fresh_metrics):
        fresh_metrics.record_search(5.0, result_count=0)
        snap = fresh_metrics.snapshot()
        assert snap["searches"]["zero_results"] == 1

    def test_search_latency_percentiles(self, fresh_metrics):
        for ms in [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]:
            fresh_metrics.record_search(ms, result_count=5)
        snap = fresh_metrics.snapshot()
        s = snap["searches"]
        assert s["latency_p50_ms"] is not None
        assert s["latency_p95_ms"] is not None
        assert s["latency_p99_ms"] is not None
        assert s["latency_p50_ms"] <= s["latency_p95_ms"] <= s["latency_p99_ms"]

    def test_record_autocomplete_latency(self, fresh_metrics):
        for ms in [1.0, 2.0, 3.0]:
            fresh_metrics.record_autocomplete(ms)
        snap = fresh_metrics.snapshot()
        ac = snap["autocomplete"]
        assert ac["latency_p50_ms"] is not None
        assert ac["latency_samples"] == 3

    def test_record_rebuild(self, fresh_metrics):
        fresh_metrics.record_rebuild(4500.0)
        snap = fresh_metrics.snapshot()
        assert snap["rebuilds"]["count"] == 1
        assert snap["rebuilds"]["last_ms"] == 4500.0

    def test_multiple_rebuilds_percentiles(self, fresh_metrics):
        for ms in [3000.0, 4000.0, 5000.0, 6000.0]:
            fresh_metrics.record_rebuild(ms)
        snap = fresh_metrics.snapshot()
        rb = snap["rebuilds"]
        assert rb["count"] == 4
        assert rb["duration_p50_ms"] is not None
        assert rb["duration_p95_ms"] is not None

    def test_uptime_increases(self, fresh_metrics):
        t0 = fresh_metrics.snapshot()["uptime_seconds"]
        time.sleep(0.05)
        t1 = fresh_metrics.snapshot()["uptime_seconds"]
        assert t1 > t0

    def test_rolling_window_eviction(self):
        from modules.metrics import SearchMetrics
        m = SearchMetrics(latency_window=5)
        for i in range(10):
            m.record_search(float(i), result_count=1)
        snap = m.snapshot()
        assert snap["searches"]["latency_samples"] == 5

    def test_reset_clears_all(self, fresh_metrics):
        fresh_metrics.record_disk_cache_hit()
        fresh_metrics.record_query_cache_hit()
        fresh_metrics.record_search(10.0, result_count=5)
        fresh_metrics.record_rebuild(3000.0)
        fresh_metrics.reset()
        snap = fresh_metrics.snapshot()
        assert snap["disk_cache"]["hits"] == 0
        assert snap["query_cache"]["hits"] == 0
        assert snap["searches"]["total"] == 0
        assert snap["rebuilds"]["count"] == 0
        assert snap["searches"]["latency_samples"] == 0

    def test_thread_safety(self, fresh_metrics):
        errors = []

        def _worker():
            try:
                for _ in range(100):
                    fresh_metrics.record_search(1.0, result_count=1)
                    fresh_metrics.record_query_cache_hit()
                    fresh_metrics.record_disk_cache_miss()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        snap = fresh_metrics.snapshot()
        assert snap["searches"]["total"] == 1000
        assert snap["query_cache"]["hits"] == 1000
        assert snap["disk_cache"]["misses"] == 1000

    def test_snapshot_is_json_serialisable(self, fresh_metrics):
        import json
        fresh_metrics.record_search(5.0, result_count=3)
        fresh_metrics.record_rebuild(2000.0)
        snap = fresh_metrics.snapshot()
        dumped = json.dumps(snap)
        assert isinstance(dumped, str)
        loaded = json.loads(dumped)
        assert loaded["searches"]["total"] == 1
