# Testing Guide — Fuzzy Search Hosting

Complete reference for running, extending, and interpreting the automated test suite.

---

## Prerequisites

```bash
# Install test dependencies
pip install pytest pytest-html pytest-json-report pytest-benchmark psutil locust

# Verify rapidfuzz is installed (required for fuzzy_search tests)
pip install rapidfuzz
```

---

## Directory Structure

```
tests/
├── conftest.py              — shared fixtures (temp DB, Flask client, engines)
├── pytest.ini               — pytest config (in project root)
├── TESTING.md               — this file
│
├── unit/                    — pure unit tests, no I/O
│   ├── test_cache_manager.py
│   ├── test_cache.py
│   ├── test_autocomplete.py
│   ├── test_fuzzy_search.py
│   └── test_metrics.py
│
├── integration/             — Flask test client + seeded SQLite DB
│   ├── test_search_api.py
│   ├── test_autocomplete_api.py
│   └── test_cache_rebuild.py
│
├── performance/             — latency SLA assertions + pytest-benchmark
│   └── test_benchmarks.py
│
├── concurrency/             — thread-safety, race condition, CoW tests
│   └── test_concurrent_search.py
│
├── memory/                  — RSS growth monitoring via psutil
│   └── test_memory_usage.py
│
├── load/                    — Locust load test scenarios
│   └── locustfile.py
│
├── fixtures/                — shared test data
│   └── __init__.py
│
├── test_data_generator.py   — synthetic product catalogue generator
├── report_generator.py      — converts pytest JSON → HTML + production readiness
├── benchmark_runner.py      — standalone latency benchmarks
│
└── reports/                 — generated output (git-ignored)
    ├── test_report.json
    ├── test_report.csv
    ├── test_report.html
    ├── benchmark_report.json
    ├── benchmark_report.html
    ├── production_readiness_report.json
    ├── production_readiness_report.html
    └── dashboard.html
```

---

## Running Tests

### Full suite (all phases)

```bash
pytest
```

### By phase

```bash
# Unit tests only
pytest tests/unit/ -v

# Integration tests only
pytest tests/integration/ -v

# Performance / latency assertions
pytest tests/performance/ -v

# Concurrency tests
pytest tests/concurrency/ -v

# Memory leak tests (requires psutil)
pytest tests/memory/ -v
```

### By marker

```bash
pytest -m unit
pytest -m integration
pytest -m "not slow"
```

### With coverage

```bash
pip install pytest-cov
pytest --cov=modules --cov=routes --cov-report=html:tests/reports/coverage
```

---

## Benchmark Suite

### pytest-benchmark (inline with pytest)

```bash
# Run benchmark tests, save JSON
pytest tests/performance/ \
  --benchmark-only \
  --benchmark-json=tests/reports/benchmark_report.json \
  --benchmark-columns=min,mean,max,stddev,median,iqr,rounds

# Compare two benchmark runs
pytest-benchmark compare benchmark_v1.json benchmark_v2.json
```

### Standalone benchmark runner

```bash
# Against the real production DB
python tests/benchmark_runner.py --db db/local.db --runs 200

# Auto-creates a temp DB if --db is omitted
python tests/benchmark_runner.py --runs 100
```

Output: `tests/reports/benchmark_report.json` + `tests/reports/benchmark_report.html`

---

## Load Testing (Locust)

### Start the Flask app first

```bash
python app.py
```

### Locust scenarios

| Class | Behaviour | Weight |
|-------|-----------|--------|
| `SearchUser` | Pure search traffic | 3 |
| `AutocompleteUser` | Pure autocomplete traffic | 4 |
| `MixedUser` | Realistic mixed (search + AC + stats) | 5 |
| `GlobalSearchUser` | Cross-DB global search | 1 |
| `HeavyUser` | High-frequency rapid requests | 2 |

### Headless runs

```bash
# 50 users, ramp 5/s, 60s
locust -f tests/load/locustfile.py \
  --headless -u 50 -r 5 -t 60s \
  --host http://localhost:5000

# 100 users
locust -f tests/load/locustfile.py \
  --headless -u 100 -r 10 -t 120s \
  --host http://localhost:5000

# 500 users (stress test)
locust -f tests/load/locustfile.py \
  --headless -u 500 -r 50 -t 300s \
  --host http://localhost:5000

# Web UI (visit http://localhost:8089)
locust -f tests/load/locustfile.py --host http://localhost:5000
```

