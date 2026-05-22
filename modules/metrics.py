"""
modules/metrics.py
------------------
Thread-safe, zero-dependency search metrics collector.

Tracks six signal categories in a single process-global singleton:

  1. Engine disk cache  — hits/misses loading engine.pkl.gz on startup
  2. Query result cache — hits/misses on Redis / in-memory search result cache
  3. Search latency     — rolling P50 / P95 / P99 window (last N executions)
  4. Autocomplete       — rolling latency percentiles
  5. Rebuild            — duration and count per rebuild call
  6. Uptime             — wall-clock seconds since module import

Design constraints
------------------
- Zero external dependencies (standard library only).
- All counter increments are O(1).
- snapshot() is O(N log N) for one percentile sort; N ≤ latency_window.
- Rolling windows use deque(maxlen) — O(1) append, bounded memory.
- A single threading.Lock protects all mutations and window appends.
  snapshot() acquires the lock once, copies mutable state, then releases
  before sorting so the critical section stays as short as possible.

Public API
----------
  search_metrics.record_disk_cache_hit()
  search_metrics.record_disk_cache_miss()
  search_metrics.record_query_cache_hit()
  search_metrics.record_query_cache_miss()
  search_metrics.record_search(latency_ms, result_count)
  search_metrics.record_autocomplete(latency_ms)
  search_metrics.record_rebuild(duration_ms)
  search_metrics.snapshot()  → dict

The module-level ``search_metrics`` singleton is imported by:
  modules/fuzzy_search.py   — disk cache and rebuild recording
  routes/search_routes.py   — query cache, search latency, autocomplete latency
"""

import threading
import time
from collections import deque
from typing import Dict, List, Optional


# ── Helpers ────────────────────────────────────────────────────────────────────

def _hit_rate(hits: int, total: int) -> float:
    """Compute hit rate as a percentage rounded to one decimal place."""
    if total == 0:
        return 0.0
    return round(hits / total * 100.0, 1)


def _percentile(data: List[float], p: float) -> Optional[float]:
    """
    Return the p-th percentile of *data* (already sorted externally).

    Parameters
    ----------
    data : list of float  — must be PRE-SORTED ascending
    p    : float          — percentile in [0, 100]
    """
    if not data:
        return None
    idx = max(0, min(int(len(data) * p / 100), len(data) - 1))
    return round(data[idx], 2)


def _latency_stats(data: List[float], prefix: str) -> Dict:
    """
    Return P50 / P95 / P99 / avg for a latency list.

    *data* is copied and sorted inside this function; the caller passes
    the raw (unsorted) snapshot from the deque.
    """
    if not data:
        return {
            f"{prefix}_p50_ms":  None,
            f"{prefix}_p95_ms":  None,
            f"{prefix}_p99_ms":  None,
            f"{prefix}_avg_ms":  None,
            f"{prefix}_samples": 0,
        }
    s = sorted(data)
    return {
        f"{prefix}_p50_ms":  _percentile(s, 50),
        f"{prefix}_p95_ms":  _percentile(s, 95),
        f"{prefix}_p99_ms":  _percentile(s, 99),
        f"{prefix}_avg_ms":  round(sum(s) / len(s), 2),
        f"{prefix}_samples": len(s),
    }


# ── SearchMetrics class ────────────────────────────────────────────────────────

