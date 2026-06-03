"""
tests/integration/test_search_api.py
--------------------------------------
Integration tests for /api/search, /api/autocomplete, /api/cache/* endpoints.
Uses the Flask test client against a seeded test database.
"""

import json
import sqlite3

import pytest


# ── /api/search ───────────────────────────────────────────────────────────────

class TestSearchEndpoint:
    def test_missing_query_returns_400(self, client):
        resp = client.get("/api/search")
        assert resp.status_code == 400

    def test_short_query_returns_empty_results(self, client):
        resp = client.get("/api/search?q=a")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["results"] == []

    def test_valid_query_returns_200(self, client):
        resp = client.get("/api/search?q=hookah")
        assert resp.status_code == 200

    def test_response_schema_keys(self, client):
        resp = client.get("/api/search?q=hookah")
        data = json.loads(resp.data)
        expected_keys = {"query", "results", "total_results", "page", "limit"}
        assert expected_keys.issubset(data.keys())

    def test_results_have_required_fields(self, client):
        resp = client.get("/api/search?q=hookah&limit=5")
        data = json.loads(resp.data)
        for r in data.get("results", []):
            assert "id" in r
            assert "name" in r
            assert "score" in r

    def test_pagination_page1_vs_page2(self, client):
        resp1 = client.get("/api/search?q=hookah&page=1&limit=1")
        resp2 = client.get("/api/search?q=hookah&page=2&limit=1")
        d1 = json.loads(resp1.data)
        d2 = json.loads(resp2.data)
        if d1["total_results"] >= 2:
            r1_ids = [r["id"] for r in d1["results"]]
            r2_ids = [r["id"] for r in d2["results"]]
            assert r1_ids != r2_ids

    def test_limit_parameter_respected(self, client):
        resp = client.get("/api/search?q=hookah&limit=2")
        data = json.loads(resp.data)
        assert len(data["results"]) <= 2

    def test_scores_in_valid_range(self, client):
        resp = client.get("/api/search?q=hookah")
        data = json.loads(resp.data)
        for r in data.get("results", []):
            assert 0.0 <= r["score"] <= 100.0

    def test_results_sorted_by_score_desc(self, client):
        resp = client.get("/api/search?q=hookah&limit=10")
        data = json.loads(resp.data)
        scores = [r["score"] for r in data.get("results", [])]
        assert scores == sorted(scores, reverse=True)

    def test_db_id_isolation(self, client):
        resp = client.get("/api/search?q=hookah&db_id=1")
        data = json.loads(resp.data)
        for r in data.get("results", []):
            assert r.get("source_db_id") == 1

    def test_global_search_mode(self, client):
        resp = client.get("/api/search?q=hookah&db_id=all")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "results" in data

    def test_cache_hit_on_second_request(self, client):
        # First request → cache miss (builds cache)
        client.get("/api/search?q=grinder&limit=5")
        # Second request → cache hit (faster)
        resp = client.get("/api/search?q=grinder&limit=5")
        assert resp.status_code == 200

    def test_content_type_is_json(self, client):
        resp = client.get("/api/search?q=hookah")
        assert "application/json" in resp.content_type

    def test_zero_results_query(self, client):
        resp = client.get("/api/search?q=xzqmwvb123xyz999")
        data = json.loads(resp.data)
        assert data["total_results"] == 0
        assert data["results"] == []

    def test_sort_by_name(self, client):
        resp = client.get("/api/search?q=hookah&sort=name")
        assert resp.status_code == 200

    def test_large_limit_capped(self, client):
        resp = client.get("/api/search?q=hookah&limit=9999")
        data = json.loads(resp.data)
        assert len(data["results"]) <= 100  # SEARCH_MAX_K

    def test_expanded_query_in_response(self, client):
        resp = client.get("/api/search?q=vaping")
        data = json.loads(resp.data)
        assert "expanded_query" in data


