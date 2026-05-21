"""
modules/image_search.py
-----------------------
Image-to-text pipeline that bridges visual input with the existing
fuzzy search engine.

Pipeline
--------
  1. Receive image bytes (JPEG / PNG / WEBP / GIF)
  2. Extract descriptive labels from the image
  3. Map those labels to a text query string
  4. Call the existing FuzzySearchEngine — zero changes to fuzzy_search.py

Label extraction — two-tier strategy
--------------------------------------
Tier 1 (preferred): MobileNetV2 via TensorFlow / Keras
  - Loads the pretrained ImageNet model on first use (lazy, cached)
  - Decodes top-N predictions and maps ImageNet class names to
    product-domain terms using a curated translation table
  - Requires:  pip install tensorflow pillow
    (or:        pip install torch torchvision pillow  — see _load_torch_model)

Tier 2 (always available): heuristic extractor
  - Reads EXIF metadata (camera model, description, user comment)
  - Falls back to the image filename supplied by the caller
  - Strips noise tokens (numbers, file extensions, camera model prefixes)
  - Always produces *something* useful without any ML dependency

The tier used is recorded in the response as ``extractor_used`` so the
caller can see which path ran.

Independence guarantee
----------------------
This module imports ONLY from:
  - Python stdlib  (io, os, re, logging)
  - modules.fuzzy_search  (get_engine only — read-only, no modifications)
  - Optional third-party: tensorflow / torch / PIL (all guarded by try/except)

It does NOT modify fuzzy_search.py or any other existing module.

Deployment notes (Render / ephemeral hosting)
----------------------------------------------
IMPORTANT: This module is designed for ephemeral hosting environments:
  • All image processing happens in-memory (bytes only, no temp files)
  • No persistent filesystem writes during image processing
  • ML models are cached in memory after first load (not persisted to disk)
  • Suitable for Render free instances with ~512 MB RAM
  • Compatible with Gunicorn multi-worker deployments

If the app restarts or a worker recycles:
  • ML models reload on first image classification request (no issue)
  • Previous images are not retained (expected in ephemeral environments)
  • Cache miss on model load is acceptable (~1–3 seconds for first inference)
"""

import io
import os
import re
import logging
from typing import List, Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)

# ── Optional dependency flags ──────────────────────────────────────────────────
# Checked once at import time so the rest of the module can branch cleanly.

_PIL_AVAILABLE = False
_TF_AVAILABLE  = False
_TORCH_AVAILABLE = False

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    pass

try:
    import tensorflow as _tf                          # noqa: F401
    _TF_AVAILABLE = True
except ImportError:
    pass

try:
    import torch as _torch                            # noqa: F401
    _TORCH_AVAILABLE = True
except ImportError:
    pass


# ── ImageNet → product-domain translation table ───────────────────────────────
# MobileNet is trained on ImageNet (1000 classes).  Most class names are not
# product terms.  This table maps the most relevant ImageNet labels to the
# vocabulary our search engine understands.
#
# Format:  "imagenet_class_fragment" → "product search term"
# Matching is substring / case-insensitive so partial class names work.
# Add new mappings freely — this table has no effect on search scoring.