Output: `tests/reports/load_test_report.json`

---

## Test Data Generator

```bash
# 10k products, single DB
python tests/test_data_generator.py --count 10000 --db db/local.db

# 100k products across 3 DBs
python tests/test_data_generator.py --count 100000 --dbs 1,2,3 --db db/local.db

# 1M products (stress test — takes a few minutes)
python tests/test_data_generator.py --count 1000000 --db db/local.db

# Dry-run (print stats only)
python tests/test_data_generator.py --count 50000 --dry-run
```

---

## Report Generation

### After running pytest

```bash
# pytest auto-generates test_report.html (via pytest-html)
# For the full dashboard + production readiness report:
python tests/report_generator.py \
  --json tests/reports/.pytest_report.json \
  --benchmark tests/reports/benchmark_report.json
```

This generates:
- `tests/reports/test_report.json` — enriched test results
- `tests/reports/test_report.csv` — for spreadsheet import
- `tests/reports/test_report.html` — visual HTML report
- `tests/reports/production_readiness_report.json`
- `tests/reports/production_readiness_report.html`
- `tests/reports/dashboard.html` — combined visual dashboard

### Open the dashboard

```bash
# Windows
start tests/reports/dashboard.html

# macOS
open tests/reports/dashboard.html
```

---

## Pass / Fail Criteria

| Criterion | Threshold | Test location |
|-----------|-----------|---------------|
| Cache hit ratio | > 80% | `test_cache_rebuild.py` |
| P95 search latency | < 100ms | `test_benchmarks.py::TestLatencyThresholds` |
| P99 search latency | < 250ms | `test_benchmarks.py::TestLatencyThresholds` |
| Autocomplete P95 | < 50ms | `test_benchmarks.py::TestLatencyThresholds` |
| Startup speedup (warm vs cold) | > 3× | `benchmark_runner.py` |
| Memory growth (10k searches) | < 10% | `test_memory_usage.py` |
| Concurrency failures | 0 | `test_concurrent_search.py` |
| Cache corruption failures | 0 | `test_cache_manager.py` |

---

## Production Readiness Score

| Score | Grade | Status |
|-------|-------|--------|
| 90–100 | A | Production Ready |
| 80–89  | B | Minor Improvements Needed |
| 70–79  | C | Significant Improvements Needed |
| < 70   | D | Not Production Ready |

Score is computed as a weighted average:
- Unit tests: 25%
- Integration tests: 25%
- Performance tests: 20%
- Concurrency tests: 20%
- Memory tests: 10%

---

## All-in-one command

```bash
# 1. Run full test suite with reports
pytest && python tests/report_generator.py --json tests/reports/.pytest_report.json

# 2. Run benchmarks
python tests/benchmark_runner.py

# 3. Regenerate dashboard with benchmarks
python tests/report_generator.py \
  --json tests/reports/.pytest_report.json \
  --benchmark tests/reports/benchmark_report.json

# 4. Open dashboard
start tests/reports/dashboard.html
```

---

## Environment Variables for Tests

All these are automatically set by `tests/conftest.py` — no `.env` changes needed.

| Variable | Test Value |
|----------|-----------|
| `SQLITE_PATH` | temp dir / test.db |
| `CACHE_DIR` | temp dir / cache |
| `REDIS_URL` | `` (disabled) |
| `FLASK_ENV` | development |
| `SECRET_KEY` | test-secret-key |
| `METRICS_LATENCY_WINDOW` | 200 |
| `FULL_REBUILD_AFTER_N_INCREMENTALS` | 0 (disabled) |

---

## Extending the Test Suite

### Add a unit test

Create `tests/unit/test_mymodule.py`:
```python
import pytest

class TestMyFeature:
    def test_something(self, fresh_metrics):
        fresh_metrics.record_search(5.0, result_count=3)
        snap = fresh_metrics.snapshot()
        assert snap["searches"]["total"] == 1
```

### Add an integration test

Use the `client` fixture (Flask test client):
```python
def test_my_endpoint(self, client):
    resp = client.get("/api/search?q=hookah")
    assert resp.status_code == 200
```

### Add a load test scenario

Add a new `HttpUser` subclass to `tests/load/locustfile.py`.
