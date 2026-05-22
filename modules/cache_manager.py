"""
modules/cache_manager.py
------------------------
Persistent disk cache for FuzzySearchEngine indexes.

Architecture
------------
Each FuzzySearchEngine instance (one per source database, plus one global)
is backed by a CacheManager slot on disk.  The slot serialises the engine's
expensive in-memory structures — product dicts, raw strings, normalized
strings — so application restarts load in <1s instead of rebuilding from
SQLite.

Cache directory layout
----------------------
    cache/
        global/
            engine.pkl.gz   ← gzip-compressed pickle of engine data
            metadata.json   ← validation metadata (version, count, checksum)
        db_1/
            engine.pkl.gz
            metadata.json
        db_2/
            engine.pkl.gz
            metadata.json

Validation before loading
-------------------------
Before any cached data is trusted the manager validates:
  1. metadata.json is readable and well-formed
  2. cache_version matches config.CACHE_VERSION
  3. schema_version matches internal _SCHEMA_VERSION
  4. max_age: built_at + CACHE_MAX_AGE > now  (if CACHE_MAX_AGE > 0)
  5. product_count matches current SQLite COUNT(*) — fast indexed query
  6. engine file exists on disk
  7. SHA-256 checksum of the engine file matches metadata checksum

If any check fails the cache is invalidated and the caller must rebuild.

Atomic writes
-------------
Saves use a write-to-.tmp / os.replace() pattern.  os.replace() is atomic
on both POSIX and Windows (MoveFileExW / MOVEFILE_REPLACE_EXISTING), so a
reader never sees a partial file regardless of when it opens the path.

Thread safety
-------------
A per-slot threading.Lock serialises all reads and writes within the same
process.  Concurrent requests share the in-memory engine singleton and
never contend on the disk cache directly.

Public API
----------
  get_cache_manager(source_db_id)         → CacheManager
  CacheManager.load_engine_data()         → dict | None
  CacheManager.save_engine_data(data)     → bool
  CacheManager.invalidate()               → None
  CacheManager.is_valid()                 → bool
  CacheManager.get_stats()                → dict
  cleanup_stale_tmp_files(cache_dir)      → int  (files removed)
"""

import gzip
import hashlib
import json
import logging
import os
import pickle
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Internal schema version.  Bump this constant when the structure of cached
# data changes (e.g. new fields added to product dicts, normalization logic
# changes).  This forces a rebuild across all slots automatically.
_SCHEMA_VERSION = "1"

# ── Per-slot lock registry ─────────────────────────────────────────────────────
# One threading.Lock per slot key ("global" or "db_<id>").
# Guards all disk reads and writes for that slot within this process.

_lock_registry: Dict[str, threading.Lock] = {}
_registry_mutex = threading.Lock()


def _get_slot_lock(slot_key: str) -> threading.Lock:
    with _registry_mutex:
        if slot_key not in _lock_registry:
            _lock_registry[slot_key] = threading.Lock()
        return _lock_registry[slot_key]


# ── Utility helpers ────────────────────────────────────────────────────────────

