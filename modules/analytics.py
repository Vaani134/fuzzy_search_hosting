"""
modules/analytics.py
--------------------
Search history & analytics.

Responsibilities:
  - Log every search query with its result count and timestamp.
  - Expose recent searches (GET /api/search/history).
  - Expose top / most-frequent queries (GET /api/search/top).

The search_history table is created by db/schema.sql on first run.
"""

import sys
import os
from datetime import datetime, timezone
from typing import List, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.database import get_connection


def log_search(query: str, result_count: int) -> None:
    """
    Persist a search event to the search_history table.
    Called after every successful search so we can analyse usage patterns.

    Parameters
    ----------
    query        : the raw (pre-synonym-expansion) query string
    result_count : number of results returned to the user
    """
    if not query or not query.strip():
        return

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO search_history (query, result_count, timestamp)
            VALUES (?, ?, ?)
            """,
            (
                query.strip().lower(),
                result_count,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except Exception as exc:
        # Analytics must never break the main search flow
        print(f"[Analytics] Failed to log search: {exc}")
    finally:
        conn.close()


def get_recent_searches(limit: int = 10) -> List[Dict]:
    """
    Return the most recent `limit` search events, newest first.

    Returns
    -------
    list of dicts: {id, query, result_count, timestamp}
    """
    limit = min(max(1, limit), 100)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, query, result_count, timestamp
            FROM search_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_top_queries(limit: int = 10) -> List[Dict]:
    """
    Return the `limit` most-frequently searched queries.

    Returns
    -------
    list of dicts: {query, search_count, avg_results}
    Sorted by search_count descending.
    """
    limit = min(max(1, limit), 100)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                query,
                COUNT(*)          AS search_count,
                AVG(result_count) AS avg_results
            FROM search_history
            GROUP BY query
            ORDER BY search_count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "query":        r["query"],
                "search_count": r["search_count"],
                "avg_results":  round(r["avg_results"] or 0, 1),
            }
            for r in rows
        ]
    finally:
        conn.close()
