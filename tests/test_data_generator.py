"""
tests/test_data_generator.py
-----------------------------
Generates realistic synthetic product catalogues for load / stress testing.

Usage:
    # Insert 10k products into a target SQLite DB
    python tests/test_data_generator.py --count 10000 --db path/to/local.db

    # Generate 100k products across 3 DBs (db_id 1, 2, 3)
    python tests/test_data_generator.py --count 100000 --dbs 1,2,3 --db path/to/local.db

    # Dry-run: just print stats, don't write
    python tests/test_data_generator.py --count 1000 --dry-run
"""

import argparse
import os
import random
import sqlite3
import sys
import time
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Vocabulary pools ──────────────────────────────────────────────────────────

_ADJECTIVES = [
    "Premium", "Classic", "Deluxe", "Pro", "Mini", "Large", "Small",
    "Heavy", "Lite", "Ultra", "Super", "Standard", "Elite", "Crystal",
    "Black", "Gold", "Silver", "Carbon", "Organic", "Natural",
]

_PRODUCT_TYPES = [
    "Hookah", "Pipe", "Grinder", "Charcoal", "Tobacco", "Lighter",
    "Rolling Paper", "Blunt Wrap", "Filter", "Ashtray", "Vape Pen",
    "E-Cigarette", "Bong", "Bubbler", "Shisha", "Herb Grinder",
    "Glass Pipe", "Water Pipe", "Torch", "Cigar", "Rolling Machine",
]

_VARIANTS = [
    "50g", "100g", "250g", "500g",
    "10pc", "20pc", "50pc", "100pc",
    "Small", "Medium", "Large", "XL",
    "3-Part", "4-Part", "5-Part",
    "6 inch", "8 inch", "10 inch", "12 inch",
    "King Size", "Regular", "Slim",
    "Mint", "Vanilla", "Cherry", "Grape", "Apple",
]

_BRANDS = [
    "HookahKing", "TobaccoPro", "CoalMaster", "GrindCraft", "FlameX",
    "VapeCloud", "GlassWorks", "SmokeElite", "PremiumSmoke", "CloudNine",
    "BlueMountain", "RedLeaf", "GreenHerb", "SilverSmoke", "GoldLeaf",
]

_CATEGORIES = [
    "Hookahs", "Tobacco", "Charcoal", "Grinders", "Lighters",
    "Rolling Supplies", "Vaping", "Accessories", "Glass", "Cigars",
]

_TYPO_TRANSFORMS = [
    lambda s: s[:-1] if len(s) > 3 else s,          # drop last char
    lambda s: s + s[-1],                              # double last char
    lambda s: s.replace("a", "@") if "a" in s else s, # @ for a
    lambda s: s[::-1][:4] + s[4:] if len(s) > 4 else s,  # partial reverse
    lambda s: s.replace("o", "0") if "o" in s else s, # 0 for o
]


def _random_name(add_typo: bool = False) -> str:
    adj     = random.choice(_ADJECTIVES)
    ptype   = random.choice(_PRODUCT_TYPES)
    variant = random.choice(_VARIANTS) if random.random() > 0.4 else ""
    name    = f"{adj} {ptype} {variant}".strip()

    if add_typo:
        transform = random.choice(_TYPO_TRANSFORMS)
        words = name.split()
        idx = random.randint(0, len(words) - 1)
        words[idx] = transform(words[idx])
        name = " ".join(words)

    return name


def _random_sku(idx: int, db_id: int) -> str:
    prefix = random.choice(["HK", "TB", "GR", "LT", "VP", "CH", "RP"])
    return f"{prefix}{db_id:02d}{idx:06d}"