def _file_sha256(path: str) -> str:
    """Compute SHA-256 hex digest of a file using streaming reads."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_product_count(source_db_id: Optional[int]) -> int:
    """
    Fast SQLite COUNT of active products for the given source.
    None → count across all sources (used by the global engine).
    """
    from db.database import get_connection
    conn = get_connection()
    try:
        if source_db_id is None:
            return conn.execute(
                "SELECT COUNT(*) FROM products WHERE is_inactive = 0"
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM products WHERE is_inactive = 0 AND source_db_id = ?",
            (source_db_id,),
        ).fetchone()[0]
    finally:
        conn.close()


# ── CacheManager ───────────────────────────────────────────────────────────────

class CacheManager:
    """
    Manages the persistent disk cache for one FuzzySearchEngine slot.

    Parameters
    ----------
    source_db_id  : int or None
        Identifies the engine.  None = global engine (all sources).
    cache_dir     : str
        Root cache directory (e.g. ``/app/cache``).
    cache_version : str
        Must match ``config.CACHE_VERSION``; increment to force invalidation.
    compression   : bool
        True → gzip(pickle) for smaller files; False → plain pickle.
    max_age       : int
        Maximum cache age in seconds.  0 disables age checking.
    """

    def __init__(
        self,
        source_db_id: Optional[int],
        cache_dir: str,
        cache_version: str,
        compression: bool,
        max_age: int,
    ) -> None:
        self.source_db_id   = source_db_id
        self.compression    = compression
        self.max_age        = max_age
        self._cache_version = cache_version

        slot_name           = "global" if source_db_id is None else f"db_{source_db_id}"
        self.slot_key       = slot_name
        self.cache_dir      = os.path.join(cache_dir, slot_name)
        ext                 = "pkl.gz" if compression else "pkl"
        self.engine_path    = os.path.join(self.cache_dir, f"engine.{ext}")
        self.metadata_path  = os.path.join(self.cache_dir, "metadata.json")
        self._lock          = _get_slot_lock(slot_name)

    # ── Metadata ───────────────────────────────────────────────────────────────

    def load_metadata(self) -> Optional[Dict]:
        """Read and return metadata.json for this slot, or None on any error."""
        try:
            with open(self.metadata_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    # ── Validation ─────────────────────────────────────────────────────────────

    def is_valid(self) -> bool:
        """
        Return True only when the cache passes all seven validation gates:

        1. metadata.json readable
        2. cache_version matches config.CACHE_VERSION
        3. schema_version matches internal _SCHEMA_VERSION
        4. cache age ≤ CACHE_MAX_AGE  (skipped when max_age = 0)
        5. product count in SQLite matches cached product_count
        6. engine file exists on disk
        7. SHA-256 checksum of engine file matches stored checksum

        Any failure is logged at INFO/WARNING level; returns False immediately.
        """
        meta = self.load_metadata()
        if not meta:
            logger.debug("[Cache] %s: no metadata", self.slot_key)
            return False

        # Gate 2 — cache_version
        if meta.get("cache_version") != self._cache_version:
            logger.info(
                "[Cache] %s: cache_version mismatch (stored=%s, expected=%s)",
                self.slot_key, meta.get("cache_version"), self._cache_version,
            )
            return False

        # Gate 3 — schema_version
        if meta.get("schema_version") != _SCHEMA_VERSION:
            logger.info(
                "[Cache] %s: schema_version mismatch (stored=%s, expected=%s)",
                self.slot_key, meta.get("schema_version"), _SCHEMA_VERSION,
            )
            return False

        # Gate 4 — age
        if self.max_age > 0:
            built_at = meta.get("built_at", "")
            if built_at:
                try:
                    ts  = datetime.fromisoformat(
                        built_at.replace("Z", "+00:00")
                    ).timestamp()
                    age = time.time() - ts
                    if age > self.max_age:
                        logger.info(
                            "[Cache] %s: expired (age=%.0fs > max=%ss)",
                            self.slot_key, age, self.max_age,
                        )
                        return False
                except Exception:
                    return False

        # Gate 6 — engine file present (before the more expensive checks)
        if not os.path.isfile(self.engine_path):
            logger.info("[Cache] %s: engine file missing", self.slot_key)
            return False

        # Gate 5 — product count
        try:
            current  = _get_product_count(self.source_db_id)
            cached   = meta.get("product_count", -1)
            if current != cached:
                logger.info(
                    "[Cache] %s: product_count mismatch (db=%d, cache=%d)",
                    self.slot_key, current, cached,
                )
                return False
        except Exception as exc:
            logger.warning("[Cache] %s: product_count check error: %s", self.slot_key, exc)
            return False

        # Gate 7 — checksum integrity
        stored_checksum = meta.get("checksum")
        if stored_checksum:
            try:
                computed = _file_sha256(self.engine_path)
                if computed != stored_checksum:
                    logger.warning(
                        "[Cache] %s: checksum mismatch — cache corrupted, invalidating",
                        self.slot_key,
                    )
                    return False
            except Exception as exc:
                logger.warning("[Cache] %s: checksum read error: %s", self.slot_key, exc)
                return False

        return True

    # ── Load ───────────────────────────────────────────────────────────────────

    def load_engine_data(self) -> Optional[Dict]:
        """
        Load and return the cached engine data dict, or None on miss / failure.

        On a cache miss the slot is left untouched; the caller must rebuild.
        On a corrupted file the slot is cleaned up automatically before returning.

        The returned dict contains:
            items              — list of product dicts (fully enriched)
            raw_strings        — list of raw searchable strings (one per item)
            normalized_strings — list of normalised strings (one per item)
            last_built         — Unix timestamp of when the index was built
        """
        with self._lock:
            if not self.is_valid():
                return None

            t0 = time.time()
            try:
                if self.compression:
                    with gzip.open(self.engine_path, "rb") as fh:
                        data = pickle.load(fh)
                else:
                    with open(self.engine_path, "rb") as fh:
                        data = pickle.load(fh)

                elapsed = time.time() - t0
                size_mb = os.path.getsize(self.engine_path) / (1024 * 1024)
                n       = len(data.get("items", []))
                logger.info(
                    "[Cache] HIT  %s: %d items, %.1f MB, loaded in %.3fs",
                    self.slot_key, n, size_mb, elapsed,
                )
                print(
                    f"[Cache] HIT  {self.slot_key}: {n} items, "
                    f"{size_mb:.1f} MB loaded in {elapsed:.3f}s"
                )
                return data

            except Exception as exc:
                logger.warning("[Cache] %s: load error: %s — invalidating", self.slot_key, exc)
                print(f"[Cache] {self.slot_key}: load error ({exc}) — will rebuild from DB")
                self._cleanup_files()
                return None

    # ── Save ───────────────────────────────────────────────────────────────────

    def save_engine_data(self, data: Dict[str, Any]) -> bool:
        """
        Persist engine data to disk using an atomic write strategy:
          1. Write to ``engine.pkl.gz.tmp`` (or ``engine.pkl.tmp``).
          2. Compute SHA-256 of the temp file.
          3. Atomically rename temp → final with ``os.replace()``.
          4. Write metadata.json the same way.

        Returns True on success, False on any error (non-fatal).
        """
        with self._lock:
            tmp_engine   = self.engine_path + ".tmp"
            tmp_metadata = self.metadata_path + ".tmp"
            t0           = time.time()

            try:
                os.makedirs(self.cache_dir, exist_ok=True)

                # ── Step 1: write engine data ──────────────────────────────────
                if self.compression:
                    with gzip.open(tmp_engine, "wb", compresslevel=6) as fh:
                        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
                else:
                    with open(tmp_engine, "wb") as fh:
                        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)

                # ── Step 2: compute checksum while temp file is still accessible
                checksum = _file_sha256(tmp_engine)

                # ── Step 3: atomic rename ──────────────────────────────────────
                os.replace(tmp_engine, self.engine_path)

                # ── Step 4: save metadata ──────────────────────────────────────
                n        = len(data.get("items", []))
                size     = os.path.getsize(self.engine_path)
                metadata = {
                    "cache_version":  self._cache_version,
                    "schema_version": _SCHEMA_VERSION,
                    "source_db_id":   self.source_db_id,
                    "product_count":  n,
                    "built_at":       datetime.now(timezone.utc).isoformat(),
                    "checksum":       checksum,
                    "size_bytes":     size,
                    "compression":    self.compression,
                }
                with open(tmp_metadata, "w", encoding="utf-8") as fh:
                    json.dump(metadata, fh, indent=2)
                os.replace(tmp_metadata, self.metadata_path)

                elapsed = time.time() - t0
                size_mb = size / (1024 * 1024)
                logger.info(
                    "[Cache] SAVE %s: %d items, %.1f MB, written in %.3fs",
                    self.slot_key, n, size_mb, elapsed,
                )
                print(
                    f"[Cache] SAVE {self.slot_key}: {n} items, "
                    f"{size_mb:.1f} MB written in {elapsed:.3f}s"
                )
                return True

            except Exception as exc:
                logger.error("[Cache] %s: save error: %s", self.slot_key, exc)
                print(f"[Cache] {self.slot_key}: save error ({exc})")
                # Best-effort cleanup of any partial temp files
                for tmp in (tmp_engine, tmp_metadata):
                    try:
                        if os.path.isfile(tmp):
                            os.unlink(tmp)
                    except Exception:
                        pass
                return False

    # ── Invalidation ───────────────────────────────────────────────────────────

    def invalidate(self) -> None:
        """
        Delete all cache files for this slot.
        The next engine access will trigger a full rebuild from SQLite.
        Thread-safe.
        """
        with self._lock:
            self._cleanup_files()
        logger.info("[Cache] Invalidated slot %s", self.slot_key)
        print(f"[Cache] Invalidated slot {self.slot_key}")

    def _cleanup_files(self) -> None:
        """Remove engine and metadata files (and any stale .tmp variants)."""
        targets = [
            self.engine_path,
            self.metadata_path,
            self.engine_path   + ".tmp",
            self.metadata_path + ".tmp",
        ]
        for path in targets:
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except Exception as exc:
                logger.debug("[Cache] Could not remove %s: %s", path, exc)

    # ── Stats ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """
        Return a status dict for this cache slot.

        Keys
        ----
        slot            — slot key ("global" or "db_<id>")
        source_db_id    — int or None
        cached          — True if a valid engine file exists
        product_count   — number of products in the cache (None if uncached)
        built_at        — ISO 8601 timestamp of last cache write (None if uncached)
        size_bytes      — compressed/raw file size in bytes (None if uncached)
        cache_version   — version string stored in metadata
        compression     — whether gzip compression was used
        engine_path     — absolute path to the engine file
        """
        meta = self.load_metadata()
        stat: Dict = {
            "slot":         self.slot_key,
            "source_db_id": self.source_db_id,
            "cached":       False,
            "engine_path":  self.engine_path,
        }
        if meta:
            stat.update({
                "cached":        os.path.isfile(self.engine_path),
                "product_count": meta.get("product_count"),
                "built_at":      meta.get("built_at"),
                "size_bytes":    meta.get("size_bytes"),
                "cache_version": meta.get("cache_version"),
                "compression":   meta.get("compression"),
            })
        return stat


# ── Module-level manager registry ─────────────────────────────────────────────
# One CacheManager per slot, created lazily on first access.

_managers: Dict[str, "CacheManager"] = {}
_managers_lock = threading.Lock()


def get_cache_manager(source_db_id: Optional[int]) -> CacheManager:
    """
    Return (creating if needed) the CacheManager for *source_db_id*.

    source_db_id=None  → global engine manager
    source_db_id=1     → isolated engine for database 1
    etc.

    Configuration is read from config.py on first call.
    """
    key = "global" if source_db_id is None else f"db_{source_db_id}"

    if key not in _managers:
        with _managers_lock:
            if key not in _managers:
                from config import (
                    CACHE_DIR,
                    CACHE_COMPRESSION,
                    CACHE_VERSION,
                    CACHE_MAX_AGE,
                )
                _managers[key] = CacheManager(
                    source_db_id  = source_db_id,
                    cache_dir     = CACHE_DIR,
                    cache_version = CACHE_VERSION,
                    compression   = CACHE_COMPRESSION,
                    max_age       = CACHE_MAX_AGE,
                )

    return _managers[key]


def invalidate_all() -> None:
    """Invalidate every known cache slot.  Called when a global rebuild is needed."""
    with _managers_lock:
        known = list(_managers.values())
    for mgr in known:
        mgr.invalidate()


def get_all_stats() -> list:
    """Return stats dicts for every known cache slot."""
    with _managers_lock:
        known = list(_managers.values())
    return [mgr.get_stats() for mgr in known]


# ── Startup cleanup ────────────────────────────────────────────────────────────

def cleanup_stale_tmp_files(cache_dir: str) -> int:
    """
    Remove leftover ``.tmp`` files from interrupted previous writes.
    Called once at application startup when AUTO_CLEANUP is enabled.
    Returns the number of files removed.
    """
    removed = 0
    if not os.path.isdir(cache_dir):
        return 0
    try:
        for root, _dirs, files in os.walk(cache_dir):
            for fname in files:
                if fname.endswith(".tmp"):
                    path = os.path.join(root, fname)
                    try:
                        os.unlink(path)
                        removed += 1
                        logger.info("[Cache] Removed stale tmp file: %s", path)
                    except Exception as exc:
                        logger.debug("[Cache] Could not remove %s: %s", path, exc)
    except Exception as exc:
        logger.warning("[Cache] Startup cleanup error: %s", exc)
    if removed:
        print(f"[Cache] Startup cleanup: removed {removed} stale .tmp file(s).")
    return removed
