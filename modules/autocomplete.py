"""
modules/autocomplete.py
-----------------------
Fast SQLite-backed autocomplete / search suggestions.

Returns up to `limit` suggestions from:
  - product names
  - brand names
  - category names

Strategy: prefix match first (fastest), then LIKE fallback for mid-word.
Results are deduplicated and ranked: exact prefix > contains.
"""

import re
import sys
import os
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.database import get_connection


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


def get_suggestions(query: str, limit: int = 10, source_db_id: Optional[int] = 1) -> List[Dict]:
    """
    Return autocomplete suggestions for `query`.

    Each suggestion dict:
        text   — display text
        type   — "product" | "brand" | "category"
        id     — record id (for direct navigation on product suggestions)
    """
    query = query.strip()
    if not query or len(query) < 2:
        return []

    q_like_prefix  = query + "%"          # starts with
    q_like_contains = "%" + query + "%"   # contains anywhere

    conn = get_connection()
    results = []
    seen = set()

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

    # Sort: priority asc, then alphabetical
    results.sort(key=lambda x: (x["priority"], x["text"].lower()))
    return results[:limit]
