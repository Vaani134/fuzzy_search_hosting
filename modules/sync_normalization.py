"""
modules/sync_normalization.py
-----------------------------
Row-level data cleaners for MySQL → SQLite sync.

Every ERP/MySQL row is run through a table-specific normalize_*_row()
function before being handed to the SQLite upsert layer.  The goal is
to guarantee that NOT NULL columns always receive a safe value even when
the upstream ERP sends NULL, empty bytes, or garbage data.

Design rules
  - Never raise.  Return a safe default on any error.
  - Never trust ERP data types: integers may arrive as strings, booleans
    as tinyint or None, datetimes as strings or datetime objects.
  - Strip leading/trailing whitespace from every string column.
  - Convert Decimal → float (PyMySQL returns Decimal for DECIMAL cols).
  - Convert datetime objects → ISO-8601 string (SQLite stores TEXT).
  - Convert bytes (rare but observed for BLOB/BINARY cols) → None.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional


# ── Primitive cleaners ────────────────────────────────────────────────────────

def clean_nullable_string(value: Any, default: str = "") -> str:
    """Return stripped string or *default* when value is None/empty."""
    if value is None:
        return default
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return default
    s = str(value).strip()
    return s if s else default


def clean_optional_string(value: Any) -> Optional[str]:
    """Return stripped string or None.  Empty string → None."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return None
    s = str(value).strip()
    return s if s else None


def clean_nullable_number(value: Any, default: float = 0.0) -> float:
    """Return float or *default* for None / unparseable values."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        try:
            return float(value)
        except Exception:
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_optional_number(value: Any) -> Optional[float]:
    """Return float or None for None / unparseable values."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        try:
            return float(value)
        except Exception:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_nullable_int(value: Any, default: int = 0) -> int:
    """Return int or *default* for None / unparseable values."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_optional_int(value: Any) -> Optional[int]:
    """Return int or None for None / unparseable values."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_nullable_bool(value: Any, default: int = 0) -> int:
    """Return 0 or 1 (SQLite BOOLEAN) or *default*.  Treats None as default."""
    if value is None:
        return default
    try:
        return 1 if int(value) else 0
    except (TypeError, ValueError):
        return default


def clean_nullable_datetime(value: Any) -> Optional[str]:
    """
    Return ISO-8601 string or None.

    Accepts:
      - datetime objects (PyMySQL returns these for DATETIME cols)
      - strings in any common format
      - None / zero-value datetime (0000-00-00 00:00:00)
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # Zero datetime from MySQL (0000-00-00 00:00:00) → None
        if value.year == 1 and value.month == 1 and value.day == 1:
            return None
        return value.isoformat(sep=" ")
    s = str(value).strip()
    if not s or s.startswith("0000-00-00"):
        return None
    # Already a string — pass through (SQLite stores TEXT anyway)
    return s


# ── Table-specific row normalizers ────────────────────────────────────────────

def normalize_brand_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize one MySQL brands row for SQLite upsert.

    NOT NULL cols in schema: id, business_id (DEFAULT 0), name, created_by (DEFAULT 0)
    """
    return {
        "id":          clean_nullable_int(row.get("id"), 0),
        "business_id": clean_nullable_int(row.get("business_id"), 0),
        "name":        clean_nullable_string(row.get("name"), "Unknown Brand"),
        "description": clean_optional_string(row.get("description")),
        "created_by":  clean_nullable_int(row.get("created_by"), 0),
        "deleted_at":  clean_nullable_datetime(row.get("deleted_at")),
        "created_at":  clean_nullable_datetime(row.get("created_at")),
        "updated_at":  clean_nullable_datetime(row.get("updated_at")),
    }


def normalize_category_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize one MySQL categories row for SQLite upsert.

    NOT NULL cols: id, name, business_id (DEFAULT 0), parent_id (DEFAULT 0),
                  created_by (DEFAULT 0)
    """
    return {
        "id":            clean_nullable_int(row.get("id"), 0),
        "name":          clean_nullable_string(row.get("name"), "Unknown Category"),
        "business_id":   clean_nullable_int(row.get("business_id"), 0),
        "short_code":    clean_optional_string(row.get("short_code")),
        "parent_id":     clean_nullable_int(row.get("parent_id"), 0),
        "created_by":    clean_nullable_int(row.get("created_by"), 0),
        "category_type": clean_optional_string(row.get("category_type")),
        "description":   clean_optional_string(row.get("description")),
        "slug":          clean_optional_string(row.get("slug")),
        "deleted_at":    clean_nullable_datetime(row.get("deleted_at")),
        "created_at":    clean_nullable_datetime(row.get("created_at")),
        "updated_at":    clean_nullable_datetime(row.get("updated_at")),
    }


def normalize_product_group_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize one MySQL product_group row for SQLite upsert.

    All cols except id are nullable in the schema.
    """
    return {
        "id":         clean_nullable_int(row.get("id"), 0),
        "name":       clean_optional_string(row.get("name")),
        "created_by": clean_optional_int(row.get("created_by")),
        "created_at": clean_nullable_datetime(row.get("created_at")),
        "updated_at": clean_nullable_datetime(row.get("updated_at")),
    }