def generate_products(
    count: int,
    source_db_id: int = 1,
    include_duplicates: bool = True,
    include_typos: bool = True,
    include_inactive: bool = True,
    start_id: int = 1,
) -> List[Dict]:
    """
    Generate *count* synthetic product dicts suitable for SQLite insertion.

    Parameters
    ----------
    count           : Number of products to generate.
    source_db_id    : Source DB identifier (1-indexed).
    include_duplicates : If True, ~5% of products share a name with another.
    include_typos   : If True, ~10% of products have a slight name typo.
    include_inactive : If True, ~3% of products are marked is_inactive=1.
    start_id        : First product id (avoids collisions across DB batches).
    """
    products = []
    brand_ids    = list(range(1, len(_BRANDS) + 1))
    category_ids = list(range(1, len(_CATEGORIES) + 1))

    # Pre-generate some "popular" names for duplicate seeding
    popular = [_random_name() for _ in range(max(1, count // 20))]

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    scoped_offset = source_db_id * 1_000_000_000

    for i in range(count):
        local_id = start_id + i
        scoped_id = scoped_offset + local_id

        # Name selection
        use_typo      = include_typos      and random.random() < 0.10
        use_duplicate = include_duplicates and random.random() < 0.05

        if use_duplicate and popular:
            name = random.choice(popular)
        else:
            name = _random_name(add_typo=use_typo)

        is_inactive = 1 if (include_inactive and random.random() < 0.03) else 0

        products.append({
            "id":                 scoped_id,
            "name":               name,
            "item_code":          f"IC{local_id:08d}",
            "business_id":        source_db_id,
            "type":               random.choice(["single", "variable"]),
            "brand_id":           random.choice(brand_ids),
            "category_id":        random.choice(category_ids),
            "sku":                _random_sku(local_id, source_db_id),
            "sku2":               "",
            "sku3":               "",
            "enable_stock":       random.randint(0, 1),
            "is_inactive":        is_inactive,
            "not_for_selling":    0,
            "out_of_stock":       random.randint(0, 1),
            "ml":                 0.0,
            "created_by":         1,
            "created_at":         now,
            "updated_at":         now,
            "source_db_id":       source_db_id,
            "srp":                round(random.uniform(1.0, 200.0), 2),
            "sales_price":        round(random.uniform(0.5, 150.0), 2),
        })

    return products


def generate_brands() -> List[Dict]:
    return [
        {"id": i + 1, "name": name, "business_id": 1, "created_by": 1}
        for i, name in enumerate(_BRANDS)
    ]


def generate_categories() -> List[Dict]:
    return [
        {"id": i + 1, "name": name, "business_id": 1, "parent_id": 0, "created_by": 1}
        for i, name in enumerate(_CATEGORIES)
    ]


def insert_into_db(
    db_path: str,
    products: List[Dict],
    brands: Optional[List[Dict]] = None,
    categories: Optional[List[Dict]] = None,
    batch_size: int = 1000,
) -> None:
    """Insert generated data into an existing SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -32000")  # 32MB cache

    if brands:
        conn.executemany(
            "INSERT OR IGNORE INTO brands (id, name, business_id, created_by) VALUES (:id,:name,:business_id,:created_by)",
            brands,
        )

    if categories:
        conn.executemany(
            "INSERT OR IGNORE INTO categories (id, name, business_id, parent_id, created_by) VALUES (:id,:name,:business_id,:parent_id,:created_by)",
            categories,
        )

    # Batch-insert products
    for i in range(0, len(products), batch_size):
        batch = products[i:i + batch_size]
        conn.executemany(
            """INSERT OR REPLACE INTO products
               (id, name, item_code, business_id, type, brand_id, category_id,
                sku, sku2, sku3, enable_stock, is_inactive, not_for_selling,
                out_of_stock, ml, created_by, created_at, updated_at, source_db_id,
                srp, sales_price)
               VALUES
               (:id,:name,:item_code,:business_id,:type,:brand_id,:category_id,
                :sku,:sku2,:sku3,:enable_stock,:is_inactive,:not_for_selling,
                :out_of_stock,:ml,:created_by,:created_at,:updated_at,:source_db_id,
                :srp,:sales_price)""",
            batch,
        )
        conn.commit()
        print(f"  Inserted {min(i + batch_size, len(products)):,} / {len(products):,} products")

    conn.close()


def _print_stats(products: List[Dict]) -> None:
    total      = len(products)
    inactive   = sum(1 for p in products if p["is_inactive"])
    out_stock  = sum(1 for p in products if p["out_of_stock"])
    db_ids     = set(p["source_db_id"] for p in products)
    names      = [p["name"] for p in products]
    unique     = len(set(names))

    print(f"\n{'=' * 50}")
    print(f"Generated Products: {total:,}")
    print(f"  Unique names:     {unique:,} ({unique/total*100:.1f}%)")
    print(f"  Inactive:         {inactive:,} ({inactive/total*100:.1f}%)")
    print(f"  Out of stock:     {out_stock:,} ({out_stock/total*100:.1f}%)")
    print(f"  Source DB IDs:    {sorted(db_ids)}")
    print(f"{'=' * 50}\n")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate test product data")
    parser.add_argument("--count",   type=int, default=10_000, help="Total products to generate")
    parser.add_argument("--db",      type=str, default="",     help="Target SQLite DB path")
    parser.add_argument("--dbs",     type=str, default="1",    help="Comma-separated DB IDs, e.g. 1,2,3")
    parser.add_argument("--dry-run", action="store_true",       help="Print stats only, do not write")
    args = parser.parse_args()

    db_ids    = [int(x) for x in args.dbs.split(",")]
    per_db    = args.count // len(db_ids)
    brands    = generate_brands()
    categories = generate_categories()

    all_products: List[Dict] = []
    for db_id in db_ids:
        print(f"Generating {per_db:,} products for DB {db_id}…")
        products = generate_products(per_db, source_db_id=db_id)
        all_products.extend(products)

    _print_stats(all_products)

    if args.dry_run:
        print("[dry-run] Not writing to database.")
        return

    if not args.db:
        print("Error: --db is required unless --dry-run is set.")
        sys.exit(1)

    if not os.path.isfile(args.db):
        print(f"Error: database file not found: {args.db}")
        sys.exit(1)

    print(f"Writing to {args.db}…")
    t0 = time.perf_counter()
    insert_into_db(args.db, all_products, brands, categories)
    elapsed = time.perf_counter() - t0
    print(f"Done in {elapsed:.2f}s. Inserted {len(all_products):,} products.")


if __name__ == "__main__":
    main()
