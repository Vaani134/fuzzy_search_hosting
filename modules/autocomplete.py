"""
modules/autocomplete.py
-----------------------
Fast SQLite-backed autocomplete / search suggestions with an in-memory TTL cache.

Returns up to `limit` suggestions from:
  - product names
  - brand names
  - category names

Strategy: prefix match first (fastest), then LIKE fallback for mid-word.
Results are deduplicated and ranked: exact prefix > contains.

Cache
-----
Results are stored in a bounded OrderedDict with TTL expiry so repeated
keystrokes (e.g. typing "hook" then "hooka") hit memory instead of SQLite.
  _CACHE_TTL  — seconds before an entry expires (default 30)
  _CACHE_MAX  — maximum number of cached query tuples (default 500)
When the cache is full, the least-recently-used entry is evicted (LRU policy).
Call invalidate_autocomplete_cache() after a sync to force fresh results.
"""

import re
import sys
import os
import threading
import time
from collections import OrderedDict
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.database import get_connection


# ── In-memory TTL cache ────────────────────────────────────────────────────────

_cache: OrderedDict = OrderedDict()   # key → (results_list, expiry_monotonic)
_cache_lock = threading.Lock()
_CACHE_TTL: float = 30.0   # cache entry lifetime in seconds

# Max entries — read from config so it can be tuned without code changes.
try:
    from config import MAX_AUTOCOMPLETE_CACHE_ENTRIES as _CACHE_MAX
except (ImportError, AttributeError):
    _CACHE_MAX = 500


def _cache_get(key: tuple) -> Optional[List[Dict]]:
    """Return cached results for *key*, or None on cache miss or expiry."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        results, expiry = entry
        if time.monotonic() > expiry:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return results


def _cache_put(key: tuple, results: List[Dict]) -> None:
    """Insert *results* into the cache; evict LRU entries when over capacity."""
    expiry = time.monotonic() + _CACHE_TTL
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
        _cache[key] = (results, expiry)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def invalidate_autocomplete_cache() -> None:
    """Clear the entire in-memory autocomplete cache (call after a sync)."""
    with _cache_lock:
        _cache.clear()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


# ── Public API ─────────────────────────────────────────────────────────────────

def get_suggestions(query: str, limit: int = 10, source_db_id: Optional[int] = 1) -> List[Dict]:
    """
    Return autocomplete suggestions for `query`.

    Results are served from the in-memory TTL cache when available.
    Cache key: (normalised_query, limit, source_db_id).

    Each suggestion dict:
        text          — display text
        type          — "product" | "brand" | "category"
        id            — record id (for direct navigation on product suggestions)
        source_db_id  — originating database
        priority      — 1=product-prefix, 2=brand, 3=category, 4=product-contains
    """
    query = query.strip()
    if not query or len(query) < 2:
        return []

    cache_key = (_normalize(query), limit, source_db_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    q_like_prefix   = query + "%"
    q_like_contains = "%" + query + "%"

    conn = get_connection()
    results = []
    seen: set = set()

    try:
        # ── 1. Product names — prefix match (highest priority) ────────────────
        product_sql = """
            SELECT id, name, source_db_id, 1 AS priority
            FROM products
            WHERE name LIKE ? AND is_inactive = 0
        """
        product_params = [q_like_prefix]
        if source_db_id is not None:
            product_sql += " AND source_db_id = ?"
            product_params.append(int(source_db_id))
        product_sql += " ORDER BY name LIMIT ?"
        product_params.append(limit)
        rows = conn.execute(product_sql, tuple(product_params)).fetchall()

        for r in rows:
            key = ("product", r["name"].lower())
            if key not in seen:
                seen.add(key)
                results.append({
                    "text": r["name"], "type": "product", "id": r["id"],
                    "source_db_id": r["source_db_id"], "priority": 1
                })

        # ── 2. Brand names — prefix match ─────────────────────────────────────
        brand_sql = """
            SELECT id, name, source_db_id
            FROM brands
            WHERE name LIKE ? AND deleted_at IS NULL
        """
        brand_params = [q_like_prefix]
        if source_db_id is not None:
            brand_sql += " AND source_db_id = ?"
            brand_params.append(int(source_db_id))
        brand_sql += " ORDER BY name LIMIT ?"
        brand_params.append(limit // 2)
        rows = conn.execute(brand_sql, tuple(brand_params)).fetchall()

        for r in rows:
            key = ("brand", r["name"].lower())
            if key not in seen:
                seen.add(key)
                results.append({
                    "text": r["name"], "type": "brand", "id": r["id"],
                    "source_db_id": r["source_db_id"], "priority": 2
                })

        # ── 3. Category names — prefix match ──────────────────────────────────
        category_sql = """
            SELECT id, name, source_db_id
            FROM categories
            WHERE name LIKE ? AND deleted_at IS NULL
        """
        category_params = [q_like_prefix]
        if source_db_id is not None:
            category_sql += " AND source_db_id = ?"
            category_params.append(int(source_db_id))
        category_sql += " ORDER BY name LIMIT ?"
        category_params.append(limit // 2)
        rows = conn.execute(category_sql, tuple(category_params)).fetchall()

        for r in rows:
            key = ("category", r["name"].lower())
            if key not in seen:
                seen.add(key)
                results.append({
                    "text": r["name"], "type": "category", "id": r["id"],
                    "source_db_id": r["source_db_id"], "priority": 3
                })

        # ── 4. Fill remaining slots with contains-match on products ───────────
        if len(results) < limit:
            remaining = limit - len(results)
            fill_sql = """
                SELECT id, name, source_db_id
                FROM products
                WHERE name LIKE ? AND name NOT LIKE ? AND is_inactive = 0
            """
            fill_params = [q_like_contains, q_like_prefix]
            if source_db_id is not None:
                fill_sql += " AND source_db_id = ?"
                fill_params.append(int(source_db_id))
            fill_sql += " ORDER BY name LIMIT ?"
            fill_params.append(remaining)
            rows = conn.execute(fill_sql, tuple(fill_params)).fetchall()

            for r in rows:
                key = ("product", r["name"].lower())
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "text": r["name"], "type": "product", "id": r["id"],
                        "source_db_id": r["source_db_id"], "priority": 4
                    })

    finally:
        conn.close()

    results.sort(key=lambda x: (x["priority"], x["text"].lower()))
    final = results[:limit]
    _cache_put(cache_key, final)
    return final