def normalize_product_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize one MySQL products row for SQLite upsert.

    Critical NOT NULL cols that ERP may send as NULL:
      name, sku (DEFAULT ''), sku2 (DEFAULT ''), sku3 (DEFAULT ''),
      business_id (DEFAULT 0), enable_stock (DEFAULT 0),
      is_inactive (DEFAULT 0), not_for_selling (DEFAULT 0),
      out_of_stock (DEFAULT 0), created_by (DEFAULT 0), ml (DEFAULT 0.0)
    """
    return {
        "id":                    clean_nullable_int(row.get("id"), 0),
        "name":                  clean_nullable_string(row.get("name"), "Unknown Product"),
        "item_code":             clean_optional_string(row.get("item_code")),
        "business_id":           clean_nullable_int(row.get("business_id"), 0),
        "type":                  clean_optional_string(row.get("type")),
        "brand_id":              clean_optional_int(row.get("brand_id")),
        "category_id":           clean_optional_int(row.get("category_id")),
        "sub_category_id":       clean_optional_int(row.get("sub_category_id")),
        # sku / sku2 / sku3: NOT NULL DEFAULT '' — NULL from ERP → empty string
        "sku":                   clean_nullable_string(row.get("sku"),  ""),
        "sku2":                  clean_nullable_string(row.get("sku2"), ""),
        "sku3":                  clean_nullable_string(row.get("sku3"), ""),
        "barcode_type":          clean_optional_string(row.get("barcode_type")),
        "enable_stock":          clean_nullable_bool(row.get("enable_stock"), 0),
        "alert_quantity":        clean_optional_number(row.get("alert_quantity")),
        "weight":                clean_optional_string(row.get("weight")),
        "image":                 clean_optional_string(row.get("image")),
        "main_image":            clean_optional_string(row.get("main_image")),
        "product_description":   clean_optional_string(row.get("product_description")),
        "product_custom_field1": clean_optional_string(row.get("product_custom_field1")),
        "product_custom_field2": clean_optional_string(row.get("product_custom_field2")),
        "product_custom_field3": clean_optional_string(row.get("product_custom_field3")),
        "product_custom_field4": clean_optional_string(row.get("product_custom_field4")),
        "srp":                   clean_optional_number(row.get("srp")),
        "sales_price":           clean_optional_number(row.get("sales_price")),
        "is_inactive":           clean_nullable_bool(row.get("is_inactive"), 0),
        "not_for_selling":       clean_nullable_bool(row.get("not_for_selling"), 0),
        "out_of_stock":          clean_nullable_bool(row.get("out_of_stock"), 0),
        "aisle":                 clean_nullable_int(row.get("aisle"), 0),
        "rack":                  clean_nullable_int(row.get("rack"), 0),
        "shelf":                 clean_nullable_int(row.get("shelf"), 0),
        "bin":                   clean_nullable_int(row.get("bin"), 0),
        "qty_box":               clean_optional_string(row.get("qty_box")),
        "case_qty":              clean_optional_string(row.get("case_qty")),
        "master_case_qty":       clean_optional_number(row.get("master_case_qty")),
        "ml":                    clean_nullable_number(row.get("ml"), 0.0),
        "product_group_id":      clean_optional_int(row.get("product_group_id")),
        "group_variation_name":  clean_optional_string(row.get("group_variation_name")),
        "note":                  clean_optional_string(row.get("note")),
        "created_by":            clean_nullable_int(row.get("created_by"), 0),
        "created_at":            clean_nullable_datetime(row.get("created_at")),
        "updated_at":            clean_nullable_datetime(row.get("updated_at")),
        # synced_at is set by the upsert layer at write time — not from ERP
    }


# ── Dispatch ──────────────────────────────────────────────────────────────────

_NORMALIZERS = {
    "brands":        normalize_brand_row,
    "categories":    normalize_category_row,
    "product_group": normalize_product_group_row,
    "products":      normalize_product_row,
}


def normalize_row(table: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalize *row* for *table*.  Returns a clean dict or None on error.

    Callers should skip (and log) None returns rather than attempting to
    upsert the raw row, which may trigger NOT NULL constraint failures.
    """
    normalizer = _NORMALIZERS.get(table)
    if normalizer is None:
        # Unknown table — pass through as-is (no normalization applied)
        return dict(row)
    try:
        return normalizer(row)
    except Exception:
        # Last-resort catch: print but never propagate
        print(f"[NORM] normalize_row({table}) failed:\n{traceback.format_exc()}")
        return None
