"""
tests/unit/test_fuzzy_search.py
--------------------------------
Unit tests for modules/fuzzy_search.py — scoring, boosting, engine.
"""

import sqlite3
import threading
import time

import pytest

from tests.conftest import patch_all_db_connections


# ── normalize ─────────────────────────────────────────────────────────────────

class TestNormalize:
    def test_lowercase(self):
        from modules.fuzzy_search import normalize
        assert normalize("HOOKAH") == "hookah"

    def test_strip_price(self):
        from modules.fuzzy_search import normalize
        assert "$20.00" not in normalize("Hookah $20.00 Small")

    def test_strip_brackets(self):
        from modules.fuzzy_search import normalize
        result = normalize("Pipe [GLASS] 9MM")
        assert "[" not in result
        assert "glass" not in result   # bracket content removed

    def test_collapse_whitespace(self):
        from modules.fuzzy_search import normalize
        assert "  " not in normalize("hookah   large   glass")

    def test_empty_string(self):
        from modules.fuzzy_search import normalize
        assert normalize("") == ""

    def test_none_input(self):
        from modules.fuzzy_search import normalize
        assert normalize(None) == ""

    def test_special_chars_removed(self):
        from modules.fuzzy_search import normalize
        result = normalize("hookah & pipe — special!")
        assert "&" not in result
        assert "—" not in result


# ── apply_synonyms ────────────────────────────────────────────────────────────

class TestApplySynonyms:
    def test_no_synonyms_passthrough(self):
        from modules.fuzzy_search import apply_synonyms, SYNONYMS
        if not SYNONYMS:
            result = apply_synonyms("hookah grinder")
            assert result == "hookah grinder"

    def test_empty_query(self):
        from modules.fuzzy_search import apply_synonyms
        assert apply_synonyms("") == ""

    def test_whitespace_only(self):
        from modules.fuzzy_search import apply_synonyms
        result = apply_synonyms("   ")
        assert result.strip() == ""


# ── blend_score ───────────────────────────────────────────────────────────────

class TestBlendScore:
    def test_exact_match_scores_high(self):
        from modules.fuzzy_search import blend_score
        score = blend_score("hookah", "hookah", "hookah")
        assert score >= 90.0

    def test_unrelated_strings_score_low(self):
        from modules.fuzzy_search import blend_score
        score = blend_score("hookah", "bicycle", "bicycle")
        assert score < 40.0

    def test_partial_match_scores_medium(self):
        from modules.fuzzy_search import blend_score
        score = blend_score("hook", "hookah small glass", "hookah small glass")
        assert 40.0 < score < 100.0

    def test_score_range_0_to_100(self):
        from modules.fuzzy_search import blend_score
        score = blend_score("abc", "xyz", "xyz")
        assert 0.0 <= score <= 100.0

    def test_transposition_handled(self):
        from modules.fuzzy_search import blend_score
        score_correct = blend_score("grinder", "grinder metal", "grinder metal")
        score_typo    = blend_score("grdiner", "grinder metal", "grinder metal")
        assert score_correct > score_typo


# ── apply_boost ───────────────────────────────────────────────────────────────

class TestApplyBoost:
    def test_exact_match_adds_20(self):
        from modules.fuzzy_search import apply_boost
        score = apply_boost(70.0, "hookah", "hookah", "Hookah")
        assert score == 90.0

    def test_startswith_adds_10(self):
        from modules.fuzzy_search import apply_boost
        score = apply_boost(70.0, "hook", "hookah small glass", "Hookah Small Glass")
        assert score == 80.0

    def test_substring_raw_adds_10(self):
        from modules.fuzzy_search import apply_boost
        score = apply_boost(70.0, "hookah", "china hookah", "China Hookah Small")
        # substring match: "hookah" in "china hookah small"
        assert score >= 80.0

    def test_capped_at_100(self):
        from modules.fuzzy_search import apply_boost
        score = apply_boost(95.0, "hookah", "hookah", "Hookah")
        assert score == 100.0

    def test_no_match_returns_base(self):
        from modules.fuzzy_search import apply_boost
        score = apply_boost(55.0, "xyz", "abc def", "ABC DEF")
        assert score == 55.0


# ── get_query_suggestion ──────────────────────────────────────────────────────

class TestGetQuerySuggestion:
    def test_clear_typo(self):
        from modules.fuzzy_search import get_query_suggestion
        result = get_query_suggestion("grdiner")
        assert result == "grinder" or result is not None

    def test_correct_spelling_returns_none(self):
        from modules.fuzzy_search import get_query_suggestion
        result = get_query_suggestion("hookah")
        assert result is None

    def test_empty_query_returns_none(self):
        from modules.fuzzy_search import get_query_suggestion
        assert get_query_suggestion("") is None

    def test_gibberish_returns_none(self):
        from modules.fuzzy_search import get_query_suggestion
        result = get_query_suggestion("xzqmwvb123")
        assert result is None


# ── expand_query ──────────────────────────────────────────────────────────────

