"""
modules/autocomplete.py
-----------------------
Fast SQLite-backed autocomplete / search suggestions.

Cache backend
-------------
Redis is used when REDIS_URL is configured and reachable.
Falls back to in-memory OrderedDict (LRU + TTL) when Redis is unavailable.
Both backends expose the same _cache_get / _cache_put / invalidate interface
so the rest of the module is unaffected by which backend is active.

Redis key format:
    {REDIS_KEY_PREFIX}ac:{source_db_id}:{limit}:{normalized_query}
    e.g.  fzsearch:ac:1:10:hook
          fzsearch:ac:global:10:hook   (source_db_id=None)

TTL: 30 seconds (SETEX — Redis handles expiry natively).

In-memory fallback:
    OrderedDict with monotonic-clock TTL and LRU eviction.
    _CACHE_MAX entries max (default 500).
"""

import json
import re
import sys
import os
import threading
import time
from collections import OrderedDict
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.database import get_connection

# ── Cache TTL and size limits ──────────────────────────────────────────────────
_CACHE_TTL: int = 30   # seconds

try:
    from config import MAX_AUTOCOMPLETE_CACHE_ENTRIES as _CACHE_MAX
except (ImportError, AttributeError):
    _CACHE_MAX = 500

# ── Redis setup ────────────────────────────────────────────────────────────────

try:
    import redis as _redis_lib
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False


def _init_redis():
    """
    Try to connect to Redis using REDIS_URL from config/env.
    Returns a connected redis.Redis client, or None on any failure.
    """
    if not _REDIS_AVAILABLE:
        print("[Autocomplete] redis-py not installed — using in-memory cache.")
        return None
    try:
        from config import REDIS_URL
    except ImportError:
        REDIS_URL = os.getenv("REDIS_URL", "")

    if not REDIS_URL or not REDIS_URL.strip():
        print("[Autocomplete] REDIS_URL not set — using in-memory cache.")
        return None

    try:
        client = _redis_lib.from_url(
            REDIS_URL.strip(),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        print(f"[Autocomplete] Redis backend connected: {REDIS_URL.strip()}")
        return client
    except Exception as exc:
        print(f"[Autocomplete] Redis unavailable ({exc}) — using in-memory cache.")
        return None


_redis_client = _init_redis()


def _get_prefix() -> str:
    try:
        from config import REDIS_KEY_PREFIX
        return REDIS_KEY_PREFIX
    except (ImportError, AttributeError):
        return os.getenv("REDIS_KEY_PREFIX", "fzsearch:")


def _make_redis_key(key: tuple) -> str:
    """
    Build a namespaced Redis key from (normalized_query, limit, source_db_id).
    Example: fzsearch:ac:1:10:hook
    """
    normalized_query, limit, source_db_id = key
    db_part = str(source_db_id) if source_db_id is not None else "global"
    return f"{_get_prefix()}ac:{db_part}:{limit}:{normalized_query}"


# ── In-memory fallback cache ───────────────────────────────────────────────────
_mem_cache: OrderedDict = OrderedDict()
_mem_lock = threading.Lock()


# ── Unified cache interface ────────────────────────────────────────────────────

def _cache_get(key: tuple) -> Optional[List[Dict]]:
    """Return cached results for *key*, or None on miss / expiry."""
    # ── Redis path ─────────────────────────────────────────────────────────────
    if _redis_client is not None:
        try:
            raw = _redis_client.get(_make_redis_key(key))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            print(f"[Autocomplete] Redis GET error: {exc}")
            return None

    # ── In-memory fallback ─────────────────────────────────────────────────────
    with _mem_lock:
        entry = _mem_cache.get(key)
        if entry is None:
            return None
        results, expiry = entry
        if time.monotonic() > expiry:
            del _mem_cache[key]
            return None
        _mem_cache.move_to_end(key)
        return results


def _cache_put(key: tuple, results: List[Dict]) -> None:
    """Store *results* under *key* with TTL."""
    # ── Redis path ─────────────────────────────────────────────────────────────
    if _redis_client is not None:
        try:
            _redis_client.setex(
                _make_redis_key(key),
                _CACHE_TTL,
                json.dumps(results, ensure_ascii=False),
            )
        except Exception as exc:
            print(f"[Autocomplete] Redis SET error: {exc}")
        return

    # ── In-memory fallback ─────────────────────────────────────────────────────
    expiry = time.monotonic() + _CACHE_TTL
    with _mem_lock:
        if key in _mem_cache:
            _mem_cache.move_to_end(key)
        _mem_cache[key] = (results, expiry)
        while len(_mem_cache) > _CACHE_MAX:
            _mem_cache.popitem(last=False)


def invalidate_autocomplete_cache() -> None:
    """
    Clear all autocomplete cache entries.
    Redis: deletes all keys matching the ac: prefix pattern.
    In-memory: clears the OrderedDict.
    Called after every successful sync.
    """
    if _redis_client is not None:
        try:
            pattern = f"{_get_prefix()}ac:*"
            keys = _redis_client.keys(pattern)
            if keys:
                _redis_client.delete(*keys)
                print(f"[Autocomplete] Redis: cleared {len(keys)} autocomplete key(s).")
        except Exception as exc:
            print(f"[Autocomplete] Redis CLEAR error: {exc}")
        return

    with _mem_lock:
        _mem_cache.clear()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


# ── Public API ─────────────────────────────────────────────────────────────────

def get_suggestions(query: str, limit: int = 10, source_db_id: Optional[int] = 1) -> List[Dict]:
    """
    Return autocomplete suggestions for `query`.

    Results are served from the cache (Redis or in-memory) when available.
    Cache key: (normalised_query, limit, source_db_id).

    Each suggestion dict:
        text          — display text
        type          — "product" | "brand" | "category"
        id            — record id
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
