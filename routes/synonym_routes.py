"""
routes/synonym_routes.py
------------------------
CRUD API for the synonyms table.

Endpoints
---------
  GET    /api/synonyms          — list all synonyms
  POST   /api/synonyms/add      — add a new synonym pair
  DELETE /api/synonyms/<id>     — delete a synonym by id

After every mutation (add / delete), reload_synonyms() is called so the
change takes effect immediately in apply_synonyms() — no server restart needed.

The search cache is also cleared after each mutation so cached results that
were computed with the old synonym set are not served.
"""

from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

from db.database import get_connection
from modules.fuzzy_search import reload_synonyms
from modules.cache import search_cache

synonym_bp = Blueprint("synonyms", __name__)


# ── GET /api/synonyms ──────────────────────────────────────────────────────────

@synonym_bp.route("/api/synonyms", methods=["GET"])
def api_list_synonyms():
    """
    GET /api/synonyms
    Returns all synonym pairs ordered alphabetically by variant.

    Response
    --------
    [
      { "id": 1, "variant": "hooka", "canonical": "hookah", "created_at": "..." },
      ...
    ]
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, variant, canonical, created_at "
            "FROM synonyms ORDER BY variant ASC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ── POST /api/synonyms/add ─────────────────────────────────────────────────────

@synonym_bp.route("/api/synonyms/add", methods=["POST"])
def api_add_synonym():
    """
    POST /api/synonyms/add
    Body (JSON): { "variant": "hooka", "canonical": "hookah" }

    Rules
    -----
    • Both fields are required and must be non-empty strings.
    • variant is stored lowercase and stripped.
    • canonical is stored lowercase and stripped.
    • variant must be unique — returns 409 if it already exists.
    • After insert, reload_synonyms() is called so the change is live
      immediately without a server restart.
    • The search cache is cleared so stale results are not served.

    Response (201)
    --------------
    { "status": "ok", "id": 42, "variant": "hooka", "canonical": "hookah",
      "synonyms_loaded": 25 }
    """
    body = request.get_json(silent=True) or {}

    variant   = str(body.get("variant",   "") or "").strip().lower()
    canonical = str(body.get("canonical", "") or "").strip().lower()

    # ── Validation ────────────────────────────────────────────────────────────
    if not variant:
        return jsonify({"error": "'variant' is required and must be non-empty."}), 400
    if not canonical:
        return jsonify({"error": "'canonical' is required and must be non-empty."}), 400
    if variant == canonical:
        return jsonify({"error": "'variant' and 'canonical' must be different."}), 400

    conn = get_connection()
    try:
        # Check for duplicate variant
        existing = conn.execute(
            "SELECT id, canonical FROM synonyms WHERE variant = ?", (variant,)
        ).fetchone()
        if existing:
            return jsonify({
                "error": f"Variant '{variant}' already maps to '{existing['canonical']}'. "
                         f"Delete id={existing['id']} first if you want to remap it.",
                "existing_id": existing["id"],
            }), 409

        # Insert
        cursor = conn.execute(
            "INSERT INTO synonyms (variant, canonical, created_at) VALUES (?, ?, ?)",
            (variant, canonical, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        new_id = cursor.lastrowid

    finally:
        conn.close()

    # ── Hot-reload synonyms + clear cache ─────────────────────────────────────
    count = reload_synonyms()
    search_cache.clear()

    return jsonify({
        "status":          "ok",
        "id":              new_id,
        "variant":         variant,
        "canonical":       canonical,
        "synonyms_loaded": count,
    }), 201


# ── DELETE /api/synonyms/<id> ──────────────────────────────────────────────────

@synonym_bp.route("/api/synonyms/<int:synonym_id>", methods=["DELETE"])
def api_delete_synonym(synonym_id: int):
    """
    DELETE /api/synonyms/<id>
    Removes the synonym with the given id.

    Returns 404 if the id does not exist.
    After deletion, reload_synonyms() is called and the cache is cleared.

    Response (200)
    --------------
    { "status": "ok", "deleted_id": 42, "synonyms_loaded": 24 }
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, variant, canonical FROM synonyms WHERE id = ?",
            (synonym_id,),
        ).fetchone()

        if not row:
            return jsonify({"error": f"Synonym id={synonym_id} not found."}), 404

        conn.execute("DELETE FROM synonyms WHERE id = ?", (synonym_id,))
        conn.commit()
        deleted = dict(row)

    finally:
        conn.close()

    # ── Hot-reload synonyms + clear cache ─────────────────────────────────────
    count = reload_synonyms()
    search_cache.clear()

    return jsonify({
        "status":          "ok",
        "deleted_id":      synonym_id,
        "deleted_variant": deleted["variant"],
        "synonyms_loaded": count,
    })
