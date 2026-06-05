"""
benchmark_search.py
-------------------
Before/After latency comparison for search optimizations.
Measures search_with_field_scores() on real 40k-product index.
"""

import sys
import os
import time
import statistics
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.database import init_db
from modules.fuzzy_search import get_engine

# ── Test queries ───────────────────────────────────────────────────────────────
TEST_QUERIES = [
    ("hookah",        "exact match"),
    ("grinder",       "common product"),
    ("cigarette",     "high-volume"),
    ("glass pipe",    "multi-word"),
    ("smoking stuff", "expansion (6 terms)"),
    ("vape",          "short term"),
    ("energy drink",  "beverage"),
    ("blunt wrap",    "rolling"),
    ("lighter",       "accessory"),
    ("tobacco",       "generic"),
]

ITERATIONS = 5   # runs per query (kept low to avoid timeout)


def pct(sorted_list, p):
    idx = max(0, int(len(sorted_list) * p / 100) - 1)
    return sorted_list[idx]


def benchmark(engine, iterations=ITERATIONS):
    all_latencies = []
    rows = []

    for query, label in TEST_QUERIES:
        latencies = []
        result_count = 0
        for i in range(iterations):
            t0 = time.perf_counter()
            results = engine.search_with_field_scores(query, top_k=20)
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000.0)
            if i == 0:
                result_count = len(results)
        all_latencies.extend(latencies)
        s = sorted(latencies)
        rows.append({
            "query":   query,
            "label":   label,
            "p50":     pct(s, 50),
            "p95":     pct(s, 95),
            "min":     s[0],
            "max":     s[-1],
            "mean":    statistics.mean(latencies),
            "results": result_count,
        })

    s_all = sorted(all_latencies)
    agg = {
        "p50":  pct(s_all, 50),
        "p95":  pct(s_all, 95),
        "p99":  pct(s_all, 99),
        "mean": statistics.mean(all_latencies),
        "min":  s_all[0],
        "max":  s_all[-1],
    }
    return rows, agg


def print_table(rows, agg, title):
    print(f"\n{'='*78}")
    print(f"  {title}")
    print(f"{'='*78}")
    print(f"{'Query':<20}  {'Type':<22}  {'P50':>7}  {'P95':>7}  {'Mean':>7}  {'N':>4}")
    print("-" * 78)
    for r in rows:
        print(f"{r['query']:<20}  {r['label']:<22}  "
              f"{r['p50']:>6.0f}ms  {r['p95']:>6.0f}ms  "
              f"{r['mean']:>6.0f}ms  {r['results']:>4}")
    print("-" * 78)
    print(f"{'AGGREGATE':<44}  "
          f"{agg['p50']:>6.0f}ms  {agg['p95']:>6.0f}ms  "
          f"{agg['mean']:>6.0f}ms")
    print(f"  P99={agg['p99']:.0f}ms  Min={agg['min']:.0f}ms  Max={agg['max']:.0f}ms")


def main():
    print("Initializing...")
    init_db()
    engine = get_engine(source_db_id=4, rebuild_interval=None)
    n = engine.stats()["total_products"]
    print(f"Engine: {n:,} products (db_4)")

    # Warm-up
    print("\nWarm-up pass...")
    for q, _ in TEST_QUERIES:
        engine.search_with_field_scores(q, top_k=20)

    # Run benchmark
    print(f"\nBenchmarking ({ITERATIONS} iterations per query)...")
    rows, agg = benchmark(engine)

    print_table(rows, agg, "OPTIMIZED PERFORMANCE — search_with_field_scores() on 40,940 products")

    # Save
    result = {"per_query": rows, "aggregate": agg, "products": n}
    with open("benchmark_optimized.json", "w") as f:
        json.dump(result, f, indent=2)
    print("\nSaved to benchmark_optimized.json")

    # Compare with baseline if it exists
    if os.path.exists("benchmark_baseline.json"):
        with open("benchmark_baseline.json") as f:
            baseline = json.load(f)
        b_agg = baseline.get("aggregate", {})
        if b_agg.get("p50", 0) > 0:
            print(f"\n{'='*78}")
            print("  BEFORE vs AFTER COMPARISON")
            print(f"{'='*78}")
            for metric in ("p50", "p95", "p99", "mean"):
                before = b_agg.get(metric, 0)
                after  = agg.get(metric, 0)
                if before > 0:
                    improvement = (before - after) / before * 100
                    print(f"  {metric.upper():>4}:  {before:>7.1f}ms  →  {after:>7.1f}ms  "
                          f"({improvement:+.1f}%)")


if __name__ == "__main__":
    main()
