"""
modules/sync_live_logs.py
-------------------------
In-memory ring buffer for real-time sync log streaming.

One deque per database_id, capped at 1000 entries.  Thread-safe.
Entries are plain dicts so they can be JSON-serialised directly by Flask.

Log levels (mirrors common logging): DEBUG, INFO, WARNING, ERROR
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_lock:  threading.Lock              = threading.Lock()
_store: Dict[int, deque]            = {}   # db_id → deque of log dicts
_MAX_ENTRIES                        = 1000


def _get_deque(db_id: int) -> deque:
    """Return (creating if needed) the deque for *db_id*.  Caller holds lock."""
    if db_id not in _store:
        _store[db_id] = deque(maxlen=_MAX_ENTRIES)
    return _store[db_id]


def append_log(db_id: int, level: str, message: str, **extra: Any) -> None:
    """
    Append one log entry for *db_id*.

    Parameters
    ----------
    db_id   : connected_databases primary key
    level   : "DEBUG" | "INFO" | "WARNING" | "ERROR"
    message : human-readable message string
    **extra : optional key-value metadata merged into the entry dict
    """
    entry: Dict[str, Any] = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "level":   level.upper(),
        "message": message,
        **extra,
    }
    with _lock:
        _get_deque(db_id).append(entry)


def get_logs(db_id: int, since_ts: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return log entries for *db_id*, optionally filtered to entries after *since_ts*.

    Parameters
    ----------
    db_id    : connected_databases primary key
    since_ts : ISO-8601 timestamp string; only entries with ts > since_ts are
               returned.  Pass None (or omit) to get all buffered entries.

    Returns
    -------
    List of log entry dicts, oldest first.
    """
    with _lock:
        entries = list(_get_deque(db_id))

    if not since_ts:
        return entries

    # Filter — simple lexicographic comparison works for ISO-8601
    return [e for e in entries if e["ts"] > since_ts]


def clear_logs(db_id: int) -> None:
    """Discard all buffered log entries for *db_id*."""
    with _lock:
        if db_id in _store:
            _store[db_id].clear()


def get_log_count(db_id: int) -> int:
    """Return how many entries are currently buffered for *db_id*."""
    with _lock:
        return len(_store.get(db_id, []))
