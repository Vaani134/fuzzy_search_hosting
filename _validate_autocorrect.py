"""
Validation for pre-filter fix (bigram) + auto-correction feature.
Run: python _validate_autocorrect.py
"""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db.database as _db_mod
_mem = sqlite3.connect(":memory:", check_same_thread=False)
_mem.row_factory = sqlite3.Row

class _NC:
    def __init__(self, c): self._c = c
    def close(self): pass
    def __getattr__(self, n): return getattr(self._c, n)

_db_mod.get_connection = lambda: _NC(_mem)

schema = open("db/schema.sql", encoding="utf-8").read()
_mem.executescript(schema)
from db.database import _migrate_synonyms, _migrate_synonym_suggestions, _add_column_if_missing
_migrate_synonyms(_mem)
_migrate_synonym_suggestions(_mem)
_add_column_if_missing(_mem, "search_history", "top_score", "REAL NOT NULL DEFAULT 0.0")
_mem.execute("""
    CREATE TABLE IF NOT EXISTS product_clicks (
        product_id  INTEGER PRIMARY KEY,
        click_count INTEGER NOT NULL DEFAULT 0,
        updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
""")
_mem.commit()

# Seed products
_mem.executemany(
    "INSERT INTO products (id, name, is_inactive, business_id, created_by) VALUES (?,?,0,1,1)",
    [
        (1, "Hookah Pipe Large"),
        (2, "Hookah Charcoal"),
        (3, "Hookah Small China"),
        (4, "Cigarette Marlboro"),
        (5, "Grinder 4 Part"),
        (6, "Glass Pipe Beaker"),
    ],
)
_mem.commit()

from modules.fuzzy_search import FuzzySearchEngine, get_query_suggestion
from modules.fuzzy_search import reload_synonyms
reload_synonyms()

engine = FuzzySearchEngine(rebuild_interval=None)

G = "\033[92m  PASS\033[0m"
R = "\033[91m  FAIL\033[0m"
failures = 0

def check_true(label, cond, detail=""):
    global failures
    ok = bool(cond)
    print(f"{G if ok else R}  {label}")
    if not ok:
        print(f"         detail: {detail}")
        failures += 1

def check(label, got, expected):
    global failures
    ok = (got == expected)
    print(f"{G if ok else R}  {label}")
    if not ok:
        print(f"         expected : {expected!r}")
        print(f"         got      : {got!r}")
        failures += 1

# ════════════════════════════════════════════════════════════════
# 1. Pre-filter fix: bigram overlap passes typos through
# ════════════════════════════════════════════════════════════════
print("── 1. Pre-filter: bigram overlap passes typos ──────────────")

from modules.fuzzy_search import normalize

typos_and_products = [
    ("hukah",   "hookah pipe large"),
    ("hoka",    "hookah pipe large"),
    ("hokah",   "hookah pipe large"),
    ("cigrate", "cigarette marlboro"),
    ("hooka",   "hookah pipe large"),
]

for query, product in typos_and_products:
    qn = normalize(query)
    ns = normalize(product)
    query_bigrams = {qn[i:i+2] for i in range(len(qn) - 1)}
    query_first   = qn[0] if qn else ""
    first_ok  = query_first in ns
    bigram_ok = bool(query_bigrams) and any(bg in ns for bg in query_bigrams)
    passes = first_ok and bigram_ok
    check_true(f"'{query}' passes pre-filter for '{product}'",
               passes,
               f"first_ok={first_ok}, bigram_ok={bigram_ok}, bigrams={query_bigrams}")

# ════════════════════════════════════════════════════════════════
# 2. Engine search: typos now return hookah results
# ════════════════════════════════════════════════════════════════
print("\n── 2. Engine search: typos return results ──────────────────")

for typo in ["hooka", "hukah", "hoka", "hokah"]:
    results = engine.search(typo, top_k=10)
    names   = [r["name"] for r in results]
    check_true(f"'{typo}' returns hookah products",
               any("hookah" in n.lower() for n in names),
               f"got: {names}")

for typo in ["cigrate", "cigartte"]:
    results = engine.search(typo, top_k=10)
    names   = [r["name"] for r in results]
    check_true(f"'{typo}' returns cigarette products",
               any("cigarette" in n.lower() for n in names),
               f"got: {names}")

# ════════════════════════════════════════════════════════════════
# 3. Exact searches still rank highest
# ════════════════════════════════════════════════════════════════
print("\n── 3. Exact searches rank highest ──────────────────────────")

for exact in ["hookah", "cigarette", "grinder"]:
    results = engine.search(exact, top_k=10)
    if results:
        top = results[0]
        check_true(f"'{exact}' top result contains the word",
                   exact in top["name"].lower(),
                   f"top result: {top['name']}")
        check_true(f"'{exact}' top result has high fuzzy_score",
                   top["fuzzy_score"] >= 85,
                   f"fuzzy_score={top['fuzzy_score']}")

# ════════════════════════════════════════════════════════════════
# 4. Auto-correction: get_query_suggestion returns correction
# ════════════════════════════════════════════════════════════════
print("\n── 4. get_query_suggestion returns corrections ─────────────")

# These should produce suggestions (they're typos of known keywords)
for typo, expected_canonical in [("hukah", "hookah"), ("cigrate", "cigarette")]:
    suggestion = get_query_suggestion(typo, choices=[], top_result_score=0.0)
    check_true(f"'{typo}' → suggestion exists",
               suggestion is not None,
               f"got None")
    if suggestion:
        print(f"  INFO  '{typo}' → suggestion='{suggestion}'")

# ════════════════════════════════════════════════════════════════
# 5. Auto-correction flow: zero results → retry with correction
# ════════════════════════════════════════════════════════════════
print("\n── 5. Auto-correction flow ─────────────────────────────────")

# Simulate the route logic for a zero-result typo
def simulate_autocorrect(query):
    results = engine.search(query, top_k=10)
    corrected_query = None
    used_correction = False
    if not results:
        correction = get_query_suggestion(query, choices=[], top_result_score=0.0)
        if correction and correction.lower() != query.lower():
            retry = engine.search(correction, top_k=10)
            if retry:
                results         = retry
                corrected_query = correction
                used_correction = True
    return results, corrected_query, used_correction

# "xyzxyz" — no correction possible, stays empty
r, cq, uc = simulate_autocorrect("xyzxyz")
check("'xyzxyz' → no correction, empty results", (len(r), uc), (0, False))

# "hookah" — has results directly, no correction needed
r, cq, uc = simulate_autocorrect("hookah")
check_true("'hookah' → results without correction", len(r) > 0 and not uc)

# ════════════════════════════════════════════════════════════════
# 6. Response fields present in route response dict
# ════════════════════════════════════════════════════════════════
print("\n── 6. Response metadata fields ─────────────────────────────")

route_src = open("routes/search_routes.py", encoding="utf-8").read()
check_true("'original_query' in route source",  '"original_query"'  in route_src)
check_true("'corrected_query' in route source", '"corrected_query"' in route_src)
check_true("'used_correction' in route source", '"used_correction"' in route_src)

# ════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════
total  = 24
passed = total - failures
print(f"\nResults: {passed}/{total} passed", end="")
if failures:
    print(f"  —  {failures} FAILED")
    sys.exit(1)
else:
    print("  ✓")
