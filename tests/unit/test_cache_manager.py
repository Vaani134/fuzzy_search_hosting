"""
tests/unit/test_cache_manager.py
---------------------------------
Unit tests for modules/cache_manager.py — CacheManager and helpers.
"""

import json
import os
import time

import pytest

from tests.conftest import patch_all_db_connections


# ── is_valid tests ────────────────────────────────────────────────────────────

class TestCacheManagerIsValid:
    def test_no_metadata_returns_false(self, cache_manager):
        assert cache_manager.is_valid() is False

    def test_valid_after_save(self, cache_manager, sample_engine_data, test_db_path):
        with patch_all_db_connections(test_db_path):
            ok = cache_manager.save_engine_data(sample_engine_data)
            assert ok
            assert cache_manager.is_valid() is True

    def test_cache_version_mismatch(self, tmp_path, sample_engine_data, test_db_path):
        from modules.cache_manager import CacheManager
        with patch_all_db_connections(test_db_path):
            mgr_v1 = CacheManager(1, str(tmp_path), "1", True, 0)
            mgr_v1.save_engine_data(sample_engine_data)

            mgr_v2 = CacheManager(1, str(tmp_path), "2", True, 0)
            assert mgr_v2.is_valid() is False

    def test_expired_cache(self, tmp_path, sample_engine_data, test_db_path):
        from modules.cache_manager import CacheManager
        with patch_all_db_connections(test_db_path):
            mgr = CacheManager(1, str(tmp_path), "1", True, max_age=1)
            mgr.save_engine_data(sample_engine_data)
            time.sleep(1.1)
            assert mgr.is_valid() is False

    def test_missing_engine_file(self, cache_manager):
        meta = {
            "cache_version": "1",
            "schema_version": "1",
            "source_db_id": 1,
            "product_count": 0,
            "built_at": "2026-01-01T00:00:00+00:00",
            "checksum": "abc",
            "size_bytes": 0,
            "compression": True,
        }
        os.makedirs(cache_manager.cache_dir, exist_ok=True)
        with open(cache_manager.metadata_path, "w") as fh:
            json.dump(meta, fh)
        assert cache_manager.is_valid() is False

    def test_corrupted_checksum(self, cache_manager, sample_engine_data, test_db_path):
        with patch_all_db_connections(test_db_path):
            cache_manager.save_engine_data(sample_engine_data)
            meta = cache_manager.load_metadata()
            meta["checksum"] = "deadbeef" * 8
            with open(cache_manager.metadata_path, "w") as fh:
                json.dump(meta, fh)
            assert cache_manager.is_valid() is False

    def test_corrupted_engine_file(self, cache_manager, sample_engine_data, test_db_path):
        with patch_all_db_connections(test_db_path):
            cache_manager.save_engine_data(sample_engine_data)
            with open(cache_manager.engine_path, "wb") as fh:
                fh.write(b"this is not valid gzip content")
            assert cache_manager.is_valid() is False


# ── save_engine_data tests ────────────────────────────────────────────────────

class TestCacheManagerSave:
    def test_save_creates_files(self, cache_manager, sample_engine_data):
        ok = cache_manager.save_engine_data(sample_engine_data)
        assert ok
        assert os.path.isfile(cache_manager.engine_path)
        assert os.path.isfile(cache_manager.metadata_path)

    def test_save_metadata_content(self, cache_manager, sample_engine_data):
        cache_manager.save_engine_data(sample_engine_data)
        meta = cache_manager.load_metadata()
        assert meta is not None
        assert meta["cache_version"] == "1"
        assert meta["product_count"] == len(sample_engine_data["items"])
        assert "checksum" in meta
        assert "built_at" in meta

    def test_save_no_tmp_files_left(self, cache_manager, sample_engine_data):
        cache_manager.save_engine_data(sample_engine_data)
        tmp_files = [
            f for f in os.listdir(cache_manager.cache_dir)
            if f.endswith(".tmp")
        ]
        assert tmp_files == []

    def test_save_overwrites_previous(self, cache_manager, sample_engine_data):
        cache_manager.save_engine_data(sample_engine_data)
        meta1 = cache_manager.load_metadata()

        new_data = dict(sample_engine_data)
        new_data["items"]             = sample_engine_data["items"] * 2
        new_data["raw_strings"]       = sample_engine_data["raw_strings"] * 2
        new_data["normalized_strings"] = sample_engine_data["normalized_strings"] * 2
        cache_manager.save_engine_data(new_data)
        meta2 = cache_manager.load_metadata()
        assert meta2["product_count"] == 2
        assert meta2["checksum"] != meta1["checksum"]

    def test_save_compression(self, tmp_path, sample_engine_data):
        from modules.cache_manager import CacheManager
        mgr_c = CacheManager(1, str(tmp_path / "comp"),   "1", True,  0)
        mgr_u = CacheManager(1, str(tmp_path / "uncomp"), "1", False, 0)
        mgr_c.save_engine_data(sample_engine_data)
        mgr_u.save_engine_data(sample_engine_data)
        sz_c = os.path.getsize(mgr_c.engine_path)
        sz_u = os.path.getsize(mgr_u.engine_path)
        assert sz_c <= sz_u + 100