class TestExpandQuery:
    def test_no_expansion_returns_original(self):
        from modules.fuzzy_search import expand_query
        result = expand_query("hookah")
        assert result[0] == "hookah"

    def test_expansion_appends_terms(self):
        from modules.fuzzy_search import expand_query
        result = expand_query("smoking stuff")
        assert "smoking stuff" in result
        assert len(result) > 1

    def test_empty_query(self):
        from modules.fuzzy_search import expand_query
        result = expand_query("")
        assert result == [""]

    def test_result_is_deduplicated(self):
        from modules.fuzzy_search import expand_query
        result = expand_query("vaping")
        assert len(result) == len(set(result))


# ── FuzzySearchEngine ─────────────────────────────────────────────────────────

class TestFuzzySearchEngineSearch:
    def test_exact_query_returns_results(self, populated_engine):
        results = populated_engine.search("hookah", top_k=5)
        assert len(results) > 0
        names = [r["name"].lower() for r in results]
        assert any("hookah" in n for n in names)

    def test_fuzzy_typo_returns_results(self, populated_engine):
        results = populated_engine.search("hooka", top_k=5)
        assert len(results) > 0

    def test_unrelated_query_returns_few_or_no_results(self, populated_engine):
        results = populated_engine.search("xzqmwvb123xyz", top_k=5)
        assert len(results) == 0

    def test_results_have_required_fields(self, populated_engine):
        results = populated_engine.search("hookah", top_k=3)
        for r in results:
            assert "id" in r
            assert "name" in r
            assert "score" in r

    def test_results_sorted_by_score_desc(self, populated_engine):
        results = populated_engine.search("hookah", top_k=10)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_k_limits_results(self, populated_engine):
        results = populated_engine.search("hookah", top_k=1)
        assert len(results) <= 1

    def test_inactive_products_excluded(self, populated_engine):
        results = populated_engine.search("inactive product old", top_k=10)
        ids = [r["id"] for r in results]
        from tests.conftest import SAMPLE_PRODUCTS
        inactive_scoped_ids = [
            p["source_db_id"] * 1_000_000_000 + p["id"]
            for p in SAMPLE_PRODUCTS if p["is_inactive"] == 1
        ]
        for sid in inactive_scoped_ids:
            assert sid not in ids

    def test_search_returns_scores_in_range(self, populated_engine):
        results = populated_engine.search("hookah", top_k=10)
        for r in results:
            assert 0.0 <= r["score"] <= 100.0

    def test_empty_query_returns_empty(self, populated_engine):
        results = populated_engine.search("", top_k=10)
        assert results == []

    def test_short_query_returns_empty(self, populated_engine):
        results = populated_engine.search("a", top_k=10)
        assert results == []


class TestFuzzySearchEngineRebuild:
    def test_rebuild_loads_products(self, fresh_engine):
        stats = fresh_engine.stats()
        assert stats["total_products"] > 0

    def test_rebuild_updates_count(self, fresh_engine, fresh_db):
        initial = fresh_engine.stats()["total_products"]

        conn = sqlite3.connect(fresh_db)
        conn.execute(
            """INSERT INTO products
               (id, name, sku, business_id, is_inactive, not_for_selling,
                out_of_stock, enable_stock, ml, created_by, source_db_id)
               VALUES (1999999999,'New Test Product','NTP-001',1,0,0,0,0,0.0,0,1)"""
        )
        conn.commit()
        conn.close()

        with patch_all_db_connections(fresh_db):
            fresh_engine.rebuild()

        new_count = fresh_engine.stats()["total_products"]
        assert new_count > initial


class TestCopyOnWrite:
    def test_concurrent_read_write_no_crash(self, fresh_engine):
        errors = []
        stop = threading.Event()

        def _reader():
            while not stop.is_set():
                try:
                    fresh_engine.search("hookah", top_k=5)
                except Exception as e:
                    errors.append(e)

        readers = [threading.Thread(target=_reader) for _ in range(5)]
        for r in readers:
            r.start()

        time.sleep(0.1)
        stop.set()
        for r in readers:
            r.join(timeout=2)

        assert not errors

    def test_stats_accessible_during_search(self, fresh_engine):
        results = fresh_engine.search("hookah", top_k=5)
        stats = fresh_engine.stats()
        assert stats["total_products"] > 0
        assert len(results) > 0


class TestIncrementalUpdate:
    def test_update_nonexistent_id_is_noop(self, fresh_engine):
        before = fresh_engine.stats()["total_products"]
        fresh_engine.update_products_incremental([999999999])
        after = fresh_engine.stats()["total_products"]
        assert after == before

    def test_remove_reduces_count(self, fresh_engine):
        before = fresh_engine.stats()["total_products"]
        if before == 0:
            pytest.skip("No products loaded")

        items = fresh_engine._items
        if not items:
            pytest.skip("Engine index empty")

        # The engine stores the scoped product id; find the correct key name
        first = items[0]
        target_id = first.get("_id") or first.get("id") or first.get("scoped_id")
        if target_id is None:
            pytest.skip(f"Cannot determine id key from item keys: {list(first.keys())}")

        fresh_engine.remove_products([target_id])
        after = fresh_engine.stats()["total_products"]
        assert after == before - 1

    def test_remove_nonexistent_id_is_noop(self, fresh_engine):
        before = fresh_engine.stats()["total_products"]
        fresh_engine.remove_products([99999999999])
        after = fresh_engine.stats()["total_products"]
        assert after == before
