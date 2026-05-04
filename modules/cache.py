"""
modules/cache.py
----------------
In-memory TTL cache for search results.

Design
------
  - Pure Python dict — no external dependencies.
  - Cache key = deterministic string built from query + filters + page.
  - Each entry stores {"timestamp": float, "data": any}.
  - Entries older than TTL_SECONDS are treated as stale and recomputed.
  - Thread-safe via a threading.Lock.
  - Periodic cleanup removes expired entries to prevent unbounded growth.

Usage
-----
    from modules.cache import SearchCache

    cache = SearchCache(ttl=60)          # 60-second TTL

    key  = cache.make_key("hookah", {"category": "Hookahs"}, page=1)
    data = cache.get(key)                # None if miss / expired
    if data is None:
        data = expensive_search(...)
        cache.set(key, data)
"""

import hashlib
import json
import threading
import time
from typing import Any, Optional


class SearchCache:
    """
    Thread-safe in-memory cache with TTL expiry.

    Parameters
    ----------
    ttl : int
        Time-to-live in seconds.  Default: 60.
    max_size : int
        Maximum number of entries before the oldest are evicted.
        Default: 500.
    """

    def __init__(self, ttl: int = 60, max_size: int = 500):
        self._ttl      = ttl
        self._max_size = max_size
        self._store: dict[str, dict] = {}
        self._lock  = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    @staticmethod
    def make_key(
        query: str,
        filters: Optional[dict] = None,
        page: int = 1,
        limit: int = 20,
        sort: str = "score",
    ) -> str:
        """
        Build a deterministic cache key from search parameters.

        The key is a short SHA-256 hex digest so it is safe to use as a
        dict key regardless of query length or special characters.
        """
        payload = {
            "q":       query.strip().lower(),
            "filters": filters or {},
            "page":    page,
            "limit":   limit,
            "sort":    sort,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, key: str) -> Optional[Any]:
        """
        Return cached data for `key`, or None if missing / expired.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.time() - entry["timestamp"] > self._ttl:
                # Expired — remove and signal a miss
                del self._store[key]
                return None
            return entry["data"]

    def set(self, key: str, data: Any) -> None:
        """
        Store `data` under `key` with the current timestamp.
        Evicts the oldest entry if max_size is reached.
        """
        with self._lock:
            # Evict oldest entry if at capacity
            if len(self._store) >= self._max_size and key not in self._store:
                oldest_key = min(self._store, key=lambda k: self._store[k]["timestamp"])
                del self._store[oldest_key]

            self._store[key] = {"timestamp": time.time(), "data": data}

    def invalidate(self, key: str) -> None:
        """Remove a specific key from the cache."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> int:
        """
        Remove all entries.  Returns the number of entries cleared.
        """
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    def purge_expired(self) -> int:
        """
        Remove all expired entries.  Returns the number removed.
        Called automatically by the background thread.
        """
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._store.items()
                       if now - v["timestamp"] > self._ttl]
            for k in expired:
                del self._store[k]
            return len(expired)

    def stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            now   = time.time()
            total = len(self._store)
            live  = sum(1 for v in self._store.values()
                        if now - v["timestamp"] <= self._ttl)
            return {
                "total_entries":   total,
                "live_entries":    live,
                "expired_entries": total - live,
                "ttl_seconds":     self._ttl,
                "max_size":        self._max_size,
            }

    def start_cleanup_thread(self, interval: int = 120) -> None:
        """
        Start a background daemon thread that calls purge_expired()
        every `interval` seconds to prevent unbounded memory growth.
        """
        def _worker():
            while True:
                time.sleep(interval)
                removed = self.purge_expired()
                if removed:
                    print(f"[Cache] Purged {removed} expired entries.")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()


# ── Module-level singleton ─────────────────────────────────────────────────────
# Shared across the entire Flask app.  TTL = 60 s, max 500 entries.
search_cache = SearchCache(ttl=60, max_size=500)
search_cache.start_cleanup_thread(interval=120)