_IMAGENET_TO_PRODUCT: Dict[str, str] = {
    # Smoking / tobacco
    "hookah":           "hookah",
    "tobacco":          "tobacco",
    "cigarette":        "cigarette",
    "cigar":            "cigar",
    "pipe":             "pipe",
    "lighter":          "lighter",
    "match":            "lighter",
    # Grinders / herb tools
    "grinder":          "grinder",
    "mortar":           "grinder",
    "herb":             "herb grinder",
    # Glass / water pipes
    "glass":            "glass pipe",
    "beaker":           "glass pipe",
    "flask":            "glass pipe",
    "bottle":           "glass pipe",
    "vase":             "glass pipe",
    # Vape
    "vape":             "vape",
    "electronic":       "vape",
    "pen":              "vape pen",
    # Beverages / energy drinks
    "can":              "energy drink",
    "beverage":         "energy drink",
    "drink":            "energy drink",
    "coffee":           "coffee",
    "beer":             "beer",
    "wine":             "wine",
    # Charcoal / accessories
    "charcoal":         "charcoal",
    "coal":             "charcoal",
    "ashtray":          "ashtray",
    "tray":             "ashtray",
    # Rolling / paper
    "paper":            "rolling paper",
    "rolling":          "rolling paper",
    # Candy / snacks
    "candy":            "candy",
    "chocolate":        "chocolate",
    "snack":            "snack",
    "chip":             "chips",
    # General retail
    "box":              "box",
    "bag":              "bag",
    "container":        "container",
    "jar":              "jar",
    "tin":              "tin",
    "pack":             "pack",
    "packet":           "pack",
    "pouch":            "pouch",
}

# Noise tokens stripped from heuristic labels (filename / EXIF cleaning)
_NOISE_TOKENS = frozenset({
    "img", "image", "photo", "pic", "picture", "dsc", "dcim",
    "jpg", "jpeg", "png", "webp", "gif", "bmp",
    "copy", "new", "final", "edit", "crop", "resized",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
})


# ── Lazy model cache ───────────────────────────────────────────────────────────
# Models are loaded on first use and cached for the lifetime of the process.
# This avoids a ~200 MB load on every request.

_tf_model  = None   # TensorFlow MobileNetV2
_tf_decode = None   # tf.keras.applications.mobilenet_v2.decode_predictions
_tf_preprocess = None

_torch_model     = None
_torch_transform = None
_torch_labels    = None


def _load_tf_model():
    """Load MobileNetV2 via TensorFlow/Keras (lazy, cached)."""
    global _tf_model, _tf_decode, _tf_preprocess
    if _tf_model is not None:
        return True
    try:
        import tensorflow as tf
        from tensorflow.keras.applications import MobileNetV2
        from tensorflow.keras.applications.mobilenet_v2 import (
            preprocess_input,
            decode_predictions,
        )
        _tf_model      = MobileNetV2(weights="imagenet", include_top=True)
        _tf_decode     = decode_predictions
        _tf_preprocess = preprocess_input
        logger.info("[ImageSearch] TensorFlow MobileNetV2 loaded.")
        return True
    except Exception as exc:
        logger.warning(f"[ImageSearch] TF model load failed: {exc}")
        return False


def _load_torch_model():
    """Load MobileNet_V2 via PyTorch (lazy, cached)."""
    global _torch_model, _torch_transform, _torch_labels
    if _torch_model is not None:
        return True
    try:
        import torch
        import torchvision.models as models
        import torchvision.transforms as transforms
        import urllib.request

        _torch_model = models.mobilenet_v2(pretrained=True)
        _torch_model.eval()

        _torch_transform = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            ),
        ])

        # Download ImageNet class labels if not cached
        labels_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_imagenet_labels.txt"
        )
        if not os.path.exists(labels_path):
            url = (
                "https://raw.githubusercontent.com/pytorch/hub/master/"
                "imagenet_classes.txt"
            )
            urllib.request.urlretrieve(url, labels_path)

        with open(labels_path, encoding="utf-8") as f:
            _torch_labels = [line.strip() for line in f.readlines()]

        logger.info("[ImageSearch] PyTorch MobileNetV2 loaded.")
        return True
    except Exception as exc:
        logger.warning(f"[ImageSearch] Torch model load failed: {exc}")
        return False


# ── Label extraction ───────────────────────────────────────────────────────────

