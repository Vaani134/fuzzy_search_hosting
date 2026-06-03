"""
tests/benchmark_runner.py
--------------------------
Standalone benchmark runner — measures real latency outside pytest.

Produces:
  tests/reports/benchmark_report.json
  tests/reports/benchmark_report.html

Usage:
    python tests/benchmark_runner.py
    python tests/benchmark_runner.py --db db/local.db --runs 200
"""

import argparse
import gc
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


# ── Percentile helpers ────────────────────────────────────────────────────────

def _percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
    return round(s[idx], 3)


def _stats(samples: List[float]) -> Dict:
    if not samples:
        return {"p50": 0, "p95": 0, "p99": 0, "avg": 0, "max": 0, "min": 0, "samples": 0}
    return {
        "p50_ms":  _percentile(samples, 50),
        "p95_ms":  _percentile(samples, 95),
        "p99_ms":  _percentile(samples, 99),
        "avg_ms":  round(sum(samples) / len(samples), 3),
        "max_ms":  round(max(samples), 3),
        "min_ms":  round(min(samples), 3),
        "samples": len(samples),
    }


def _bench(fn: Callable, runs: int, warmup: int = 5) -> Dict:
    for _ in range(warmup):
        fn()
    gc.collect()

    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)

    return _stats(samples)


# ── Benchmark suites ──────────────────────────────────────────────────────────

def bench_search(engine, runs: int) -> Dict:
    queries = [
        ("hookah",    "exact prefix"),
        ("hooka",     "typo"),
        ("grinder metal 4-part", "multi-word"),
        ("xzqmwvb123",  "zero results"),
        ("glass bong",  "multi-word match"),
    ]
    results = {}
    for q, label in queries:
        stats = _bench(lambda q=q: engine.search(q, top_k=20), runs)
        results[f"search_{label.replace(' ', '_')}"] = {
            "query": q,
            **stats,
            "pass": stats["p95_ms"] < 100.0,
        }
    return results


