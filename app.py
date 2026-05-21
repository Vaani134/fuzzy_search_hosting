"""
app.py
------
Flask application — entry point.

Routes
------
  GET  /                        — dashboard
  GET  /search                  — search UI (with pagination, filters, sort)
  GET  /product/<id>            — product detail page
  GET  /sync                    — sync management page
  GET  /settings                — database settings page

  -- Search API (routes/search_routes.py) --
  GET  /api/search              — paginated, filtered, sorted fuzzy search
  GET  /api/search/history      — recent search queries (with top_score)
  GET  /api/search/top          — most-frequent queries
  GET  /api/search/zero-results — queries that returned no results
  GET  /api/search/trending     — trending queries (last N hours)
  POST /api/search/rebuild      — rebuild in-memory index + clear cache
  GET  /api/autocomplete        — autocomplete suggestions
  GET  /api/cache/stats         — cache statistics (backend: redis|memory)
  POST /api/cache/clear         — clear search cache

  -- Synonym management (routes/synonym_routes.py) --
  GET    /api/synonyms                  — list all active synonyms
  POST   /api/synonyms/add             — add a synonym pair manually
  DELETE /api/synonyms/<id>            — delete an active synonym
  POST   /api/synonyms/suggest         — run AI synonym suggester
  GET    /api/synonyms/suggestions     — list pending suggestions
  GET    /api/synonyms/suggestions/all — list all suggestions (any status)
  POST   /api/synonyms/approve/<id>    — approve → live immediately
  POST   /api/synonyms/reject/<id>     — reject a suggestion

  -- Image search (routes/image_search_routes.py) --
  POST /api/image-search        — search by uploaded image

  -- Other API --
  GET  /api/product/<id>        — product detail JSON
  POST /api/product/<id>/click  — record a click-through (updates ranking)
  POST /api/sync                — trigger MySQL → SQLite sync (legacy, syncs DB id=1)
  GET  /api/sync/live           — real-time sync progress (legacy, DB id=1)
  GET  /api/sync/history        — sync log history
  GET  /api/sync/status         — latest sync status per table
  GET  /api/stats               — engine, DB, and cache stats
  POST /api/download-zip        — download product images as ZIP
  GET  /api/settings            — get DB settings (password masked)
  POST /api/settings            — save DB settings
  POST /api/settings/test       — test DB connection

  -- Multi-database management (new) --
  GET    /api/databases                  — list all connected databases
  POST   /api/databases                  — add a new connected database
  GET    /api/database/<id>              — get one database (password masked)
  PUT    /api/database/<id>              — update connection credentials
  DELETE /api/database/<id>             — remove a connected database
  POST   /api/database/<id>/sync        — start sync (full or incremental)
  POST   /api/database/<id>/stop        — request graceful stop
  GET    /api/database/<id>/status      — live state + last job + table log
  GET    /api/database/<id>/logs        — in-memory sync log buffer (incremental ?since=)
  GET    /api/database/<id>/errors      — recent row-level sync failures
  POST   /api/database/<id>/test        — test MySQL connection
"""

