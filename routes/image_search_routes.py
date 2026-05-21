"""
routes/image_search_routes.py
------------------------------
API endpoint for image-based product search.

Endpoint
--------
  POST /api/image-search

Request
-------
  Content-Type: multipart/form-data
    file      : image file — JPEG, PNG, WEBP, or GIF  (field name "file" OR "image")
    top_k     : (optional) max results to return, default 20, max 100

  OR Content-Type: application/octet-stream
    Raw image bytes in the request body.
    Pass the original filename via the X-Filename header if available.

Response (200 OK)
-----------------
  {
    "query_generated": "hookah glass pipe",
    "labels":          ["hookah", "tobacco_shop", "glass"],
    "extractor_used":  "tensorflow" | "pytorch" | "heuristic",
    "result_count":    12,
    "results": [
      {
        "id": 101,
        "name": "China Hookah Small",
        "score": 87.5,
        "score_label": "high",
        ...
      }
    ]
  }

Error responses
---------------
  400  — no image provided, or file is not a recognised image format
  413  — image exceeds the 10 MB size limit
  500  — unexpected server error

Notes
-----
  • This blueprint is completely independent of search_routes.py.
  • It does NOT modify fuzzy_search.py — it only calls get_engine().search().
  • The extractor_used field tells the caller which label extraction path ran
    (tensorflow / pytorch / heuristic), useful for debugging.
"""

import io
from flask import Blueprint, request, jsonify

from modules.image_search import search_by_image

image_search_bp = Blueprint("image_search", __name__)

# Maximum accepted image size: 10 MB
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Accepted MIME types (checked against Content-Type and magic bytes)
_ACCEPTED_MIME_PREFIXES = ("image/jpeg", "image/png", "image/webp", "image/gif")

# Magic byte signatures for the accepted formats
_MAGIC_BYTES: dict = {
    b"\xff\xd8\xff":    "image/jpeg",
    b"\x89PNG\r\n":     "image/png",
    b"RIFF":            "image/webp",   # RIFF....WEBP
    b"GIF87a":          "image/gif",
    b"GIF89a":          "image/gif",
}


def _detect_image_type(data: bytes) -> str:
    """
    Detect image MIME type from magic bytes.
    Returns the MIME string or empty string if unrecognised.
    """
    for magic, mime in _MAGIC_BYTES.items():
        if data[:len(magic)] == magic:
            # Extra check for WEBP: bytes 8-12 must be "WEBP"
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return ""


@image_search_bp.route("/api/image-search", methods=["POST"])
def api_image_search():
    """
    POST /api/image-search

    Accepts an image via multipart/form-data (field name: "file") or as
    raw bytes in the request body.  Runs the image-to-search pipeline and
    returns fuzzy search results for the generated query.
    """
    image_bytes: bytes = b""
    filename:    str   = ""

    # ── Read image from request ───────────────────────────────────────────────
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        # Accept both "file" and "image" as the field name so the API works
        # regardless of which name the client uses.
        file = request.files.get("file") or request.files.get("image")
        if not file:
            return jsonify({
                "error": (
                    "No file provided. "
                    "Send an image in the 'file' or 'image' field."
                )
            }), 400
        filename    = file.filename or ""
        image_bytes = file.read()

    elif request.content_type and any(
        request.content_type.startswith(m) for m in _ACCEPTED_MIME_PREFIXES
    ):
        # Raw binary body (e.g. from a mobile app or curl --data-binary)
        image_bytes = request.get_data()
        filename    = request.headers.get("X-Filename", "")

    else:
        # Try reading raw body as a last resort (no Content-Type set)
        image_bytes = request.get_data()
        filename    = request.headers.get("X-Filename", "")
        if not image_bytes:
            return jsonify({
                "error": (
                    "No image received. Send a multipart/form-data request with "
                    "a 'file' field, or send raw image bytes with the appropriate "
                    "Content-Type header."
                )
            }), 400

    # ── Size guard ────────────────────────────────────────────────────────────
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        return jsonify({
            "error": f"Image too large. Maximum size is {_MAX_IMAGE_BYTES // (1024*1024)} MB."
        }), 413

    # ── Format validation via magic bytes ─────────────────────────────────────
    detected_mime = _detect_image_type(image_bytes)
    if not detected_mime:
        return jsonify({
            "error": (
                "Unrecognised image format. "
                "Accepted formats: JPEG, PNG, WEBP, GIF."
            )
        }), 400

    # ── Parse optional top_k parameter ───────────────────────────────────────
    try:
        top_k = int(request.form.get("top_k") or request.args.get("top_k") or 20)
        top_k = min(max(1, top_k), 100)
    except (ValueError, TypeError):
        top_k = 20
    db_raw = (request.form.get("db_id") or request.args.get("db_id") or "1").strip().lower()
    global_raw = (request.form.get("global") or request.args.get("global") or "").strip().lower()
    if db_raw == "all" or global_raw in ("1", "true", "yes"):
        source_db_id = None
    else:
        try:
            source_db_id = max(1, int(db_raw))
        except (ValueError, TypeError):
            source_db_id = 1

    # ── Run the image search pipeline ─────────────────────────────────────────
    try:
        result = search_by_image(
            image_bytes=image_bytes,
            filename=filename,
            top_k=top_k,
            source_db_id=source_db_id,
        )
    except Exception as exc:
        return jsonify({"error": f"Image search failed: {exc}"}), 500

    # ── Build response ────────────────────────────────────────────────────────
    # Match the required format: {"query_generated": "...", "results": [...]}
    # Extra fields (labels, extractor_used, result_count) are included for
    # transparency and debugging — they do not break any existing contract.
    return jsonify({
        "query_generated": result["query_generated"],
        "labels":          result["labels"],
        "extractor_used":  result["extractor_used"],
        "result_count":    result["result_count"],
        "results":         result["results"],
        # Surface non-fatal errors (e.g. ML model fallback) without failing
        **({"warning": result["error"]} if result.get("error") else {}),
    })
