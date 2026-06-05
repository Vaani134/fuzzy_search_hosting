"""Final before/after benchmark."""
import sys, os, time, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db.database import init_db
from modules.fuzzy_search import get_engine

init_db()
e = get_engine(source_db_id=4)
print(f"Engine: {e.stats()['total_products']:,} products (db_4)")

queries = [
    ("hookah",        "exact match"),
    ("grinder",       "common product"),
    ("cigarette",     "high-volume"),
    ("glass pipe",    "multi-word broad"),
    ("smoking stuff", "expansion 6 terms"),
    ("vape",          "short term"),
    ("energy drink",  "beverage"),
    ("blunt wrap",    "rolling"),
    ("lighter",       "accessory"),
    ("tobacco",       "generic"),
]

# Warm-up
print("Warm-up...")
for q, _ in queries:
    e.search_with_field_scores(q, top_k=20)

print("\nTiming (3 runs per query)...")
print(f"{'Query':<20}  {'Type':<20}  {'Run1':>6}  {'Run2':>6}  {'Run3':>6}  {'Mean':>6}  {'N':>4}")
print("-" * 80)

all_means = []
for query, label in queries:
    times = []
    result_count = 0
    for i in range(3):
        t0 = time.perf_counter()
        r = e.search_with_field_scores(query, top_k=20)
        times.append((time.perf_counter() - t0) * 1000)
        if i == 0:
            result_count = len(r)
    mean = statistics.mean(times)
    all_means.append(mean)
    print(f"{query:<20}  {label:<20}  {times[0]:>5.0f}ms  {times[1]:>5.0f}ms  {times[2]:>5.0f}ms  {mean:>5.0f}ms  {result_count:>4}")

print("-" * 80)
print(f"{'AGGREGATE MEAN':<44}                       {statistics.mean(all_means):>5.0f}ms")

print("\n" + "="*80)
print("BEFORE / AFTER COMPARISON (original WRatio full scan vs optimized)")
print("="*80)

# Baseline was ~4,400ms per query from micro2.py for hookah
# and proportionally slower for expansion queries
baseline_estimates = {
    "hookah":        4418,
    "grinder":       4418,
    "cigarette":     4418,
    "glass pipe":    4418,
    "smoking stuff": 4418 * 6,   # 6 expansion terms
    "vape":          4418,
    "energy drink":  4418,
    "blunt wrap":    4418,
    "lighter":       4418,
    "tobacco":       4418,
}

for (query, label), mean in zip(queries, all_means):
    baseline = baseline_estimates[query]
    speedup  = baseline / mean if mean > 0 else 0
    print(f"  {query:<20}  before={baseline:>6.0f}ms  after={mean:>5.0f}ms  speedup={speedup:>5.1f}x")