# ── load_engine_data tests ────────────────────────────────────────────────────

class TestCacheManagerLoad:
    def test_load_miss_returns_none(self, cache_manager):
        assert cache_manager.load_engine_data() is None

    def test_load_hit_returns_data(self, cache_manager, sample_engine_data, test_db_path):
        with patch_all_db_connections(test_db_path):
            cache_manager.save_engine_data(sample_engine_data)
            loaded = cache_manager.load_engine_data()
        assert loaded is not None
        assert len(loaded["items"]) == len(sample_engine_data["items"])

    def test_load_returns_correct_structure(self, cache_manager, sample_engine_data, test_db_path):
        with patch_all_db_connections(test_db_path):
            cache_manager.save_engine_data(sample_engine_data)
            loaded = cache_manager.load_engine_data()
        assert "items" in loaded
        assert "raw_strings" in loaded
        assert "normalized_strings" in loaded
        assert "last_built" in loaded

    def test_load_corrupt_file_returns_none_and_cleans(self, cache_manager, sample_engine_data, test_db_path):
        with patch_all_db_connections(test_db_path):
            cache_manager.save_engine_data(sample_engine_data)
            # Corrupt the file AND update metadata to reflect bad checksum so
            # is_valid() passes the checksum gate and load_engine_data() gets to
            # the unpickling step where it detects corruption and cleans up.
            with open(cache_manager.engine_path, "wb") as fh:
                fh.write(b"\x00\x01\x02 corrupted bytes that are definitely not gzip")
            # Update metadata checksum to match the new (corrupt) file so is_valid() passes
            import hashlib
            h = hashlib.sha256(b"\x00\x01\x02 corrupted bytes that are definitely not gzip").hexdigest()
            meta = cache_manager.load_metadata()
            meta["checksum"] = h
            with open(cache_manager.metadata_path, "w") as fh:
                import json
                json.dump(meta, fh)
            result = cache_manager.load_engine_data()
        assert result is None
        assert not os.path.isfile(cache_manager.engine_path)


# ── invalidate tests ──────────────────────────────────────────────────────────

class TestCacheManagerInvalidate:
    def test_invalidate_removes_files(self, cache_manager, sample_engine_data):
        cache_manager.save_engine_data(sample_engine_data)
        assert os.path.isfile(cache_manager.engine_path)
        cache_manager.invalidate()
        assert not os.path.isfile(cache_manager.engine_path)
        assert not os.path.isfile(cache_manager.metadata_path)

    def test_invalidate_idempotent(self, cache_manager):
        cache_manager.invalidate()
        cache_manager.invalidate()


# ── get_stats tests ───────────────────────────────────────────────────────────

class TestCacheManagerStats:
    def test_stats_uncached(self, cache_manager):
        stats = cache_manager.get_stats()
        assert stats["cached"] is False
        assert stats["slot"] == "db_1"

    def test_stats_after_save(self, cache_manager, sample_engine_data):
        cache_manager.save_engine_data(sample_engine_data)
        stats = cache_manager.get_stats()
        assert stats["product_count"] == len(sample_engine_data["items"])
        assert stats["built_at"] is not None
        assert stats["size_bytes"] > 0


# ── cleanup_stale_tmp_files tests ─────────────────────────────────────────────

class TestCleanupStaleTmpFiles:
    def test_removes_tmp_files(self, tmp_path):
        from modules.cache_manager import cleanup_stale_tmp_files
        slot = tmp_path / "db_1"
        slot.mkdir()
        stale = slot / "engine.pkl.gz.tmp"
        stale.write_bytes(b"leftover")
        removed = cleanup_stale_tmp_files(str(tmp_path))
        assert removed == 1
        assert not stale.exists()

    def test_no_tmp_files_returns_zero(self, tmp_path):
        from modules.cache_manager import cleanup_stale_tmp_files
        assert cleanup_stale_tmp_files(str(tmp_path)) == 0

    def test_nonexistent_dir_returns_zero(self):
        from modules.cache_manager import cleanup_stale_tmp_files
        assert cleanup_stale_tmp_files("/nonexistent/path/xyz") == 0