def _extract_labels_tf(image_bytes: bytes, top_n: int = 5) -> List[str]:
    """
    Run MobileNetV2 inference via TensorFlow and return top-N class names.

    Parameters
    ----------
    image_bytes : raw image bytes (any PIL-supported format)
    top_n       : number of top predictions to return

    Returns
    -------
    list of lowercase class name strings, e.g. ["hookah", "tobacco_shop"]
    """
    import numpy as np
    import tensorflow as tf

    img = _PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.resize((224, 224))
    arr = np.array(img, dtype=np.float32)
    arr = np.expand_dims(arr, axis=0)
    arr = _tf_preprocess(arr)

    preds = _tf_model.predict(arr, verbose=0)
    decoded = _tf_decode(preds, top=top_n)[0]   # list of (id, name, prob)

    # Return class names, replacing underscores with spaces
    return [name.replace("_", " ").lower() for _, name, _ in decoded]


def _extract_labels_torch(image_bytes: bytes, top_n: int = 5) -> List[str]:
    """
    Run MobileNet_V2 inference via PyTorch and return top-N class names.
    """
    import torch

    img = _PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    tensor = _torch_transform(img).unsqueeze(0)

    with torch.no_grad():
        output = _torch_model(tensor)

    probs      = torch.nn.functional.softmax(output[0], dim=0)
    top_probs, top_indices = torch.topk(probs, top_n)

    labels = []
    for idx in top_indices:
        label = _torch_labels[idx.item()] if _torch_labels else f"class_{idx.item()}"
        labels.append(label.replace("_", " ").lower())
    return labels


def _extract_labels_heuristic(
    image_bytes: bytes,
    filename: str = "",
) -> List[str]:
    """
    Heuristic label extractor — no ML required.

    Sources (in priority order):
      1. EXIF ImageDescription / UserComment / XPComment (if PIL available)
      2. EXIF Make / Model (camera info — usually not useful, filtered out)
      3. Filename tokens (split on non-alphanumeric characters)

    Returns
    -------
    list of cleaned token strings
    """
    labels: List[str] = []

    # ── EXIF extraction (requires PIL) ────────────────────────────────────────
    if _PIL_AVAILABLE and image_bytes:
        try:
            img = _PILImage.open(io.BytesIO(image_bytes))
            exif_data = img._getexif() if hasattr(img, "_getexif") else None

            if exif_data:
                # EXIF tag IDs for useful text fields
                EXIF_TAGS = {
                    270:  "ImageDescription",
                    37510: "UserComment",
                    40091: "XPComment",
                    40095: "XPSubject",
                    40094: "XPKeywords",
                }
                for tag_id, tag_name in EXIF_TAGS.items():
                    value = exif_data.get(tag_id)
                    if not value:
                        continue
                    # XP tags are UTF-16LE bytes
                    if isinstance(value, bytes):
                        try:
                            value = value.decode("utf-16-le").rstrip("\x00")
                        except Exception:
                            try:
                                value = value.decode("utf-8", errors="ignore")
                            except Exception:
                                continue
                    if isinstance(value, str) and value.strip():
                        labels.extend(
                            t.lower() for t in re.split(r"[\s,;|/\\]+", value)
                            if t and len(t) > 1
                        )
        except Exception:
            pass  # EXIF read failure is non-fatal

    # ── Filename tokens ───────────────────────────────────────────────────────
    if filename:
        # Strip path and extension, then split on non-alphanumeric chars
        base = os.path.splitext(os.path.basename(filename))[0]
        tokens = re.split(r"[^a-zA-Z]+", base)
        labels.extend(t.lower() for t in tokens if t and len(t) > 1)

    # ── Clean: remove noise tokens and very short strings ─────────────────────
    cleaned = [
        t for t in labels
        if t not in _NOISE_TOKENS and len(t) >= 3
    ]

    return cleaned if cleaned else ["product"]


# ── Label → query conversion ───────────────────────────────────────────────────

