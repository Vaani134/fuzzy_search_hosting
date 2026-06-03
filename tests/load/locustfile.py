"""
tests/load/locustfile.py
------------------------
Locust load test scenarios for the Fuzzy Search API.

Scenarios:
  1. SearchUser        — pure search traffic
  2. AutocompleteUser  — pure autocomplete traffic
  3. MixedUser         — realistic mixed traffic (search + autocomplete + stats)
  4. GlobalSearchUser  — global multi-DB search
  5. HeavyUser         — concurrent heavy load

Run examples:
  # Headless, 50 users, ramp 5/s, 60s duration
  locust -f tests/load/locustfile.py --headless -u 50 -r 5 -t 60s \
         --host http://localhost:5000 \
         --json --logfile tests/reports/load_test_report.json

  # Web UI
  locust -f tests/load/locustfile.py --host http://localhost:5000
"""

import random
from locust import HttpUser, task, between, events
import json
import time
import os


# ── Sample queries ────────────────────────────────────────────────────────────

SEARCH_QUERIES = [
    "hookah", "grinder", "lighter", "tobacco", "charcoal",
    "vape", "glass pipe", "ashtray", "filter", "blunt wrap",
    "rolling paper", "bong", "e-cigarette", "cigar", "hemp",
    "hooka",        # typo
    "grdiner",      # typo
    "tobaco",       # typo
    "charcoall",    # typo
    "xzqmwvb123",   # zero result
]

AUTOCOMPLETE_PREFIXES = [
    "ho", "gr", "li", "to", "ch", "va", "gl", "as", "fi",
    "bl", "ro", "bo", "ci", "he",
]


# ── Scenario 1: Search only ───────────────────────────────────────────────────