def bench_autocomplete(test_db_path: str, runs: int) -> Dict:
    import db.database as _db
    orig = _db.get_connection

    def _conn():
        conn = sqlite3.connect(test_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    _db.get_connection = _conn
    import modules.autocomplete as ac
    orig_redis = ac._redis_client
    ac._redis_client = None

    try:
        from modules.autocomplete import get_suggestions

        # Cold (cache-miss) benchmark
        def _cold():
            with ac._mem_lock:
                ac._mem_cache.clear()
            return get_suggestions("hook", limit=10, source_db_id=1)

        # Warm (cache-hit) benchmark
        get_suggestions("hook", limit=10, source_db_id=1)

        def _warm():
            return get_suggestions("hook", limit=10, source_db_id=1)

        cold_stats = _bench(_cold, runs // 2)
        warm_stats = _bench(_warm, runs)

        return {
            "autocomplete_cold": {**cold_stats, "pass": cold_stats["p95_ms"] < 50.0},
            "autocomplete_warm": {**warm_stats, "pass": warm_stats["p95_ms"] < 10.0},
        }
    finally:
        _db.get_connection = orig
        ac._redis_client = orig_redis


def bench_cache(runs: int) -> Dict:
    from modules.cache import SearchCache, _InMemoryCache
    sc = SearchCache.__new__(SearchCache)
    sc._ttl = 60
    sc._max_size = 1000
    sc._backend = _InMemoryCache(ttl=60, max_size=1000)

    key  = SearchCache.make_key("hookah")
    data = [{"id": i, "name": f"Product {i}"} for i in range(20)]
    sc.set(key, data)

    miss_stats = _bench(lambda: sc.get("nonexistent_key_xyz"), runs)
    hit_stats  = _bench(lambda: sc.get(key), runs)

    return {
        "cache_get_miss": {**miss_stats, "pass": miss_stats["avg_ms"] < 1.0},
        "cache_get_hit":  {**hit_stats,  "pass": hit_stats["avg_ms"] < 1.0},
    }


def bench_rebuild(engine, fresh_db: str, runs: int) -> Dict:
    import db.database as _db
    orig = _db.get_connection

    def _conn():
        conn = sqlite3.connect(fresh_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    _db.get_connection = _conn
    try:
        rebuild_stats = _bench(engine.rebuild, runs, warmup=1)
        return {
            "rebuild_full": {**rebuild_stats, "pass": rebuild_stats["p95_ms"] < 10_000},
        }
    finally:
        _db.get_connection = orig


def bench_startup(fresh_db: str) -> Dict:
    import db.database as _db
    orig = _db.get_connection

    def _conn():
        conn = sqlite3.connect(fresh_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    _db.get_connection = _conn
    try:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="bench_cache_")

        from modules.cache_manager import CacheManager
        from modules.fuzzy_search import FuzzySearchEngine

        # Cold start
        t0 = time.perf_counter()
        engine = FuzzySearchEngine(source_db_id=1)
        engine.rebuild()
        cold_ms = (time.perf_counter() - t0) * 1000

        # Warm start
        t1 = time.perf_counter()
        engine2 = FuzzySearchEngine(source_db_id=1)
        engine2._load_from_cache()
        warm_ms = (time.perf_counter() - t1) * 1000

        speedup = round(cold_ms / warm_ms, 1) if warm_ms > 0 else 0

        return {
            "cold_startup_ms": round(cold_ms, 2),
            "warm_startup_ms": round(warm_ms, 2),
            "speedup_factor":  speedup,
            "pass": speedup >= 3.0 or warm_ms < 500,
        }
    finally:
        _db.get_connection = orig
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ── HTML report ───────────────────────────────────────────────────────────────

def _pass_badge(passed: bool) -> str:
    if passed:
        return '<span style="color:#2ecc71">✓ PASS</span>'
    return '<span style="color:#e74c3c">✗ FAIL</span>'


def save_benchmark_html(report: Dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    rows_html = ""
    for name, data in report.get("benchmarks", {}).items():
        if not isinstance(data, dict):
            continue
        passed = data.get("pass", True)
        rows_html += f"""
        <tr>
          <td>{name}</td>
          <td>{data.get('p50_ms', 'N/A')}</td>
          <td>{data.get('p95_ms', 'N/A')}</td>
          <td>{data.get('p99_ms', 'N/A')}</td>
          <td>{data.get('avg_ms', 'N/A')}</td>
          <td>{data.get('samples', 'N/A')}</td>
          <td>{_pass_badge(passed)}</td>
        </tr>"""

    startup = report.get("startup", {})
    startup_html = f"""
    <h2>Startup Performance</h2>
    <table>
      <thead><tr><th>Metric</th><th>Value</th></tr></thead>
      <tbody>
        <tr><td>Cold Startup</td><td>{startup.get('cold_startup_ms', 'N/A')}ms</td></tr>
        <tr><td>Warm Startup</td><td>{startup.get('warm_startup_ms', 'N/A')}ms</td></tr>
        <tr><td>Speedup Factor</td><td>{startup.get('speedup_factor', 'N/A')}×</td></tr>
        <tr><td>Status</td><td>{_pass_badge(startup.get('pass', False))}</td></tr>
      </tbody>
    </table>""" if startup else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Benchmark Report — Fuzzy Search</title>
<style>
  body{{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5}}
  h1,h2{{color:#2c3e50}} table{{width:100%;border-collapse:collapse;background:#fff;margin-bottom:20px}}
  th{{background:#2c3e50;color:#fff;padding:8px;text-align:left}}
  td{{padding:7px 8px;border-bottom:1px solid #ddd}}
</style>
</head>
<body>
<h1>Benchmark Report — Fuzzy Search Hosting</h1>
<p>Generated: {report.get('generated_at', '')} | Runs per benchmark: {report.get('runs', '?')}</p>

{startup_html}

<h2>Latency Benchmarks</h2>
<table>
<thead><tr><th>Benchmark</th><th>P50 (ms)</th><th>P95 (ms)</th><th>P99 (ms)</th><th>Avg (ms)</th><th>Samples</th><th>SLA</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Saved {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run standalone benchmarks")
    parser.add_argument("--db",   default="", help="SQLite DB path (defaults to test DB)")
    parser.add_argument("--runs", type=int, default=100, help="Runs per benchmark")
    args = parser.parse_args()

    # Resolve DB path
    if args.db and os.path.isfile(args.db):
        db_path = args.db
    else:
        # Use or create a temporary test database
        import tempfile
        import shutil
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _schema = os.path.join(_project_root, "db", "schema.sql")
        _tmp = tempfile.mkdtemp(prefix="bench_db_")
        db_path = os.path.join(_tmp, "bench.db")

        with open(_schema) as fh:
            schema = fh.read()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(schema)
        conn.execute("INSERT OR IGNORE INTO connected_databases (id,name,host,port,username,password,database_name,sync_status) VALUES (1,'Bench','localhost',3306,'root','','bench','ok')")
        from tests.test_data_generator import generate_products, generate_brands, generate_categories, insert_into_db
        brands = generate_brands()
        categories = generate_categories()
        products = generate_products(1000, source_db_id=1)
        conn.commit()
        conn.close()
        insert_into_db(db_path, products, brands, categories)
        print(f"  Created test DB at {db_path} with 1,000 products")

    import db.database as _db
    orig = _db.get_connection

    def _conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    _db.get_connection = _conn

    try:
        import tempfile, shutil
        cache_tmp = tempfile.mkdtemp(prefix="bench_cache_")
        import config
        config.CACHE_DIR = cache_tmp
        config.REDIS_URL = ""

        from modules.fuzzy_search import FuzzySearchEngine
        print("Building engine…")
        engine = FuzzySearchEngine(source_db_id=1)
        engine.rebuild()
        n = engine.stats()["total_products"]
        print(f"  Engine loaded: {n} products")

        print(f"\nRunning benchmarks ({args.runs} runs each)…")
        all_benchmarks = {}

        print("  search…")
        all_benchmarks.update(bench_search(engine, args.runs))

        print("  autocomplete…")
        all_benchmarks.update(bench_autocomplete(db_path, args.runs))

        print("  cache…")
        all_benchmarks.update(bench_cache(args.runs))

        print("  rebuild…")
        all_benchmarks.update(bench_rebuild(engine, db_path, max(5, args.runs // 10)))

        print("  startup…")
        startup = bench_startup(db_path)

        report = {
            "generated_at": datetime.now().isoformat(),
            "runs":         args.runs,
            "product_count": n,
            "startup":      startup,
            "benchmarks":   all_benchmarks,
        }

        os.makedirs(REPORTS_DIR, exist_ok=True)
        json_path = f"{REPORTS_DIR}/benchmark_report.json"
        html_path = f"{REPORTS_DIR}/benchmark_report.html"

        with open(json_path, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"\n  Saved {json_path}")

        save_benchmark_html(report, html_path)

        print("\n── Results ──────────────────────────────────────")
        for name, data in all_benchmarks.items():
            if isinstance(data, dict):
                status = "✓" if data.get("pass") else "✗"
                print(f"  {status} {name}: P50={data.get('p50_ms','?')}ms  P95={data.get('p95_ms','?')}ms  P99={data.get('p99_ms','?')}ms")

        print(f"\n  Startup: cold={startup.get('cold_startup_ms','?')}ms  warm={startup.get('warm_startup_ms','?')}ms  speedup={startup.get('speedup_factor','?')}×")

        shutil.rmtree(cache_tmp, ignore_errors=True)

    finally:
        _db.get_connection = orig


if __name__ == "__main__":
    main()