class SearchMetrics:
    """
    Thread-safe search performance metrics collector.

    Parameters
    ----------
    latency_window : int
        Maximum number of recent latency samples retained in each rolling
        window.  Older samples are evicted automatically (deque(maxlen=N)).
        Default: 1000.
    """

    def __init__(self, latency_window: int = 1000) -> None:
        self._lock = threading.Lock()

        # ── Engine disk cache ──────────────────────────────────────────────────
        self._disk_hits:   int = 0
        self._disk_misses: int = 0

        # ── Query result cache (Redis / in-memory) ─────────────────────────────
        self._query_hits:   int = 0
        self._query_misses: int = 0

        # ── Search execution ───────────────────────────────────────────────────
        self._total_searches:       int = 0
        self._zero_result_searches: int = 0
        self._search_latencies: deque = deque(maxlen=latency_window)

        # ── Autocomplete ───────────────────────────────────────────────────────
        self._autocomplete_latencies: deque = deque(maxlen=latency_window)

        # ── Index rebuild ──────────────────────────────────────────────────────
        self._rebuild_count:    int = 0
        self._last_rebuild_ms:  Optional[float] = None
        self._rebuild_durations: deque = deque(maxlen=100)

        # ── Process start time ─────────────────────────────────────────────────
        self._started_at: float = time.time()

    # ── Recorders — all O(1), never raise ──────────────────────────────────────

    def record_disk_cache_hit(self) -> None:
        with self._lock:
            self._disk_hits += 1

    def record_disk_cache_miss(self) -> None:
        with self._lock:
            self._disk_misses += 1

    def record_query_cache_hit(self) -> None:
        with self._lock:
            self._query_hits += 1

    def record_query_cache_miss(self) -> None:
        with self._lock:
            self._query_misses += 1

    def record_search(self, latency_ms: float, result_count: int = 0) -> None:
        with self._lock:
            self._total_searches += 1
            if result_count == 0:
                self._zero_result_searches += 1
            self._search_latencies.append(latency_ms)

    def record_autocomplete(self, latency_ms: float) -> None:
        with self._lock:
            self._autocomplete_latencies.append(latency_ms)

    def record_rebuild(self, duration_ms: float) -> None:
        with self._lock:
            self._rebuild_count      += 1
            self._last_rebuild_ms     = duration_ms
            self._rebuild_durations.append(duration_ms)

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict:
        """
        Return a JSON-serialisable snapshot of all current metrics.

        Acquires the lock once to copy mutable state, releases it, then
        computes percentiles outside the lock (avoids holding it during
        the O(N log N) sort).

        Returns
        -------
        dict with keys:
          uptime_seconds  — process uptime
          disk_cache      — engine pickle cache hit/miss/rate
          query_cache     — search result cache hit/miss/rate
          searches        — count, zero-result count, latency percentiles
          autocomplete    — latency percentiles
          rebuilds        — count, last duration, duration percentiles
        """
        with self._lock:
            disk_hits    = self._disk_hits
            disk_misses  = self._disk_misses
            q_hits       = self._query_hits
            q_misses     = self._query_misses
            s_total      = self._total_searches
            s_zero       = self._zero_result_searches
            s_lats       = list(self._search_latencies)
            ac_lats      = list(self._autocomplete_latencies)
            rb_count     = self._rebuild_count
            rb_last      = self._last_rebuild_ms
            rb_durs      = list(self._rebuild_durations)
            started_at   = self._started_at

        total_disk  = disk_hits  + disk_misses
        total_query = q_hits     + q_misses

        return {
            "uptime_seconds": round(time.time() - started_at, 1),
            "disk_cache": {
                "hits":         disk_hits,
                "misses":       disk_misses,
                "total":        total_disk,
                "hit_rate_pct": _hit_rate(disk_hits, total_disk),
            },
            "query_cache": {
                "hits":         q_hits,
                "misses":       q_misses,
                "total":        total_query,
                "hit_rate_pct": _hit_rate(q_hits, total_query),
            },
            "searches": {
                "total":        s_total,
                "zero_results": s_zero,
                **_latency_stats(s_lats, "latency"),
            },
            "autocomplete": {
                **_latency_stats(ac_lats, "latency"),
            },
            "rebuilds": {
                "count":   rb_count,
                "last_ms": round(rb_last, 1) if rb_last is not None else None,
                **_latency_stats(rb_durs, "duration"),
            },
        }

    def reset(self) -> None:
        """
        Reset all counters and rolling windows to zero.
        Useful for testing.  Not called during normal operation.
        """
        with self._lock:
            self._disk_hits              = 0
            self._disk_misses            = 0
            self._query_hits             = 0
            self._query_misses           = 0
            self._total_searches         = 0
            self._zero_result_searches   = 0
            self._search_latencies.clear()
            self._autocomplete_latencies.clear()
            self._rebuild_count          = 0
            self._last_rebuild_ms        = None
            self._rebuild_durations.clear()
            self._started_at             = time.time()


# ── Module-level singleton ─────────────────────────────────────────────────────
#
# Imported by fuzzy_search.py, search_routes.py, and potentially other modules.
# All recording calls are O(1) and never raise; latency tracking is
# bounded-memory via deque(maxlen=1000).
search_metrics = SearchMetrics(latency_window=1000)