class SearchUser(HttpUser):
    wait_time = between(0.5, 2.0)
    weight = 3

    @task(5)
    def search_product(self):
        query = random.choice(SEARCH_QUERIES)
        with self.client.get(
            "/api/search",
            params={"q": query, "limit": 20, "db_id": 1},
            catch_response=True,
            name="/api/search [db=1]",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if "results" not in data:
                    resp.failure("Missing 'results' in response")
            elif resp.status_code == 400:
                resp.success()  # empty query is valid 400
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    @task(2)
    def search_with_pagination(self):
        query = random.choice(SEARCH_QUERIES[:8])  # use queries with results
        page = random.randint(1, 3)
        self.client.get(
            "/api/search",
            params={"q": query, "limit": 10, "page": page, "db_id": 1},
            name="/api/search [paginated]",
        )

    @task(1)
    def search_with_sort(self):
        query = random.choice(SEARCH_QUERIES[:5])
        sort = random.choice(["score", "name"])
        self.client.get(
            "/api/search",
            params={"q": query, "sort": sort, "db_id": 1},
            name="/api/search [sorted]",
        )


# ── Scenario 2: Autocomplete only ────────────────────────────────────────────

class AutocompleteUser(HttpUser):
    wait_time = between(0.1, 0.5)
    weight = 4

    @task(8)
    def autocomplete_request(self):
        prefix = random.choice(AUTOCOMPLETE_PREFIXES)
        with self.client.get(
            "/api/autocomplete",
            params={"q": prefix, "limit": 10, "db_id": 1},
            catch_response=True,
            name="/api/autocomplete",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if "suggestions" not in data:
                    resp.failure("Missing 'suggestions' in response")
            else:
                resp.failure(f"Unexpected status {resp.status_code}")

    @task(2)
    def autocomplete_longer_prefix(self):
        query = random.choice(SEARCH_QUERIES[:6])
        prefix = query[:4]
        self.client.get(
            "/api/autocomplete",
            params={"q": prefix, "limit": 5, "db_id": 1},
            name="/api/autocomplete [longer]",
        )


# ── Scenario 3: Mixed realistic traffic ───────────────────────────────────────

class MixedUser(HttpUser):
    wait_time = between(0.3, 1.5)
    weight = 5

    @task(5)
    def search(self):
        query = random.choice(SEARCH_QUERIES)
        self.client.get(
            "/api/search",
            params={"q": query, "limit": 20, "db_id": 1},
            name="/api/search [mixed]",
        )

    @task(4)
    def autocomplete(self):
        prefix = random.choice(AUTOCOMPLETE_PREFIXES)
        self.client.get(
            "/api/autocomplete",
            params={"q": prefix, "limit": 10, "db_id": 1},
            name="/api/autocomplete [mixed]",
        )

    @task(1)
    def check_stats(self):
        with self.client.get(
            "/api/cache/stats",
            catch_response=True,
            name="/api/cache/stats",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if "metrics" not in data:
                    resp.failure("Missing 'metrics' in stats response")
            else:
                resp.failure(f"Unexpected status {resp.status_code}")


# ── Scenario 4: Global search ─────────────────────────────────────────────────

class GlobalSearchUser(HttpUser):
    wait_time = between(1.0, 3.0)
    weight = 1

    @task(8)
    def global_search(self):
        query = random.choice(SEARCH_QUERIES)
        with self.client.get(
            "/api/search",
            params={"q": query, "limit": 20, "db_id": "all"},
            catch_response=True,
            name="/api/search [global]",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if "results" not in data:
                    resp.failure("Missing 'results' in global search")
            else:
                resp.failure(f"Global search failed: {resp.status_code}")

    @task(2)
    def global_autocomplete(self):
        prefix = random.choice(AUTOCOMPLETE_PREFIXES)
        self.client.get(
            "/api/autocomplete",
            params={"q": prefix, "limit": 10},
            name="/api/autocomplete [global]",
        )


# ── Scenario 5: Heavy concurrent load ────────────────────────────────────────

class HeavyUser(HttpUser):
    wait_time = between(0.05, 0.2)
    weight = 2

    @task(10)
    def rapid_search(self):
        query = random.choice(SEARCH_QUERIES)
        self.client.get(
            "/api/search",
            params={"q": query, "limit": 5, "db_id": 1},
            name="/api/search [heavy]",
        )

    @task(10)
    def rapid_autocomplete(self):
        prefix = random.choice(AUTOCOMPLETE_PREFIXES)
        self.client.get(
            "/api/autocomplete",
            params={"q": prefix, "limit": 5, "db_id": 1},
            name="/api/autocomplete [heavy]",
        )


# ── Event hooks for report generation ────────────────────────────────────────

_stats_snapshot = {}


@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    """Save load test results to JSON report on exit."""
    stats = environment.stats
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_requests":     stats.total.num_requests,
        "total_failures":     stats.total.num_failures,
        "failure_rate_pct":   round(
            stats.total.num_failures / max(stats.total.num_requests, 1) * 100, 2
        ),
        "avg_response_time_ms": round(stats.total.avg_response_time, 2),
        "p50_ms":  round(stats.total.get_response_time_percentile(0.50) or 0, 2),
        "p95_ms":  round(stats.total.get_response_time_percentile(0.95) or 0, 2),
        "p99_ms":  round(stats.total.get_response_time_percentile(0.99) or 0, 2),
        "max_response_time_ms": round(stats.total.max_response_time or 0, 2),
        "requests_per_second": round(stats.total.current_rps, 2),
        "endpoints": {},
    }

    for name, entry in stats.entries.items():
        report["endpoints"][name[0]] = {
            "method":        name[1],
            "requests":      entry.num_requests,
            "failures":      entry.num_failures,
            "avg_ms":        round(entry.avg_response_time, 2),
            "p95_ms":        round(entry.get_response_time_percentile(0.95) or 0, 2),
            "p99_ms":        round(entry.get_response_time_percentile(0.99) or 0, 2),
        }

    os.makedirs("tests/reports", exist_ok=True)
    out_path = "tests/reports/load_test_report.json"
    with open(out_path, "w") as fh:
        import json as _json
        _json.dump(report, fh, indent=2)
    print(f"\n[Locust] Load test report saved to {out_path}")