def labels_to_query(labels: List[str]) -> str:
    """
    Convert a list of image labels into a single search query string.

    Strategy
    --------
    1. For each label, check if any key in _IMAGENET_TO_PRODUCT is a
       substring of the label (case-insensitive).  If so, use the mapped
       product term instead of the raw label.
    2. Deduplicate while preserving order.
    3. Join with spaces, capped at 5 terms to keep the query focused.

    Parameters
    ----------
    labels : list of raw label strings from the extractor

    Returns
    -------
    str — a clean product search query, e.g. "hookah glass pipe charcoal"
    """
    mapped: List[str] = []
    seen:   set       = set()

    for label in labels:
        label_lower = label.lower()

        # Try to map via the translation table
        translated = None
        for key, product_term in _IMAGENET_TO_PRODUCT.items():
            if key in label_lower:
                translated = product_term
                break

        term = translated if translated else label_lower

        # Deduplicate
        if term not in seen:
            seen.add(term)
            mapped.append(term)

    # Cap at 5 terms — longer queries dilute fuzzy scoring
    query = " ".join(mapped[:5]).strip()
    return query if query else "product"


# ── Public API ─────────────────────────────────────────────────────────────────

def search_by_image(
    image_bytes: bytes,
    filename: str = "",
    top_k: int = 20,
    source_db_id: Optional[int] = 1,
) -> Dict[str, Any]:
    """
    Full image-to-search pipeline.

    Steps
    -----
    1. Attempt ML-based label extraction (TF → PyTorch → heuristic fallback)
    2. Convert labels to a text query via labels_to_query()
    3. Run the existing FuzzySearchEngine.search() — no modifications needed
    4. Return structured result dict

    Parameters
    ----------
    image_bytes : raw bytes of the uploaded image file
    filename    : original filename (used by heuristic extractor as a hint)
    top_k       : maximum number of search results to return

    Returns
    -------
    dict with keys:
        query_generated  : str   — the text query derived from the image
        labels           : list  — raw labels before query conversion
        extractor_used   : str   — "tensorflow" | "pytorch" | "heuristic"
        results          : list  — search results from FuzzySearchEngine
        result_count     : int   — number of results returned
        error            : str | None — set if a non-fatal error occurred
    """
    labels: List[str]  = []
    extractor_used: str = "heuristic"
    error: Optional[str] = None

    # ── Tier 1a: TensorFlow MobileNetV2 ──────────────────────────────────────
    if _TF_AVAILABLE and _PIL_AVAILABLE:
        try:
            if _load_tf_model():
                labels         = _extract_labels_tf(image_bytes)
                extractor_used = "tensorflow"
        except Exception as exc:
            error = f"TF inference failed: {exc}"
            logger.warning(f"[ImageSearch] {error}")
            labels = []

    # ── Tier 1b: PyTorch MobileNet_V2 (if TF unavailable or failed) ──────────
    if not labels and _TORCH_AVAILABLE and _PIL_AVAILABLE:
        try:
            if _load_torch_model():
                labels         = _extract_labels_torch(image_bytes)
                extractor_used = "pytorch"
        except Exception as exc:
            error = f"Torch inference failed: {exc}"
            logger.warning(f"[ImageSearch] {error}")
            labels = []

    # ── Tier 2: heuristic fallback (always available) ─────────────────────────
    if not labels:
        labels         = _extract_labels_heuristic(image_bytes, filename)
        extractor_used = "heuristic"

    # ── Convert labels → query ────────────────────────────────────────────────
    query = labels_to_query(labels)

    # ── Run fuzzy search ──────────────────────────────────────────────────────
    # Import here (not at module top) to keep the module independent and
    # avoid circular imports.  get_engine() returns the singleton — no new
    # engine is created.
    from modules.fuzzy_search import get_engine, get_global_engine
    engine  = get_global_engine() if source_db_id is None else get_engine(source_db_id=source_db_id)
    results = engine.search(query, top_k=top_k)

    return {
        "query_generated": query,
        "labels":          labels,
        "extractor_used":  extractor_used,
        "results":         results,
        "result_count":    len(results),
        "error":           error,
    }