import os
import sys
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify, abort, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SECRET_KEY, DEBUG, HOST, PORT, SEARCH_DEFAULT_K, SYNC_TABLES
from db.database import init_db, get_connection, dict_from_row, seed_primary_database_from_settings
from modules.fuzzy_search import get_engine, get_global_engine, apply_synonyms
from modules.sync import sync_all_background, sync_table, get_sync_status, get_sync_history, get_live_state
from modules.autocomplete import get_suggestions
from modules.zip_builder import build_zip
from modules.settings_manager import (
    load as load_settings,
    save as save_settings,
    test_connection as test_connection_legacy,
    get_mysql_config,
)
from modules.db_manager import (
    list_databases,
    get_database_masked,
    add_database,
    update_database,
    delete_database,
    update_sync_status,
    test_connection as test_db_connection,
)
from modules.sync_manager import (
    sync_database_background,
    request_stop,
    get_live_state as get_db_live_state,
    get_all_live_states,
    get_database_status,
    get_sync_logs,
    get_sync_errors,
    get_active_job,
)
from routes.search_routes import search_bp
from routes.image_search_routes import image_search_bp
from routes.synonym_routes import synonym_bp

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Register search blueprint (all /api/search*, /api/autocomplete, /api/cache/*)
app.register_blueprint(search_bp)
# Register image search blueprint (/api/image-search)
app.register_blueprint(image_search_bp)
# Register synonym management blueprint (/api/synonyms*)
app.register_blueprint(synonym_bp)


# ── Jinja filter: resolve product image path → full URL ───────────────────────
def _get_image_base_for_db(source_db_id: int) -> str:
    """Return configured image base URL for a source database."""
    if not source_db_id:
        return ""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT image_base_url FROM connected_databases WHERE id = ?",
            (int(source_db_id),),
        ).fetchone()
    finally:
        conn.close()
    image_base_url = (row["image_base_url"] if row else "") or ""
    image_base_url = image_base_url.strip().rstrip("/")
    return image_base_url


@app.template_filter("img_url")
def img_url_filter(path: str, source_db_id: int = None) -> str:
    """
    Convert a DB image path to a full URL.

    Real DB values observed:
      image      column: /uploads/img/chinahosmall_p1.jpg  ← has /uploads/
      main_image column: /img/chinahosmall_p1.jpg          ← missing /uploads/

    Both must resolve to:
      https://novxcloud.com/uploads/img/<filename>
    """
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path

    clean = path.lstrip("/")

    base = _get_image_base_for_db(source_db_id)

    if base:
        if clean.startswith("uploads/"):
            return f"{base}/{clean}"
        if clean.startswith("img/"):
            return f"{base}/uploads/{clean}"
        if "/" not in clean:
            return f"{base}/uploads/img/{clean}"
        return f"{base}/{clean}"

    # No configured base URL for this DB: return path as-is (relative) so
    # there is no hardcoded cloud host fallback.
    if clean.startswith("uploads/"):
        return f"/{clean}"
    if clean.startswith("img/"):
        return f"/uploads/{clean}"
    if "/" not in clean:
        return f"/uploads/img/{clean}"
    return f"/{clean}"


# Initialise SQLite schema on startup (also creates search_history + synonyms tables)
init_db()

# One-time migration: import db_settings.json → connected_databases table (id=1).
# Idempotent — does nothing if connected_databases already has rows.
seed_primary_database_from_settings()


def _cleanup_stale_running_sync_jobs() -> None:
    """
    Mark orphaned `running` jobs as failed on app startup.

    Any process restart clears in-memory live sync state, so rows left in
    sync_jobs with status='running' cannot still be truly running.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        stale_rows = conn.execute(
            "SELECT id, database_id FROM sync_jobs WHERE status = 'running'"
        ).fetchall()
        if not stale_rows:
            return

        stale_ids = [row["id"] for row in stale_rows]
        db_ids = sorted({row["database_id"] for row in stale_rows})
        placeholders = ",".join("?" for _ in stale_ids)

        conn.execute(
            f"UPDATE sync_jobs "
            f"SET status = 'failed', finished_at = ?, "
            f"error_msg = COALESCE(error_msg, 'App restarted while sync was running') "
            f"WHERE id IN ({placeholders})",
            (now, *stale_ids),
        )
        db_placeholders = ",".join("?" for _ in db_ids)
        conn.execute(
            f"""
            UPDATE connected_databases
            SET sync_status = ?, updated_at = ?
            WHERE id IN ({db_placeholders})
            """,
            ("error", now, *db_ids),
        )
        conn.commit()
        print(
            f"[startup] Marked {len(stale_ids)} stale running sync job(s) as failed."
        )
    finally:
        conn.close()


_cleanup_stale_running_sync_jobs()

# Reload synonyms from DB now that the table is guaranteed to exist.
# The first call at module import time may have found an empty DB.
from modules.fuzzy_search import reload_synonyms as _reload_synonyms
_reload_synonyms()

# ── Engine initialisation ──────────────────────────────────────────────────────
# Background automatic index rebuilding is DISABLED for this environment.
# We are relying on fully manual index rebuilds via the API.
# Use POST /api/search/rebuild to refresh the in-memory index.
engine = get_engine(source_db_id=1, rebuild_interval=None)


# ── UI Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    """Dashboard — counts, top categories, recent sync status."""
    conn = get_connection()
    try:
        total_products     = conn.execute(
            "SELECT COUNT(*) FROM products WHERE is_inactive=0"
        ).fetchone()[0]
        total_all_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        total_categories   = conn.execute("SELECT COUNT(*) FROM categories WHERE deleted_at IS NULL").fetchone()[0]
        total_brands       = conn.execute("SELECT COUNT(*) FROM brands WHERE deleted_at IS NULL").fetchone()[0]
        total_groups       = conn.execute("SELECT COUNT(*) FROM product_group").fetchone()[0]
        total_transactions = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        out_of_stock       = conn.execute(
            "SELECT COUNT(*) FROM products WHERE out_of_stock=1 AND is_inactive=0"
        ).fetchone()[0]
        not_for_selling    = conn.execute(
            "SELECT COUNT(*) FROM products WHERE not_for_selling=1 AND is_inactive=0"
        ).fetchone()[0]
        inactive_products  = conn.execute(
            "SELECT COUNT(*) FROM products WHERE is_inactive=1"
        ).fetchone()[0]

        # Top 10 categories by product count
        top_categories = conn.execute(
            """
            SELECT c.name, COUNT(p.id) AS product_count
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id
                AND p.is_inactive = 0 AND p.not_for_selling = 0
            WHERE c.deleted_at IS NULL
            GROUP BY c.id, c.name
            ORDER BY product_count DESC
            LIMIT 10
            """
        ).fetchall()

        # Top 10 brands by product count
        top_brands = conn.execute(
            """
            SELECT b.name, COUNT(p.id) AS product_count
            FROM brands b
            LEFT JOIN products p ON p.brand_id = b.id
                AND p.is_inactive = 0 AND p.not_for_selling = 0
            WHERE b.deleted_at IS NULL
            GROUP BY b.id, b.name
            ORDER BY product_count DESC
            LIMIT 10
            """
        ).fetchall()

        # Recent transactions (last 5)
        recent_transactions = conn.execute(
            """
            SELECT id, invoice_no, type, status, payment_status,
                   final_total, transaction_date
            FROM transactions
            ORDER BY id DESC
            LIMIT 5
            """
        ).fetchall()

    finally:
        conn.close()

    sync_status = get_sync_status()
    eng_stats   = engine.stats()

    return render_template(
        "dashboard.html",
        total_products=total_products,
        total_all_products=total_all_products,
        total_categories=total_categories,
        total_brands=total_brands,
        total_groups=total_groups,
        total_transactions=total_transactions,
        out_of_stock=out_of_stock,
        not_for_selling=not_for_selling,
        inactive_products=inactive_products,
        top_categories=[dict(r) for r in top_categories],
        top_brands=[dict(r) for r in top_brands],
        recent_transactions=[dict(r) for r in recent_transactions],
        sync_status=sync_status,
        eng_stats=eng_stats,
        now=datetime.now(),
    )


@app.route("/search")
def index():
    """
    Main search page — supports pagination, filters, and sort.
    Passes all parameters to the template for UI rendering.
    """
    db_raw = (request.args.get("db_id", "1") or "1").strip().lower()
    global_flag = (request.args.get("global", "") or "").strip().lower() in ("1", "true", "yes")
    is_global = db_raw == "all" or global_flag
    db_id = None if is_global else max(1, int(db_raw or 1))
    query    = request.args.get("q", "").strip()
    page     = max(1, int(request.args.get("page", 1) or 1))
    limit    = min(max(1, int(request.args.get("limit", SEARCH_DEFAULT_K) or SEARCH_DEFAULT_K)), 100)
    sort     = request.args.get("sort", "score").strip()
    category = request.args.get("category", "").strip()
    min_price = request.args.get("min_price", "").strip()
    max_price = request.args.get("max_price", "").strip()

    results      = []
    total_results = 0
    total_pages   = 1
    search_engine = get_global_engine() if is_global else get_engine(source_db_id=db_id)
    stats         = search_engine.stats()

    if query:
        import math
        filters = {}
        if category:
            filters["category"] = category
        if min_price:
            try:
                filters["min_price"] = float(min_price)
            except ValueError:
                pass
        if max_price:
            try:
                filters["max_price"] = float(max_price)
            except ValueError:
                pass

        all_results = search_engine.search_with_field_scores(
            query, top_k=100, filters=filters if filters else None
        )

        # Sort
        if sort == "name":
            all_results.sort(key=lambda r: (r.get("name") or "").lower())

        total_results = len(all_results)
        total_pages   = max(1, math.ceil(total_results / limit))
        page          = min(page, total_pages)
        start         = (page - 1) * limit
        results       = all_results[start:start + limit]

        # "Did You Mean" suggestion
        from modules.fuzzy_search import get_query_suggestion
        top_names  = [r["name"] for r in all_results[:200] if r.get("name")]
        top_score  = all_results[0]["score"] if all_results else 0.0
        suggestion = get_query_suggestion(query, choices=top_names, top_result_score=top_score)

        # Log search — pass top_score so the synonym suggester can detect
        # low-confidence queries (e.g. "grdiner" scores ~60, not zero results)
        from modules.analytics import log_search
        log_search(query, total_results, top_score)

    else:
        suggestion = None

    databases = list_databases()
    if (not is_global) and databases and not any(d["id"] == db_id for d in databases):
        db_id = databases[0]["id"]
        search_engine = get_engine(source_db_id=db_id)
        stats = search_engine.stats()

    # Fetch distinct categories for the filter dropdown
    conn = get_connection()
    try:
        if is_global:
            category_rows = conn.execute(
                """
                SELECT DISTINCT c.name
                FROM categories c
                INNER JOIN products p ON p.category_id = c.id AND p.is_inactive = 0
                WHERE c.deleted_at IS NULL
                ORDER BY c.name
                """
            ).fetchall()
        else:
            category_rows = conn.execute(
                """
                SELECT DISTINCT c.name
                FROM categories c
                INNER JOIN products p ON p.category_id = c.id AND p.is_inactive = 0
                WHERE c.deleted_at IS NULL AND p.source_db_id = ?
                ORDER BY c.name
                """
                ,
                (db_id,),
            ).fetchall()
        categories = [r["name"] for r in category_rows]
    finally:
        conn.close()

    return render_template(
        "index.html",
        query=query,
        db_id=("all" if is_global else db_id),
        is_global=is_global,
        databases=databases,
        results=results,
        stats=stats,
        top_k=limit,
        # Pagination
        page=page,
        limit=limit,
        total_results=total_results,
        total_pages=total_pages,
        # Filters & sort
        sort=sort,
        category=category,
        min_price=min_price,
        max_price=max_price,
        categories=categories,
        suggestion=suggestion,
        now=datetime.now(),
    )


@app.route("/product/<int:product_id>")
def product_detail(product_id: int):
    """Product detail page."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT p.*,
                   COALESCE(b.name, '')  AS brand_name,
                   COALESCE(c.name, '')  AS category_name,
                   COALESCE(pg.name, '') AS group_name
            FROM products p
            LEFT JOIN brands        b  ON b.id  = p.brand_id
            LEFT JOIN categories    c  ON c.id  = p.category_id
            LEFT JOIN product_group pg ON pg.id = p.product_group_id
            WHERE p.id = ?
            """,
            (product_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    product = dict_from_row(row)
    return render_template("product.html", product=product)


@app.route("/product/<int:db_id>/<int:product_id>")
def product_detail_scoped(db_id: int, product_id: int):
    """Product detail page using source database id + source product id."""
    scoped_id = db_id * 1_000_000_000 + product_id
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT p.*,
                   COALESCE(b.name, '')  AS brand_name,
                   COALESCE(c.name, '')  AS category_name,
                   COALESCE(pg.name, '') AS group_name
            FROM products p
            LEFT JOIN brands        b  ON b.id  = p.brand_id
            LEFT JOIN categories    c  ON c.id  = p.category_id
            LEFT JOIN product_group pg ON pg.id = p.product_group_id
            WHERE p.id = ? AND p.source_db_id = ?
            """,
            (scoped_id, db_id),
        ).fetchone()
        if not row:
            # Backward-compat fallback: handle rows that still use unscoped IDs.
            row = conn.execute(
                """
                SELECT p.*,
                       COALESCE(b.name, '')  AS brand_name,
                       COALESCE(c.name, '')  AS category_name,
                       COALESCE(pg.name, '') AS group_name
                FROM products p
                LEFT JOIN brands        b  ON b.id  = p.brand_id
                LEFT JOIN categories    c  ON c.id  = p.category_id
                LEFT JOIN product_group pg ON pg.id = p.product_group_id
                WHERE p.id = ? AND p.source_db_id = ?
                """,
                (product_id, db_id),
            ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    product = dict_from_row(row)
    return render_template("product.html", product=product)


@app.route("/sync")
def sync_page():
    """Sync management page."""
    status = get_sync_status()
    return render_template("sync.html", sync_status=status, now=datetime.now())


# ── JSON API ───────────────────────────────────────────────────────────────────

@app.route("/api/product/<int:product_id>")
def api_product(product_id: int):
    """GET /api/product/<id> — product detail JSON."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT p.*,
                   COALESCE(b.name, '')  AS brand_name,
                   COALESCE(c.name, '')  AS category_name,
                   COALESCE(pg.name, '') AS group_name
            FROM products p
            LEFT JOIN brands        b  ON b.id  = p.brand_id
            LEFT JOIN categories    c  ON c.id  = p.category_id
            LEFT JOIN product_group pg ON pg.id = p.product_group_id
            WHERE p.id = ?
            """,
            (product_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "Product not found"}), 404

    return jsonify(dict_from_row(row))


@app.route("/api/product/<int:product_id>/click", methods=["POST"])
def api_product_click(product_id: int):
    """
    POST /api/product/<id>/click

    Record a click-through on a product from search results.
    Increments the click_count in product_clicks and updates updated_at.

    Uses INSERT OR REPLACE (upsert) so the first click creates the row
    and subsequent clicks increment the counter atomically.

    Called automatically by the frontend when a user opens a product
    detail page from the search results grid.

    Response
    --------
    { "status": "ok", "product_id": 101, "click_count": 5 }
    """
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc).isoformat()

    conn = get_connection()
    try:
        # Verify the product exists
        exists = conn.execute(
            "SELECT id FROM products WHERE id = ? AND is_inactive = 0",
            (product_id,),
        ).fetchone()
        if not exists:
            return jsonify({"error": "Product not found"}), 404

        # Upsert: insert on first click, increment on subsequent clicks
        conn.execute(
            """
            INSERT INTO product_clicks (product_id, click_count, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                click_count = click_count + 1,
                updated_at  = excluded.updated_at
            """,
            (product_id, now),
        )
        conn.commit()

        new_count = conn.execute(
            "SELECT click_count FROM product_clicks WHERE product_id = ?",
            (product_id,),
        ).fetchone()["click_count"]

    finally:
        conn.close()

    return jsonify({
        "status":     "ok",
        "product_id": product_id,
        "click_count": new_count,
    })


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """
    POST /api/sync
    Body (JSON, optional): {"full": true, "tables": ["products"]}

    Starts sync in a background thread and returns immediately.
    Poll GET /api/sync/live for real-time progress.
    
    ⚠️ SECURITY NOTE: This endpoint is publicly accessible.
    For production, consider adding API token authentication.
    See DEPLOYMENT_SECURITY.md for details.
    """
    from modules.sync import get_live_state as _live
    body   = request.get_json(silent=True) or {}
    full   = body.get("full", True)
    tables = body.get("tables", None)

    # Reject if already running
    state = _live()
    if state["running"]:
        return jsonify({"status": "already_running",
                        "message": "A sync is already in progress."}), 409

    def _after_sync(results):
        get_engine(source_db_id=1).rebuild()
        from modules.cache import search_cache as _cache
        _cache.clear()

    if tables:
        valid   = [t for t in tables if t in SYNC_TABLES]
        results = [sync_table(t, full=full) for t in valid]
        get_engine(source_db_id=1).rebuild()
        from modules.cache import search_cache as _cache
        _cache.clear()
        return jsonify({"status": "ok", "results": results,
                        "indexed": get_engine(source_db_id=1).stats()["total_products"]})

    sync_all_background(full=full, callback=_after_sync)
    return jsonify({"status": "started",
                    "message": "Sync started in background. Poll /api/sync/live for progress."})


@app.route("/api/sync/live")
def api_sync_live():
    """GET /api/sync/live — real-time sync progress (poll every 2s)."""
    return jsonify(get_live_state())


@app.route("/api/sync/history")
def api_sync_history():
    """GET /api/sync/history?limit=50 — full sync log history, newest first."""
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify(get_sync_history(limit=limit))


@app.route("/api/sync/status")
def api_sync_status():
    """GET /api/sync/status — latest sync log per table."""
    return jsonify(get_sync_status())


@app.route("/api/stats")
def api_stats():
    """GET /api/stats — engine and DB stats."""
    db_id = max(1, int(request.args.get("db_id", 1) or 1))
    stats = get_engine(source_db_id=db_id).stats()
    conn  = get_connection()
    try:
        product_count  = conn.execute(
            "SELECT COUNT(*) FROM products WHERE is_inactive=0 AND source_db_id=?",
            (db_id,),
        ).fetchone()[0]
        brand_count    = conn.execute(
            "SELECT COUNT(*) FROM brands WHERE source_db_id=?",
            (db_id,),
        ).fetchone()[0]
        category_count = conn.execute(
            "SELECT COUNT(*) FROM categories WHERE source_db_id=?",
            (db_id,),
        ).fetchone()[0]
        search_count   = conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    finally:
        conn.close()

    from modules.cache import search_cache as _cache
    return jsonify({
        **stats,
        "db_id":         db_id,
        "db_products":    product_count,
        "db_brands":      brand_count,
        "db_categories":  category_count,
        "total_searches": search_count,
        "cache":          {**_cache.stats(), "backend": _cache.backend_name},
    })


# ── ZIP Download API ───────────────────────────────────────────────────────────

@app.route("/api/download-zip", methods=["POST"])
def api_download_zip():
    """
    POST /api/download-zip
    Body: {"product_ids": [1, 2, 3, ...]}

    Single product  → proxies the image file directly (no ZIP overhead).
    Multiple products → builds and returns a ZIP.
    """
    body        = request.get_json(silent=True) or {}
    product_ids = body.get("product_ids", [])

    if not product_ids:
        return jsonify({"error": "product_ids is required"}), 400

    if len(product_ids) > 200:
        return jsonify({"error": "Maximum 200 products per download"}), 400

    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(product_ids))
        rows = conn.execute(
            f"SELECT id, name, image, main_image FROM products WHERE id IN ({placeholders})",
            product_ids,
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return jsonify({"error": "No products found"}), 404

    products = [dict(r) for r in rows]

    # ── Single image — proxy directly ─────────────────────────────────────────
    if len(products) == 1:
        from modules.zip_builder import resolve_image_url
        import urllib.request as urlreq
        import urllib.error
        import io
        import re

        p   = products[0]
        raw = p.get("main_image") or p.get("image") or ""
        url = resolve_image_url(raw)

        if not url:
            return jsonify({"error": "No image available for this product"}), 404

        try:
            req = urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0 FuzzySearch/1.0"})
            with urlreq.urlopen(req, timeout=10) as resp:
                data  = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")

            ext = os.path.splitext(url.split("?")[0])[-1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                ext = ".jpg"
            safe_name = re.sub(r'[^\w\s-]', '', p.get("name", "image")).strip()
            safe_name = re.sub(r'\s+', '_', safe_name)[:80]
            filename  = f"{safe_name}{ext}"

            return send_file(
                io.BytesIO(data),
                mimetype=ctype,
                as_attachment=True,
                download_name=filename,
            )

        except urllib.error.HTTPError as e:
            return jsonify({"error": f"Image fetch failed: HTTP {e.code}"}), 502
        except Exception as e:
            return jsonify({"error": f"Image fetch failed: {e}"}), 502

    # ── Multiple images — build ZIP ────────────────────────────────────────────
    zip_buffer, stats = build_zip(products)

    if stats["downloaded"] == 0:
        return jsonify({"error": "No images could be downloaded", "stats": stats}), 422

    filename = f"product_images_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


# ── Settings page ─────────────────────────────────────────────────────────────

@app.route("/settings")
def settings_page():
    """Database configuration page."""
    return render_template("settings.html", settings=load_settings())


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """GET /api/settings — return current DB settings (password masked)."""
    s = load_settings()
    s["password"] = "••••••••" if s["password"] else ""
    return jsonify(s)


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """
    POST /api/settings
    Body: {host, port, database, user, password}
    """
    body    = request.get_json(silent=True) or {}
    current = load_settings()

    new_settings = {
        "host":     body.get("host",     current["host"]).strip(),
        "port":     str(body.get("port", current["port"])).strip(),
        "database": body.get("database", current["database"]).strip(),
        "user":     body.get("user",     current["user"]).strip(),
        "password": body.get("password", current["password"]),
    }

    if new_settings["password"] in ("••••••••", ""):
        if body.get("password", "") in ("••••••••", ""):
            new_settings["password"] = current["password"]

    save_settings(new_settings)
    return jsonify({"status": "ok", "message": "Settings saved successfully."})


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
    """POST /api/settings/test — test connection without saving."""
    body    = request.get_json(silent=True) or {}
    current = load_settings()

    host     = body.get("host",     current["host"]).strip()
    port     = int(body.get("port", current["port"]) or 3306)
    database = body.get("database", current["database"]).strip()
    user     = body.get("user",     current["user"]).strip()
    password = body.get("password", current["password"])

    if password in ("••••••••",):
        password = current["password"]

    result = test_connection_legacy(host, port, user, password, database)
    return jsonify(result)


# ── Multi-database management API ─────────────────────────────────────────────
#
# These routes manage the connected_databases table that replaces the flat
# db_settings.json approach.  All existing /api/settings routes continue to
# work unchanged — they now read/write connected_databases id=1 via the
# legacy settings_manager, keeping full backward compatibility.


@app.route("/api/databases", methods=["GET"])
def api_list_databases():
    """
    GET /api/databases
    Return all connected databases.  Passwords are masked in the response.
    """
    return jsonify(list_databases())


@app.route("/api/databases", methods=["POST"])
def api_add_database():
    """
    POST /api/databases
    Body: {name, host, port, username, password, database_name, image_base_url}
    Add a new ERP database connection.  Returns the created record (id included).
    """
    body = request.get_json(silent=True) or {}
    required = ("name", "host", "database_name")
    missing = [f for f in required if not body.get(f, "").strip()]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        new_id = add_database(
            name          = body["name"].strip(),
            host          = body.get("host", "").strip(),
            port          = int(body.get("port", 3306) or 3306),
            username      = body.get("username", "").strip(),
            password      = body.get("password", ""),
            database_name = body["database_name"].strip(),
            image_base_url = body.get("image_base_url", "").strip(),
        )
        db = get_database_masked(new_id)
        return jsonify({"status": "ok", "database": db}), 201
    except (ValueError, Exception) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/database/<int:db_id>", methods=["GET"])
def api_get_database(db_id: int):
    """GET /api/database/<id> — return one connected database (password masked)."""
    db = get_database_masked(db_id)
    if not db:
        return jsonify({"error": "Database not found"}), 404
    return jsonify(db)


@app.route("/api/database/<int:db_id>", methods=["PUT"])
def api_update_database(db_id: int):
    """
    PUT /api/database/<id>
    Body: any subset of {name, host, port, username, password, database_name, image_base_url}
    Update connection credentials for an existing database.
    Send password as blank string to leave it unchanged.
    """
    body = request.get_json(silent=True) or {}

    # If password is blank or the masked sentinel, keep existing value
    if body.get("password", "") in ("", "••••••••"):
        body.pop("password", None)

    updated = update_database(db_id, **body)
    if not updated:
        return jsonify({"error": "Database not found"}), 404

    return jsonify({"status": "ok", "database": get_database_masked(db_id)})


@app.route("/api/database/<int:db_id>", methods=["DELETE"])
def api_delete_database(db_id: int):
    """
    DELETE /api/database/<id>
    Remove a connected database.  Fails if a sync is currently running.
    """
    try:
        removed = delete_database(db_id)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409

    if not removed:
        return jsonify({"error": "Database not found"}), 404

    return jsonify({"status": "ok", "message": f"Database {db_id} deleted."})


@app.route("/api/database/<int:db_id>/test", methods=["POST"])
def api_test_database(db_id: int):
    """POST /api/database/<id>/test — test the MySQL connection for this database."""
    result = test_db_connection(db_id)
    return jsonify(result)


@app.route("/api/database/<int:db_id>/sync", methods=["POST"])
def api_database_sync(db_id: int):
    """
    POST /api/database/<id>/sync
    Body (JSON, optional): {"full": true|false}

    Start a sync for one connected database in a background thread.
    - full=true  : delete this source's rows and reimport everything.
    - full=false : only fetch rows updated since last_sync_at (incremental).
    - omitted    : auto-detect (full on first run, incremental thereafter).

    Returns immediately. Poll GET /api/database/<id>/status for progress.
    """
    db = get_database_masked(db_id)
    if not db:
        return jsonify({"error": "Database not found"}), 404

    # Reject if this database already has a running sync in DB or memory
    active_job = get_active_job(db_id)
    if active_job:
        return jsonify({
            "status": "already_running",
            "message": f"Database {db_id} already has a running sync job "
                       f"(job_id={active_job['id']}). "
                       "Use POST /api/database/<id>/stop to request stop.",
        }), 409

    live = get_db_live_state(db_id)
    if live.get("running"):
        return jsonify({
            "status":  "already_running",
            "message": f"Database {db_id} already has an active sync. "
                       "Use POST /api/database/<id>/stop to cancel it.",
        }), 409

    body = request.get_json(silent=True) or {}
    full = body.get("full", None)   # None → auto-detect in sync_manager

    sync_database_background(db_id=db_id, full=full)
    return jsonify({
        "status":  "started",
        "db_id":   db_id,
        "message": f"Sync started for database '{db['name']}'. "
                   f"Poll /api/database/{db_id}/status for progress.",
    })


@app.route("/api/database/<int:db_id>/stop", methods=["POST"])
def api_database_stop(db_id: int):
    """
    POST /api/database/<id>/stop
    Request a graceful stop for the running sync on this database.
    The sync thread will exit after its current batch — no force-kill.
    """
    result = request_stop(db_id)
    status_code = 200 if result["ok"] else 404
    return jsonify(result), status_code


@app.route("/api/database/<int:db_id>/status", methods=["GET"])
def api_database_status(db_id: int):
    """
    GET /api/database/<id>/status
    Return combined sync status for one database:
      - live:         in-memory real-time state (updated after every batch)
      - last_job:     most recent sync_jobs row
      - table_status: latest sync_log entry per table
    """
    db = get_database_masked(db_id)
    if not db:
        return jsonify({"error": "Database not found"}), 404

    status = get_database_status(db_id)
    status["database"] = db
    return jsonify(status)


@app.route("/api/database/<int:db_id>/logs", methods=["GET"])
def api_database_logs(db_id: int):
    """
    GET /api/database/<id>/logs[?since=<iso-ts>]
    Return the in-memory live log buffer for a database.
    Pass ?since=<ISO-8601 timestamp> to get only entries after that point
    (useful for incremental polling without re-processing old entries).
    """
    if not get_database_masked(db_id):
        return jsonify({"error": "Database not found"}), 404
    since = request.args.get("since")
    logs  = get_sync_logs(db_id, since_ts=since)
    return jsonify({"db_id": db_id, "logs": logs, "count": len(logs)})


@app.route("/api/database/<int:db_id>/errors", methods=["GET"])
def api_database_errors(db_id: int):
    """
    GET /api/database/<id>/errors[?limit=100]
    Return recent row-level sync errors from the sync_errors table.
    """
    if not get_database_masked(db_id):
        return jsonify({"error": "Database not found"}), 404
    limit  = min(int(request.args.get("limit", 100)), 500)
    errors = get_sync_errors(db_id, limit=limit)
    return jsonify({"db_id": db_id, "errors": errors, "count": len(errors)})


@app.route("/api/databases/live", methods=["GET"])
def api_all_databases_live():
    """
    GET /api/databases/live
    Return in-memory live sync state for ALL connected databases.
    Useful for the settings page to show running-indicator badges.
    """
    return jsonify(get_all_live_states())


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    if request.path == "/favicon.ico":
        return "", 204   # No Content — silences browser favicon requests
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return render_template("500.html"), 500


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)