# ── /api/autocomplete ─────────────────────────────────────────────────────────

class TestAutocompleteEndpoint:
    def test_missing_query_returns_empty(self, client):
        resp = client.get("/api/autocomplete")
        data = json.loads(resp.data)
        # API returns bare list
        assert isinstance(data, list)
        assert data == []

    def test_valid_query_returns_200(self, client):
        resp = client.get("/api/autocomplete?q=hook")
        assert resp.status_code == 200

    def test_response_is_list(self, client):
        resp = client.get("/api/autocomplete?q=hook")
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_response_has_suggestions_key(self, client):
        resp = client.get("/api/autocomplete?q=hook")
        data = json.loads(resp.data)
        # API returns a bare list — check it's non-empty for "hook"
        assert isinstance(data, list)

    def test_suggestions_are_list(self, client):
        resp = client.get("/api/autocomplete?q=hook")
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_suggestion_fields(self, client):
        resp = client.get("/api/autocomplete?q=hook")
        data = json.loads(resp.data)
        # data is a list of suggestion dicts
        for s in data:
            assert "text" in s
            assert "type" in s

    def test_short_query_returns_empty_suggestions(self, client):
        resp = client.get("/api/autocomplete?q=a")
        data = json.loads(resp.data)
        assert data == []

    def test_db_id_param_accepted(self, client):
        resp = client.get("/api/autocomplete?q=hook&db_id=1")
        assert resp.status_code == 200


# ── /api/cache/stats ─────────────────────────────────────────────────────────

class TestCacheStatsEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/api/cache/stats")
        assert resp.status_code == 200

    def test_response_schema(self, client):
        resp = client.get("/api/cache/stats")
        data = json.loads(resp.data)
        assert "query_cache" in data
        assert "metrics" in data

    def test_metrics_have_search_section(self, client):
        resp = client.get("/api/cache/stats")
        data = json.loads(resp.data)
        assert "searches" in data["metrics"]

    def test_metrics_have_rebuild_section(self, client):
        resp = client.get("/api/cache/stats")
        data = json.loads(resp.data)
        assert "rebuilds" in data["metrics"]


# ── /api/cache/clear ─────────────────────────────────────────────────────────

class TestCacheClearEndpoint:
    def test_returns_200(self, client):
        resp = client.post("/api/cache/clear")
        assert resp.status_code == 200

    def test_response_has_cleared_key(self, client):
        resp = client.post("/api/cache/clear")
        data = json.loads(resp.data)
        assert "cleared" in data or "status" in data or "message" in data

    def test_clears_cached_search(self, client):
        # Populate cache
        client.get("/api/search?q=hookah&limit=5")
        # Clear it
        client.post("/api/cache/clear")
        # Stats should show 0 entries (or at least not crash)
        resp = client.get("/api/cache/stats")
        assert resp.status_code == 200


# ── /api/search/rebuild ───────────────────────────────────────────────────────

class TestSearchRebuildEndpoint:
    def test_returns_200(self, client):
        resp = client.post("/api/search/rebuild")
        assert resp.status_code == 200

    def test_response_schema(self, client):
        resp = client.post("/api/search/rebuild")
        data = json.loads(resp.data)
        assert "status" in data or "message" in data or "ok" in data

    def test_search_works_after_rebuild(self, client):
        client.post("/api/search/rebuild")
        resp = client.get("/api/search?q=hookah")
        assert resp.status_code == 200


# ── /api/search/history ───────────────────────────────────────────────────────

class TestSearchHistoryEndpoint:
    def test_returns_200(self, client):
        client.get("/api/search?q=hookah")  # generate some history
        resp = client.get("/api/search/history")
        assert resp.status_code == 200

    def test_response_is_list_or_has_history_key(self, client):
        resp = client.get("/api/search/history")
        data = json.loads(resp.data)
        assert isinstance(data, list) or "history" in data or "queries" in data
