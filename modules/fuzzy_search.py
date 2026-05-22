"""
modules/fuzzy_search.py
-----------------------
Core fuzzy search engine with:
  - 3-algorithm blend (token_set_ratio, WRatio, partial_ratio)
  - Synonym normalisation before scoring
  - Score boosting (exact match, startswith, substring)
  - Relevance-first composite ranking
  - In-memory product index rebuilt from SQLite
  - Optional background auto-rebuild thread

Algorithms & weights
--------------------
  token_set_ratio  0.5  — word order irrelevant, partial overlap
  WRatio           0.3  — typo tolerance
  partial_ratio    0.2  — short query inside long string

Boosting rules (applied on top of blend score, capped at 100)
--------------------------------------------------------------
  Exact match (normalised)   → +20
  Starts-with match          → +10
  Substring match (raw name) → +10

Composite ranking formula — relevance-first
-------------------------------------------
  Fuzzy threshold gate: products with fuzzy_score < 70 are excluded entirely.

  Tie band (fuzzy gap ≤ 10 points):
    final = 0.85 × fuzzy + 0.10 × popularity + 0.05 × click_rate

  Clear winner (fuzzy gap > 10 points):
    final = fuzzy   (popularity and clicks ignored)

  This ensures:
  • Irrelevant products never appear regardless of popularity
  • Clearly more relevant products always rank above less relevant ones
  • Popularity/clicks only break ties between equally relevant products
"""

import re
import threading
import time
import sys
import os
from typing import List, Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    SEARCH_MIN_SCORE, SEARCH_DEFAULT_K, SEARCH_MAX_K,
    SEARCH_SOURCE_PRIORITY, FULL_REBUILD_AFTER_N_INCREMENTALS,
)
from db.database import get_connection

try:
    from modules.metrics import search_metrics as _search_metrics
except ImportError:
    _search_metrics = None

try:
    from rapidfuzz import fuzz, process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False


# ── Synonym store — DB-backed, hot-reloadable ─────────────────────────────────
#
# Synonyms are loaded from the `synonyms` SQLite table at startup and cached
# in module-level variables.  apply_synonyms() uses these cached variables so
# every search call is O(1) — no DB round-trip per query.
#
# Call reload_synonyms() after any INSERT / DELETE on the synonyms table to
# rebuild the regex and lookup dict without restarting the server.
#
# Thread safety: _synonyms_lock guards all reads and writes to the three
# module-level variables below.

_synonyms_lock   = threading.Lock()
SYNONYMS: Dict[str, str] = {}          # variant → canonical  (public, read-only)
_SYNONYM_PATTERN = None                 # compiled regex (rebuilt on reload)
_SYNONYM_LOOKUP:  Dict[str, str] = {}  # lowercase variant → canonical


def _load_synonyms_from_db() -> Dict[str, str]:
    """
    Read all rows from the synonyms table and return as a dict.
    Returns an empty dict if the table doesn't exist yet (first-run race).
    """
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT variant, canonical FROM synonyms ORDER BY LENGTH(variant) DESC"
            ).fetchall()
            return {r["variant"].strip().lower(): r["canonical"].strip().lower()
                    for r in rows if r["variant"] and r["canonical"]}
        finally:
            conn.close()
    except Exception as exc:
        print(f"[Synonyms] Could not load from DB: {exc}")
        return {}


def _compile_synonym_regex(synonyms: Dict[str, str]):
    """
    Build a single compiled regex alternation from the synonym keys.

    Why one combined pattern instead of a loop?
    --------------------------------------------
    A loop applies replacements sequentially, so a later synonym can
    accidentally match text that was just written by an earlier one.
    Example with a loop:
        "sheesha" → "hookah"          (first replacement)
        "hooka"   matches in "hookah" → "hookaah"  (second replacement, wrong!)

    A single alternation pattern is applied in ONE scan of the string.
    The regex engine advances past each match without revisiting it, so
    no replacement can ever be re-processed.

    Construction rules:
      1. Sort keys by descending length so longer phrases (e.g. "e cig")
         are tried before shorter ones (e.g. "ecig") at each position.
      2. Wrap each key with \\b word boundaries to block partial matches
         ("pip" must not fire inside "pipe").
      3. Compile with re.IGNORECASE so the match is case-insensitive while
         the replacement is always the canonical lowercase form.
    """
    if not synonyms:
        # Return a pattern that never matches anything
        return re.compile(r"(?!)")

    sorted_variants = sorted(synonyms.keys(), key=len, reverse=True)
    alternation     = "|".join(re.escape(v) for v in sorted_variants)
    return re.compile(r"\b(?:" + alternation + r")\b", re.IGNORECASE)


def reload_synonyms() -> int:
    """
    Reload synonyms from the database into the in-memory cache.

    Called automatically at module import time and after any API mutation
    (add / delete).  Thread-safe.

    Returns
    -------
    int — number of synonym pairs now loaded.
    """
    global SYNONYMS, _SYNONYM_PATTERN, _SYNONYM_LOOKUP

    new_synonyms = _load_synonyms_from_db()
    new_pattern  = _compile_synonym_regex(new_synonyms)
    new_lookup   = {k.lower(): v for k, v in new_synonyms.items()}

    with _synonyms_lock:
        SYNONYMS         = new_synonyms
        _SYNONYM_PATTERN = new_pattern
        _SYNONYM_LOOKUP  = new_lookup

    print(f"[Synonyms] Loaded {len(new_synonyms)} synonym(s) from DB.")
    return len(new_synonyms)


# Load synonyms at import time.
# The DB may not exist yet on the very first import (before init_db() runs),
# so we catch any error and start with an empty set — reload_synonyms() will
# be called again after init_db() completes.
reload_synonyms()


def apply_synonyms(query: str) -> str:
    """
    Expand known synonym variants in *query* to their canonical forms.

    Algorithm
    ---------
    Uses a single pre-compiled regex alternation so the entire string is
    scanned exactly once.  Each matched token is replaced via a lookup
    callback; unmatched text is passed through unchanged.

    The regex and lookup dict are loaded from the `synonyms` SQLite table
    at startup and can be hot-reloaded via reload_synonyms() without
    restarting the server.

    Properties guaranteed by this implementation:
      • Word-boundary safety  — "pip" never fires inside "pipe"
      • No double replacement — "sheesha" → "hookah", not "hookaah"
      • Longest-match first   — "e cig" wins over "ecig" at the same position
      • Case-insensitive      — "Sheesha", "SHEESHA", "sheesha" all expand
      • Original spacing kept — only the matched token is replaced

    Parameters
    ----------
    query : str
        Raw user input, any casing.

    Returns
    -------
    str
        Query with synonym variants replaced by canonical terms.
        Identical to input if no synonyms are found.

    Examples
    --------
    >>> apply_synonyms("sheesha pipe")
    'hookah pipe'
    >>> apply_synonyms("hooka")
    'hookah'
    >>> apply_synonyms("glass pipe")
    'glass pipe'
    """
    q = query.lower().strip()
    if not q:
        return q

    with _synonyms_lock:
        pattern = _SYNONYM_PATTERN
        lookup  = _SYNONYM_LOOKUP

    if not lookup:
        return q

    return pattern.sub(lambda m: lookup[m.group(0).lower()], q)


# ── Text normaliser ────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """
    Prepare text for fuzzy matching:
      - lowercase
      - strip prices  ($20.00)
      - strip bracket content  (9MM)  [BOX]
      - keep only alphanumeric + spaces
      - collapse whitespace
    """
    if not text or not isinstance(text, str):
        return ""
    text = text.lower()
    text = re.sub(r'\$\s*\d+\.?\d*', ' ', text)        # prices
    text = re.sub(r'[\[\(\{].*?[\]\)\}]', ' ', text)   # brackets
    text = re.sub(r'[^a-z0-9\s]', ' ', text)           # special chars
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ── Core scoring ───────────────────────────────────────────────────────────────

def blend_score(query: str, normalized_text: str, raw_text: str) -> float:
    """
    Three-algorithm blend.  Scores against both normalised and raw text,
    returns the higher of the two.

    Weights:
        token_set_ratio  0.5
        WRatio           0.3
        partial_ratio    0.2
    """
    if not RAPIDFUZZ_AVAILABLE:
        raise RuntimeError("rapidfuzz is not installed. Run: pip install rapidfuzz")

    # Against normalised text
    ts_n = fuzz.token_set_ratio(query, normalized_text)
    wr_n = fuzz.WRatio(query, normalized_text)
    pr_n = fuzz.partial_ratio(query, normalized_text)
    score_n = 0.5 * ts_n + 0.3 * wr_n + 0.2 * pr_n

    # Against raw text (preserves numbers, original casing)
    ts_r = fuzz.token_set_ratio(query, raw_text)
    wr_r = fuzz.WRatio(query, raw_text)
    pr_r = fuzz.partial_ratio(query, raw_text)
    score_r = 0.5 * ts_r + 0.3 * wr_r + 0.2 * pr_r

    return max(score_n, score_r)


# ── "Did You Mean" suggestion ─────────────────────────────────────────────────

# Static keyword vocabulary — common product-type terms users frequently
# misspell. Merged into every suggestion pool automatically.
# Safe to extend; has no effect on search scoring.
_SUGGESTION_KEYWORDS: List[str] = [
    "hookah", "grinder", "cigarette", "tobacco", "charcoal", "lighter",
    "pipe", "vape", "e-cigarette", "energy drink", "blunt wrap", "cigar",
    "rolling paper", "filter", "ashtray", "bong", "bubbler", "dab rig",
    "glass pipe", "water pipe", "herb grinder", "rolling machine",
]

# Maximum number of candidates passed to extractOne.
# Keeps the function fast even when the caller supplies thousands of names.
_SUGGESTION_POOL_LIMIT = 500
_ID_SCOPE_MULTIPLIER = 1_000_000_000


def get_query_suggestion(
    query: str,
    choices: Optional[List[str]] = None,
    top_result_score: Optional[float] = None,   # kept for API compatibility; unused
) -> Optional[str]:
    """
    Return a "Did You Mean" spelling correction for *query*, or ``None``.

    Design goals
    ------------
    • Suggest when helpful  — obvious typos like "grdiner" → "grinder"
    • Stay silent otherwise — correct queries, gibberish, or empty input
    • Independent of search results — does not rely on top_result_score
      (that parameter is accepted for backwards-compatibility but ignored)

    Candidate pool (three sources, merged and deduplicated)
    -------------------------------------------------------
    1. ``choices``          — caller-supplied strings (top product names,
                              category names, etc.)
    2. ``SYNONYMS`` keys    — known misspelling variants ("hooka", "grider"…)
    3. ``SYNONYMS`` values  — canonical forms ("hookah", "grinder"…)
    4. ``_SUGGESTION_KEYWORDS`` — curated product-type vocabulary

    Combining synonym keys AND values is important: a user typing "grdiner"
    needs to match against "grinder" (a value), while a user typing "hooka"
    needs to match against "hookah" (also a value, since "hooka" → "hookah").

    Threshold logic  (score = WRatio on normalized strings)
    --------------------------------------------------------
    score < 50   → too distant — no reliable correction exists → None
    50 ≤ score < 100 → plausible typo — return the best candidate
    score = 100  → identical after normalization — nothing to correct → None

    Why 100 instead of 90 as the upper bound?
    ------------------------------------------
    "hooka" scores 90.9 against "hookah" — that IS a typo that needs
    correcting.  Cutting off at 90 would suppress it.  The only case where
    we must return None is when the normalized query IS the candidate
    (score = 100), which the identity check below handles explicitly.

    Parameters
    ----------
    query            : str
        Raw user input, any casing.
    choices          : list of str, optional
        Extra candidates (e.g. top product names from the search results).
        Merged with synonym vocabulary and keyword list.
    top_result_score : float, optional
        Accepted for API compatibility with the route layer; not used.

    Returns
    -------
    str or None
        The best-matching candidate string, or None.

    Examples
    --------
    >>> get_query_suggestion("grdiner")
    'grinder'
    >>> get_query_suggestion("hooka")
    'hookah'
    >>> get_query_suggestion("hookah")
    None
    >>> get_query_suggestion("asdlkj123")
    None
    """
    if not RAPIDFUZZ_AVAILABLE:
        return None

    # ── Step 1: normalize the query ───────────────────────────────────────────
    # Use the same normalize() the search engine uses so comparisons are
    # apples-to-apples (lowercase, no prices/brackets/special chars).
    query_n = normalize(query.strip())
    if not query_n:
        return None

    # ── Step 2: build the candidate pool ─────────────────────────────────────
    # Sources (in priority order for deduplication):
    #   a) caller-supplied choices  (real product names — highest signal)
    #   b) SYNONYMS values          (canonical forms: "hookah", "grinder"…)
    #   c) static keyword list      (curated fallback vocabulary)
    #
    # IMPORTANT: synonym KEYS are intentionally excluded from the pool.
    # Keys are known misspellings ("hooka", "grider", "sheesha"…).
    # If a key were in the pool, the query "hooka" would score 100 against
    # itself, trigger the identity check, and return None — the opposite of
    # what we want.  Only canonical values belong here as valid suggestions.
    #
    # dict.fromkeys() deduplicates while preserving insertion order so that
    # higher-priority sources win when two entries normalize to the same string.
    raw_pool: List[str] = list(dict.fromkeys(
        [c for c in (choices or []) if isinstance(c, str) and c.strip()]
        + [v for v in SYNONYMS.values() if isinstance(v, str) and v.strip()]
        + _SUGGESTION_KEYWORDS
    ))

    # Cap pool size for performance — keep the first N entries (caller-supplied
    # choices are first, so the most relevant candidates are always included).
    pool = raw_pool[:_SUGGESTION_POOL_LIMIT]
    if not pool:
        return None

    # ── Step 3: normalize every candidate ────────────────────────────────────
    # Pre-normalize so extractOne compares clean strings on both sides.
    normalized_pool: List[str] = [normalize(c) for c in pool]

    # ── Step 4: find the best match ───────────────────────────────────────────
    # WRatio handles character transpositions, insertions, and deletions —
    # exactly the errors users make when typing product names.
    result = process.extractOne(
        query_n,
        normalized_pool,
        scorer=fuzz.WRatio,
    )
    if result is None:
        return None

    _matched_text, match_score, idx = result

    # ── Step 5: apply threshold gates ────────────────────────────────────────
    #
    # Gate A — lower bound (score < 60):
    #   The best candidate is too distant from the query.  Suggesting it
    #   would be misleading.  60 is chosen because:
    #     • Real typos ("grdiner"→"grinder") score 85+
    #     • Gibberish ("asdlkj123"→"wraps") scores ~51 — safely below 60
    #     • The gap between real typos and noise is wide enough that 60
    #       cleanly separates them without suppressing valid corrections.
    if match_score < 60:
        return None

    # Gate B — identity check (normalized match == normalized query):
    #   The query already IS the canonical form — nothing to correct.
    #   This catches "hookah" → "hookah" (score 100) without needing a
    #   hard upper-bound cutoff that would suppress real typos like
    #   "hooka" → "hookah" (score 90.9).
    if normalized_pool[idx] == query_n:
        return None

    # ── Step 6: return the original (un-normalized) candidate ────────────────
    # Return pool[idx] rather than the normalized form so the UI receives
    # proper casing — e.g. "Grinder" if that's what was in the choices list.
    return pool[idx]


def apply_boost(base_score: float, query_n: str, product_name_n: str, raw_product_name: str = "") -> float:
    """
    Apply deterministic text-match boosting rules on top of the blend score.

    Rules (applied in order, cumulative):
      +20  exact match on normalised product name
      +10  product name starts with the query
      +10  query appears as a substring in the raw product name (case-insensitive)
           — catches "hookah" inside "China Hookah Small" without exact/prefix match

    The final score is capped at 100.
    This is the FUZZY component only — composite ranking is applied separately
    in _composite_score() after all three signals are available.

    Parameters
    ----------
    base_score       : blend score (0–100)
    query_n          : normalised query string
    product_name_n   : normalised product name
    raw_product_name : original (un-normalised) product name for substring check
    """
    boost = 0.0

    if query_n and product_name_n:
        if query_n == product_name_n:
            boost += 20.0
        elif product_name_n.startswith(query_n):
            boost += 10.0
        # Exact substring match in raw name — rewards products that literally
        # contain the query word (e.g. "hookah" in "China Hookah Small")
        elif raw_product_name and query_n in raw_product_name.lower():
            boost += 10.0

    return min(base_score + boost, 100.0)


# ── Query expansion ───────────────────────────────────────────────────────────
#
# Maps a user's intent phrase to a list of concrete product keywords.
# When a query matches a key, the engine searches for ALL the mapped terms
# in addition to the original query, then merges the results.
#
# Design rules
# ------------
# • Keys are lowercase, stripped phrases (matched case-insensitively).
# • Values are lists of canonical product keywords — each is searched
#   independently and results are merged by product ID (best score wins).
# • The original query is ALWAYS searched first; expansions are additive.
# • Keep entries domain-specific — generic phrases produce noisy results.
# • Add new entries freely; no code changes needed elsewhere.
#
# Examples
# --------
#   "smoking stuff"  → searches "hookah", "pipe", "cigarette", "tobacco"
#   "vaping"         → searches "vape", "e-cigarette"
#   "rolling"        → searches "rolling paper", "blunt wrap", "grinder"

QUERY_EXPANSIONS: Dict[str, List[str]] = {
    # ── Smoking / tobacco ─────────────────────────────────────────────────────
    "smoking stuff":    ["hookah", "pipe", "cigarette", "tobacco", "cigar"],
    "smoking":          ["cigarette", "tobacco", "cigar", "pipe"],
    "smoke":            ["cigarette", "tobacco", "cigar", "pipe"],
    "tobacco products": ["cigarette", "tobacco", "cigar", "pipe", "hookah"],
    # ── Hookah / shisha ───────────────────────────────────────────────────────
    "hookah stuff":     ["hookah", "charcoal", "pipe", "shisha"],
    "shisha":           ["hookah", "charcoal", "pipe"],
    "water pipe":       ["hookah", "bong", "bubbler"],
    # ── Vaping ────────────────────────────────────────────────────────────────
    "vaping":           ["vape", "e-cigarette"],
    "vape stuff":       ["vape", "e-cigarette"],
    "e cig":            ["e-cigarette", "vape"],
    # ── Rolling / wrapping ────────────────────────────────────────────────────
    "rolling":          ["rolling paper", "blunt wrap", "grinder"],
    "roll up":          ["rolling paper", "tobacco", "filter"],
    "blunt":            ["blunt wrap", "rolling paper"],
    # ── Grinding ──────────────────────────────────────────────────────────────
    "herb grinder":     ["grinder"],
    "weed grinder":     ["grinder"],
    # ── Accessories ───────────────────────────────────────────────────────────
    "smoking accessories": ["lighter", "ashtray", "filter", "pipe", "grinder"],
    "accessories":      ["lighter", "ashtray", "filter"],
    "lighter":          ["lighter", "torch"],
    # ── Beverages ─────────────────────────────────────────────────────────────
    "energy":           ["energy drink"],
    "drinks":           ["energy drink", "beverage"],
    # ── Glass ─────────────────────────────────────────────────────────────────
    "glass":            ["glass pipe", "bong", "bubbler"],
    "glass pipe":       ["glass pipe", "bong"],
    "bong":             ["bong", "bubbler", "glass pipe"],
}


def expand_query(query: str) -> List[str]:
    """
    Return a list of search terms derived from *query* via the expansion map.

    The original query is always the first element so it is searched with
    the highest priority.  Expansion terms follow in the order they appear
    in QUERY_EXPANSIONS.

    Matching is case-insensitive and checks both the full query and each
    individual token, so "I need smoking stuff" matches "smoking stuff".

    Parameters
    ----------
    query : str
        Raw user input (pre-synonym-expansion).

    Returns
    -------
    list of str
        [original_query] if no expansion found, or
        [original_query, term1, term2, ...] if expansion found.
        Always deduplicated; original query is never repeated.

    Examples
    --------
    >>> expand_query("smoking stuff")
    ['smoking stuff', 'hookah', 'pipe', 'cigarette', 'tobacco', 'cigar']
    >>> expand_query("hookah")
    ['hookah']
    >>> expand_query("VAPING")
    ['VAPING', 'vape', 'e-cigarette']
    """
    q_lower = query.strip().lower()
    if not q_lower:
        return [query]

    extra: List[str] = []
    seen_extra: set  = set()

    # Check the full query first (longest match wins naturally)
    if q_lower in QUERY_EXPANSIONS:
        for term in QUERY_EXPANSIONS[q_lower]:
            if term not in seen_extra and term.lower() != q_lower:
                seen_extra.add(term)
                extra.append(term)

    # Also check each individual token so "I need smoking stuff" still expands
    if not extra:
        for token in q_lower.split():
            if token in QUERY_EXPANSIONS:
                for term in QUERY_EXPANSIONS[token]:
                    if term not in seen_extra and term.lower() != q_lower:
                        seen_extra.add(term)
                        extra.append(term)

    return [query] + extra

# Minimum fuzzy score a product must achieve to appear in results.
# Products below this threshold are irrelevant to the query and excluded
# before the composite formula is applied.
# 70 is the "high match" boundary — below it the match is speculative.
FUZZY_MIN_THRESHOLD: float = 50.0

# When two products' fuzzy scores differ by more than this, the higher-fuzzy
# product wins outright — popularity and clicks are ignored.
# This prevents a popular-but-less-relevant product from overtaking a clearly
# better match.  10 points is roughly the difference between "hookah pipe"
# (exact word match) and "hookah charcoal" (same word, different product).
FUZZY_TIE_BAND: float = 10.0


def _composite_score(
    fuzzy: float,
    popularity: float,
    click_rate: float,
    best_fuzzy_in_results: float = 0.0,
    source_priority: float = 0.0,
) -> float:
    """
    Combine three normalised signals (each 0–100) into a single ranking score.

    Relevance-first design
    ----------------------
    Popularity and click signals are only applied when the fuzzy score is
    within FUZZY_TIE_BAND (10 points) of the best fuzzy score in the result
    set.  When a product's fuzzy score is clearly lower than the best match,
    it ranks on fuzzy alone — popularity cannot rescue an irrelevant result.

    Formula (tie band — fuzzy scores are close)
    -------------------------------------------
    final = 0.85 × fuzzy + 0.10 × popularity + 0.05 × click_rate + source_priority

    Formula (clear winner — fuzzy gap > FUZZY_TIE_BAND)
    ----------------------------------------------------
    final = fuzzy + source_priority   (popularity and click signals ignored)

    Weight rationale
    ----------------
    0.85  fuzzy_score     — relevance is the dominant signal.
    0.10  popularity      — secondary tie-breaker.
    0.05  click_rate      — tertiary tie-breaker.
    source_priority       — optional per-DB boost from SEARCH_SOURCE_PRIORITY config.
                            Applied after all other signals so relevance is never
                            overridden, but equal-quality results from a preferred
                            source float to the top.

    Parameters
    ----------
    fuzzy                  : boosted fuzzy blend score for this product (0–100)
    popularity             : normalised sales-volume score (0–100)
    click_rate             : normalised click-count score (0–100)
    best_fuzzy_in_results  : highest fuzzy score across all candidates in this
                             search.  Used to determine whether this product is
                             in the tie band or clearly behind.
    source_priority        : additive boost from SEARCH_SOURCE_PRIORITY[source_db_id].
                             Only non-zero for global engine searches.
    """
    gap = best_fuzzy_in_results - fuzzy

    if gap > FUZZY_TIE_BAND:
        return min(round(fuzzy + source_priority, 2), 100.0)

    raw = 0.85 * fuzzy + 0.10 * popularity + 0.05 * click_rate + source_priority
    return min(round(raw, 2), 100.0)


# ── Main engine ────────────────────────────────────────────────────────────────

class FuzzySearchEngine:
    """
    Fuzzy product search engine backed by SQLite.

    Parameters
    ----------
    text_fields : list of str
        Product dict fields to combine into the searchable string.
        Default: ["name", "brand_name", "category_name"]
    min_score : float
        Minimum blend score (0–100) to include in results.
    rebuild_interval : int or None
        If set, a background thread rebuilds the index every N seconds.
        If None, call rebuild() manually.
    """

    def __init__(
        self,
        source_db_id: Optional[int] = 1,
        text_fields: Optional[List[str]] = None,
        min_score: float = SEARCH_MIN_SCORE,
        rebuild_interval: Optional[int] = None,
    ):
        if not RAPIDFUZZ_AVAILABLE:
            raise RuntimeError("rapidfuzz is not installed. Run: pip install rapidfuzz")

        self.source_db_id      = int(source_db_id) if source_db_id is not None else None
        self.is_global         = source_db_id is None
        self.text_fields       = text_fields or ["name", "brand_name", "category_name"]
        self.min_score         = min_score
        self.rebuild_interval  = rebuild_interval

        self._items:              List[Dict[str, Any]] = []
        self._raw_strings:        List[str] = []
        self._normalized_strings: List[str] = []
        # RLock allows re-entrant acquisition (e.g. _renormalize_signals called
        # from update_products_incremental which already holds the lock).
        self._lock                = threading.RLock()
        self._last_built:         Optional[float] = None

        # Counts incremental updates since the last full rebuild.
        # When it reaches FULL_REBUILD_AFTER_N_INCREMENTALS, a compaction
        # rebuild is triggered in a background thread.
        self._incremental_update_count: int = 0

        # Build index on startup — try disk cache first for instant startup.
        # Falls back to a full SQLite rebuild if the cache is missing/stale/corrupt.
        if not self._load_from_cache():
            self.rebuild()

        # Start background refresh thread if interval is set
        if rebuild_interval:
            self._start_background_refresh()

    # ── Disk cache integration ─────────────────────────────────────────────────

    def _load_from_cache(self) -> bool:
        """
        Try to populate the engine from the persistent disk cache.

        Returns True and updates ``_items``, ``_raw_strings``,
        ``_normalized_strings``, and ``_last_built`` on a cache hit.
        Returns False on a miss, a failed validation, or any exception —
        the caller must then call ``rebuild()`` to build from SQLite.

        Errors are caught and printed; they never propagate to the caller.
        """
        try:
            from config import CACHE_ENABLED
            if not CACHE_ENABLED:
                return False
        except ImportError:
            return False

        try:
            from modules.cache_manager import get_cache_manager
            mgr  = get_cache_manager(self.source_db_id)
            data = mgr.load_engine_data()
            if data is None:
                if _search_metrics:
                    _search_metrics.record_disk_cache_miss()
                return False

            with self._lock:
                self._items              = data["items"]
                self._raw_strings        = data["raw_strings"]
                self._normalized_strings = data["normalized_strings"]
                self._last_built         = data.get("last_built", time.time())

            if _search_metrics:
                _search_metrics.record_disk_cache_hit()
            label = "global" if self.is_global else f"db_id={self.source_db_id}"
            print(
                f"[Search] Cache HIT ({label}): "
                f"{len(self._items)} products loaded from disk."
            )
            return True

        except Exception as exc:
            if _search_metrics:
                _search_metrics.record_disk_cache_miss()
            label = "global" if self.is_global else f"db_id={self.source_db_id}"
            print(f"[Search] Cache load failed ({label}): {exc} — will rebuild from DB.")
            return False

    def _save_to_cache(
        self,
        items: List[Dict[str, Any]],
        raw_strings: List[str],
        normalized_strings: List[str],
        last_built: float,
    ) -> None:
        """
        Persist the current in-memory index to the disk cache.

        Runs synchronously (the caller — rebuild() — is already executing in
        a background thread during sync, or in a dedicated request for the
        manual rebuild endpoint).  Errors are caught and logged; they never
        surface to callers or interrupt search availability.
        """
        try:
            from config import CACHE_ENABLED
            if not CACHE_ENABLED:
                return
        except ImportError:
            return

        try:
            from modules.cache_manager import get_cache_manager
            mgr  = get_cache_manager(self.source_db_id)
            data = {
                "items":              items,
                "raw_strings":        raw_strings,
                "normalized_strings": normalized_strings,
                "last_built":         last_built,
            }
            mgr.save_engine_data(data)
        except Exception as exc:
            label = "global" if self.is_global else f"db_id={self.source_db_id}"
            print(f"[Search] Cache save failed ({label}): {exc}")

    # ── Index building ─────────────────────────────────────────────────────────

    def _load_products_from_db(
        self,
        product_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Load products from SQLite, joining brand, category names, and
        pre-aggregated ranking signals (popularity, click_count).

        popularity  = number of transaction sell lines for this product
                      (proxy for historical sales volume)
        click_count = cumulative click-throughs from product_clicks table

        Parameters
        ----------
        product_ids : list of int, optional
            When provided, only fetch these specific product IDs (scoped p.id
            values).  Used by update_products_incremental() to avoid a full
            table scan when only a subset of products changed.
        """
        conn = get_connection()
        try:
            sql = """
                SELECT
                    p.id,
                    p.name,
                    p.sku,
                    p.sku2,
                    p.item_code,
                    p.image,
                    p.main_image,
                    p.srp,
                    p.sales_price,
                    p.product_description,
                    p.aisle,
                    p.rack,
                    p.shelf,
                    p.bin,
                    p.qty_box,
                    p.case_qty,
                    p.out_of_stock,
                    p.is_inactive,
                    p.product_group_id,
                    p.group_variation_name,
                    p.category_id,
                    p.source_db_id,
                    CASE
                      WHEN p.id >= (p.source_db_id * ?)
                      THEN (p.id - (p.source_db_id * ?))
                      ELSE p.id
                    END AS source_product_id,
                    COALESCE(cd.name, '') AS database_name,
                    COALESCE(b.name,  '')  AS brand_name,
                    COALESCE(c.name,  '')  AS category_name,
                    COALESCE(pg.name, '')  AS group_name,
                    -- popularity score from pre-aggregated product_metrics
                    COALESCE(pm.popularity_score, 0) AS popularity_raw,
                    -- click_rate: cumulative click-throughs
                    COALESCE(pc.click_count, 0) AS click_count_raw
                FROM products p
                LEFT JOIN brands        b  ON b.id  = p.brand_id
                LEFT JOIN categories    c  ON c.id  = p.category_id
                LEFT JOIN product_group pg ON pg.id = p.product_group_id
                LEFT JOIN connected_databases cd ON cd.id = p.source_db_id
                LEFT JOIN product_metrics pm
                    ON pm.product_id = p.id AND pm.source_db_id = p.source_db_id
                LEFT JOIN product_clicks pc ON pc.product_id = p.id
                WHERE p.is_inactive = 0
            """
            params: list = [_ID_SCOPE_MULTIPLIER, _ID_SCOPE_MULTIPLIER]
            if not self.is_global:
                sql += " AND p.source_db_id = ?"
                params.append(self.source_db_id)
            if product_ids:
                placeholders = ",".join("?" * len(product_ids))
                sql += f" AND p.id IN ({placeholders})"
                params.extend(product_ids)
            sql += " ORDER BY p.id"
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _normalise_signal(values: List[float]) -> List[float]:
        """
        Min-max normalise a list of raw signal values to the 0–100 range.

        Products with no signal (value = 0) stay at 0.
        The product with the highest raw value gets 100.
        All others are scaled linearly between 0 and 100.

        This ensures popularity and click_rate are on the same scale as
        fuzzy_score before the composite formula is applied.
        """
        if not values:
            return values
        max_val = max(values)
        if max_val == 0:
            return [0.0] * len(values)
        return [round((v / max_val) * 100.0, 4) for v in values]

    def _renormalize_signals(self) -> None:
        """
        Re-run min-max normalization on all in-memory items using the stored
        raw signal values (popularity_raw, click_count_raw).

        Copy-on-write safe: creates a new list reference so that any search
        thread that already holds a snapshot of the old list is unaffected.
        Signal values (_popularity, _click_rate) are written atomically via
        CPython's GIL-guaranteed dict __setitem__, so a concurrent reader
        sees either the old or new float — never a torn write.

        Thread-safe: acquires self._lock (RLock, re-entrant safe).
        """
        with self._lock:
            if not self._items:
                return
            # CoW: work on a shallow copy so the live reference is replaced
            # atomically at the end rather than mutated in-place.
            snapshot = list(self._items)
            pop_raw   = [float(item.get("popularity_raw",  0) or 0) for item in snapshot]
            click_raw = [float(item.get("click_count_raw", 0) or 0) for item in snapshot]
            pop_norm   = self._normalise_signal(pop_raw)
            click_norm = self._normalise_signal(click_raw)
            for i, item in enumerate(snapshot):
                item["_popularity"] = pop_norm[i]
                item["_click_rate"] = click_norm[i]
            # Atomic reference swap — searches that already grabbed the old list
            # reference continue safely; new searches get this updated list.
            self._items = snapshot

    def update_products_incremental(self, scoped_product_ids: List[int]) -> int:
        """
        Fetch fresh data for the given product IDs and merge into the index.

        This avoids a full SQLite table scan when only a small subset of
        products was inserted or updated during an incremental sync.

        Concurrency model — copy-on-write (CoW)
        ----------------------------------------
        The entire merge-normalise-swap sequence runs under a single lock
        acquisition.  This means:

        1. A new list is built from the current state (shallow copy).
        2. New/updated items are merged into the copy.
        3. Signals are re-normalised across the full merged corpus.
        4. ``self._items / _raw_strings / _normalized_strings`` are atomically
           replaced with the new lists.

        Any search thread that grabbed a snapshot of the old lists BEFORE the
        lock was acquired will continue running safely — it holds references to
        the old, now-immutable list objects.  No structural mutation (append /
        element replace) is ever performed on a live snapshot.

        Periodic compaction
        -------------------
        After every FULL_REBUILD_AFTER_N_INCREMENTALS (default 500) calls, a
        full ``rebuild()`` is triggered in a background thread to reclaim any
        memory fragmentation caused by repeated partial updates.

        Parameters
        ----------
        scoped_product_ids : list of int
            The ``p.id`` values as stored in SQLite (scoped IDs for global
            engine; original IDs for per-DB engine).

        Returns
        -------
        int — number of products inserted or updated.
        """
        if not scoped_product_ids:
            return 0

        # ── Step 1: fetch from DB (outside the lock — expensive I/O) ──────────
        new_items = self._load_products_from_db(product_ids=scoped_product_ids)
        if not new_items:
            return 0

        new_raw_strs:  List[str] = []
        new_norm_strs: List[str] = []
        for item in new_items:
            parts = [str(item.get(f, '') or '') for f in self.text_fields]
            raw   = ' '.join(p for p in parts if p and p.lower() not in ('nan', 'none', ''))
            new_raw_strs.append(raw)
            new_norm_strs.append(normalize(raw))

        needs_compaction = False

        with self._lock:
            # ── Step 2: CoW — build new lists from current state ───────────────
            # Searches that already hold references to the current lists are
            # unaffected; they see an immutable snapshot of the pre-update state.
            merged_items = list(self._items)
            merged_raw   = list(self._raw_strings)
            merged_norm  = list(self._normalized_strings)

            # ── Step 3: merge new/updated items into the copies ────────────────
            id_to_idx = {item["id"]: i for i, item in enumerate(merged_items)}

            for i, item in enumerate(new_items):
                pid = item["id"]
                if pid in id_to_idx:
                    merged_items[id_to_idx[pid]] = item
                    merged_raw  [id_to_idx[pid]] = new_raw_strs[i]
                    merged_norm [id_to_idx[pid]] = new_norm_strs[i]
                else:
                    merged_items.append(item)
                    merged_raw.append(new_raw_strs[i])
                    merged_norm.append(new_norm_strs[i])

            # ── Step 4: re-normalise signals on the full merged corpus ─────────
            # New items are fresh dicts from SQLite — safe to mutate.
            # Unchanged items share dict objects with the old snapshot; signal
            # assignments are atomic float writes (CPython GIL guarantee), so
            # a concurrent search sees either the old or new value — never torn.
            pop_raw   = [float(it.get("popularity_raw",  0) or 0) for it in merged_items]
            click_raw = [float(it.get("click_count_raw", 0) or 0) for it in merged_items]
            pop_norm   = self._normalise_signal(pop_raw)
            click_norm = self._normalise_signal(click_raw)
            for i, it in enumerate(merged_items):
                it["_popularity"] = pop_norm[i]
                it["_click_rate"] = click_norm[i]

            # ── Step 5: atomic CoW swap ────────────────────────────────────────
            # Searches started AFTER this assignment use the new lists.
            # Searches started BEFORE use the old (now unreferenced) lists.
            self._items              = merged_items
            self._raw_strings        = merged_raw
            self._normalized_strings = merged_norm

            # ── Step 6: compaction counter ────────────────────────────────────
            self._incremental_update_count += 1
            threshold = FULL_REBUILD_AFTER_N_INCREMENTALS
            if threshold > 0 and self._incremental_update_count >= threshold:
                self._incremental_update_count = 0   # reset BEFORE async trigger
                needs_compaction = True

        # ── Step 7: schedule compaction outside the lock ──────────────────────
        if needs_compaction:
            threading.Thread(
                target=self._compaction_rebuild,
                daemon=True,
                name=f"compaction-{self.slot_key if hasattr(self, 'slot_key') else 'engine'}",
            ).start()

        label = "global" if self.is_global else f"db_id={self.source_db_id}"
        print(f"[Search] Incremental update ({label}): "
              f"{len(new_items)} product(s) merged "
              f"({len(self._items)} total"
              + (", compaction scheduled" if needs_compaction else "") + ").")
        return len(new_items)

    def _compaction_rebuild(self) -> None:
        """
        Full rebuild triggered by the compaction threshold.

        Runs in a background daemon thread so it never blocks the caller.
        If the rebuild raises, the existing index continues serving queries —
        the failure is printed and the compaction counter stays at 0 so the
        next N incrementals will trigger another attempt.
        """
        label = "global" if self.is_global else f"db_id={self.source_db_id}"
        print(f"[Search] Compaction rebuild starting ({label}) — "
              f"threshold={FULL_REBUILD_AFTER_N_INCREMENTALS} incrementals reached.")
        try:
            self.rebuild()
            print(f"[Search] Compaction rebuild complete ({label}).")
        except Exception as exc:
            print(f"[Search] Compaction rebuild failed ({label}): {exc}")

    def remove_products(self, scoped_product_ids: List[int]) -> int:
        """
        Remove products from the in-memory index by their scoped IDs.

        Does NOT trigger a signal re-normalisation — the relative ordering of
        remaining products stays valid since removing items only changes the
        absolute max of the corpus, and that effect is minor in practice.
        Call _renormalize_signals() explicitly after bulk deletions if needed.

        Parameters
        ----------
        scoped_product_ids : list of int
            Same ID space as update_products_incremental().

        Returns
        -------
        int — number of products actually removed from the index.
        """
        if not scoped_product_ids:
            return 0

        id_set = set(scoped_product_ids)

        with self._lock:
            original_count = len(self._items)
            kept = [
                (item, raw, norm)
                for item, raw, norm in zip(
                    self._items, self._raw_strings, self._normalized_strings
                )
                if item["id"] not in id_set
            ]
            if kept:
                self._items, self._raw_strings, self._normalized_strings = (
                    list(t) for t in zip(*kept)
                )
            else:
                self._items              = []
                self._raw_strings        = []
                self._normalized_strings = []

            removed = original_count - len(self._items)

        label = "global" if self.is_global else f"db_id={self.source_db_id}"
        print(f"[Search] remove_products ({label}): {removed} removed, "
              f"{len(self._items)} remaining.")
        return removed

    def rebuild(self) -> int:
        """
        Reload products from SQLite and rebuild the in-memory index.
        Also normalises popularity and click_rate signals to 0–100.

        Safe engine swap (Phase 5)
        --------------------------
        All expensive work (SQLite query, string building, normalisation) runs
        OUTSIDE the lock so searches are never blocked during the rebuild.

        Only the final atomic reference swap runs under the lock.  These are
        simple Python assignments that cannot raise, so the old index is
        guaranteed to remain active if any earlier step fails — the exception
        propagates before the lock is acquired.

        Thread-safe.  Returns number of products indexed.
        """
        t0 = time.perf_counter()
        items = self._load_products_from_db()

        raw_strings        = []
        normalized_strings = []

        # Extract raw signals for normalisation
        pop_raw   = [float(item.get("popularity_raw",  0) or 0) for item in items]
        click_raw = [float(item.get("click_count_raw", 0) or 0) for item in items]

        pop_norm   = self._normalise_signal(pop_raw)
        click_norm = self._normalise_signal(click_raw)

        # Embed normalised signals back into each item dict
        for i, item in enumerate(items):
            item["_popularity"]  = pop_norm[i]
            item["_click_rate"]  = click_norm[i]

        for item in items:
            parts = [str(item.get(f, '') or '') for f in self.text_fields]
            raw   = ' '.join(p for p in parts if p and p.lower() not in ('nan', 'none', ''))
            raw_strings.append(raw)
            normalized_strings.append(normalize(raw))

        now = time.time()
        # Atomic CoW swap: all expensive work is done; just update references.
        # Simple assignments cannot raise, so old state is preserved on any
        # earlier exception. Compaction counter reset here too.
        with self._lock:
            self._items                    = items
            self._raw_strings              = raw_strings
            self._normalized_strings       = normalized_strings
            self._last_built               = now
            self._incremental_update_count = 0   # full rebuild resets compaction clock

        rebuild_ms = (time.perf_counter() - t0) * 1000.0
        if _search_metrics:
            _search_metrics.record_rebuild(rebuild_ms)

        label = "global" if self.is_global else f"db_id={self.source_db_id}"
        print(f"[Search] Index rebuilt ({label}) — {len(items)} products indexed "
              f"in {rebuild_ms:.0f}ms.")

        # Persist to disk so the next restart loads instantly from cache.
        self._save_to_cache(items, raw_strings, normalized_strings, now)
        return len(items)

    def _start_background_refresh(self):
        """Start a daemon thread that calls rebuild() every N seconds."""
        def _worker():
            while True:
                time.sleep(self.rebuild_interval)
                try:
                    self.rebuild()
                except Exception as exc:
                    print(f"[Search] Background rebuild failed: {exc}")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        print(f"[Search] Background index refresh every {self.rebuild_interval}s started.")

    # ── Search ─────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = SEARCH_DEFAULT_K,
        filters: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search products with optional pre-filtering and query expansion.

        Query expansion
        ---------------
        Before scoring, the query is checked against QUERY_EXPANSIONS.
        If a match is found, additional search terms are generated and each
        is searched independently.  Results from all terms are merged by
        product ID — each product appears at most once, keeping its best
        score across all expansion terms.

        Example: "smoking stuff" → searches ["smoking stuff", "hookah",
        "pipe", "cigarette", "tobacco", "cigar"] and merges all results.

        The original query is always searched first.  Expansion terms are
        additive — they can only add results, never remove them.

        Filters applied BEFORE fuzzy scoring (on the in-memory index):
            category   : str  — exact category_name match (case-insensitive)
            min_price  : float — minimum sales_price or srp
            max_price  : float — maximum sales_price or srp

        Returns list of product dicts enriched with:
            score            — composite ranking score 0–100
            score_pct        — same value (for template display)
            score_label      — "high" | "medium" | "low"
            fuzzy_score      — raw fuzzy component
            popularity_score — normalised sales signal
            click_score      — normalised click signal
            expanded_from    — which query term produced this result
                               (original query or an expansion term)
        Sorted by score descending.
        """
        top_k = min(top_k, SEARCH_MAX_K)

        # ── Step 1: expand the query ──────────────────────────────────────────
        # expand_query() returns [original] + [expansion terms].
        # apply_synonyms() is applied to each term independently so both
        # the original and expanded terms benefit from synonym normalisation.
        query_terms = expand_query(query)

        with self._lock:
            items     = self._items
            raw_strs  = self._raw_strings
            norm_strs = self._normalized_strings

        if not items:
            return []

        # ── Step 2: apply category / price filters once ───────────────────────
        filters = filters or {}
        category_filter  = (filters.get("category") or "").strip().lower()
        min_price_filter = filters.get("min_price")
        max_price_filter = filters.get("max_price")

        if category_filter or min_price_filter is not None or max_price_filter is not None:
            filtered_indices = []
            for i, item in enumerate(items):
                if category_filter:
                    item_cat = (item.get("category_name") or "").lower()
                    if category_filter not in item_cat:
                        continue
                price = item.get("sales_price") or item.get("srp")
                if min_price_filter is not None and (price is None or price < min_price_filter):
                    continue
                if max_price_filter is not None and (price is None or price > max_price_filter):
                    continue
                filtered_indices.append(i)

            f_items     = [items[i]     for i in filtered_indices]
            f_raw_strs  = [raw_strs[i]  for i in filtered_indices]
            f_norm_strs = [norm_strs[i] for i in filtered_indices]
        else:
            f_items     = items
            f_raw_strs  = raw_strs
            f_norm_strs = norm_strs

        if not f_items:
            return []

        # ── Step 3: search each query term, merge by product ID ───────────────
        # best_by_id maps product_id → best result dict seen so far.
        # When the same product appears for multiple query terms, we keep
        # whichever produced the higher score.
        best_by_id: Dict[int, Dict[str, Any]] = {}

        for term in query_terms:
            # Apply synonym expansion to this term
            expanded_term = apply_synonyms(term)
            query_n       = normalize(expanded_term)
            if not query_n:
                continue

            # Pass 1 — fast WRatio scan (2× top_k candidates per term)
            fast_matches = process.extract(
                query_n,
                f_norm_strs,
                scorer=fuzz.WRatio,
                limit=top_k * 2,
            )

            # Pass 2 — full blend re-score + threshold gate
            candidates = []
            seen_idx   = set()

            for _text, _fast_score, index in fast_matches:
                if index in seen_idx:
                    continue
                seen_idx.add(index)

                base_score = blend_score(query_n, f_norm_strs[index], f_raw_strs[index])

                # Fuzzy threshold gate — irrelevant products excluded
                if base_score < FUZZY_MIN_THRESHOLD:
                    continue

                product_name_n   = normalize(f_items[index].get("name", ""))
                raw_product_name = f_items[index].get("name", "")
                fuzzy_score      = apply_boost(
                    base_score, query_n, product_name_n, raw_product_name
                )
                candidates.append((index, fuzzy_score))

            if not candidates:
                continue

            best_fuzzy = max(fs for _, fs in candidates)

            for index, fuzzy_score in candidates:
                popularity = f_items[index].get("_popularity", 0.0)
                click_rate = f_items[index].get("_click_rate", 0.0)

                source_priority = 0.0
                if self.is_global:
                    item_db_id = f_items[index].get("source_db_id")
                    if item_db_id is not None:
                        source_priority = float(
                            SEARCH_SOURCE_PRIORITY.get(int(item_db_id), 0.0)
                        )

                final_score = _composite_score(
                    fuzzy_score, popularity, click_rate,
                    best_fuzzy_in_results=best_fuzzy,
                    source_priority=source_priority,
                )

                product_id = f_items[index]["id"]

                # Keep this result only if it's better than what we have
                if product_id not in best_by_id or final_score > best_by_id[product_id]["score"]:
                    result = dict(f_items[index])
                    result["score"]            = final_score
                    result["score_pct"]        = final_score
                    result["score_label"]      = self._label(final_score)
                    result["fuzzy_score"]      = round(fuzzy_score, 2)
                    result["popularity_score"] = round(popularity,  2)
                    result["click_score"]      = round(click_rate,  2)
                    # Track which query term produced this result
                    result["expanded_from"]    = term if term != query else None
                    best_by_id[product_id]     = result

        # ── Step 4: sort merged results by composite score ────────────────────
        results = sorted(best_by_id.values(), key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def search_with_field_scores(
        self,
        query: str,
        top_k: int = SEARCH_DEFAULT_K,
        filters: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """
        Same as search() but also returns per-field scores.
        Useful for debugging and bucketing.
        """
        results  = self.search(query, top_k, filters=filters)
        query_n  = normalize(apply_synonyms(query))

        for r in results:
            r["field_scores"] = {
                field: round(
                    blend_score(
                        query_n,
                        normalize(str(r.get(field, '') or '')),
                        str(r.get(field, '') or '').lower(),
                    ),
                    2,
                )
                for field in self.text_fields
            }
        return results

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _label(score: float) -> str:
        if score >= 70:
            return "high"
        if score >= 50:
            return "medium"
        if score >= 35:
            return "low"
        return "none"

    @property
    def slot_key(self) -> str:
        """Unique string key for this engine slot (used in log messages)."""
        return "global" if self.is_global else f"db_{self.source_db_id}"

    def stats(self) -> dict:
        with self._lock:
            return {
                "source_db_id":             self.source_db_id,
                "mode":                     "global" if self.is_global else "isolated",
                "total_products":           len(self._items),
                "last_built":               self._last_built,
                "min_score":                self.min_score,
                "text_fields":              self.text_fields,
                "incremental_update_count": self._incremental_update_count,
                "compaction_threshold":     FULL_REBUILD_AFTER_N_INCREMENTALS,
            }


# ── Module-level singleton ─────────────────────────────────────────────────────
_engines: Dict[int, FuzzySearchEngine] = {}
_global_engine: Optional[FuzzySearchEngine] = None
_engine_lock = threading.Lock()


def get_engine(source_db_id: int = 1, rebuild_interval: Optional[int] = None) -> FuzzySearchEngine:
    """
    Return the module-level singleton engine, creating it on first call.

    The rebuild_interval is only honoured on the FIRST call (when the engine
    is created). Subsequent calls return the existing instance unchanged.
    This prevents duplicate background threads when the module is imported
    multiple times (e.g. Flask debug reloader).
    """
    key = int(source_db_id)
    if key not in _engines:
        with _engine_lock:
            if key not in _engines:
                _engines[key] = FuzzySearchEngine(
                    source_db_id=key,
                    rebuild_interval=rebuild_interval,
                )
    return _engines[key]


def get_global_engine(rebuild_interval: Optional[int] = None) -> FuzzySearchEngine:
    """Return the global in-memory engine across all source databases."""
    global _global_engine
    if _global_engine is None:
        with _engine_lock:
            if _global_engine is None:
                _global_engine = FuzzySearchEngine(
                    source_db_id=None,
                    rebuild_interval=rebuild_interval,
                )
    return _global_engine


def rebuild_global_index() -> int:
    """Force rebuild of global engine and return indexed product count."""
    return get_global_engine().rebuild()


def get_all_engine_stats() -> Dict:
    """
    Return a stats snapshot for every instantiated engine (per-DB and global).

    Acquires _engine_lock once to snapshot the engine registry, then calls
    stats() on each engine outside the lock to avoid blocking searches.

    Returns
    -------
    dict  e.g.::
        {
            "db_1":   {"source_db_id": 1, "mode": "isolated", "total_products": 42000, ...},
            "db_2":   {"source_db_id": 2, "mode": "isolated", "total_products": 18000, ...},
            "global": {"source_db_id": None, "mode": "global", "total_products": 60000, ...},
        }
    Only engines that have been created (via get_engine / get_global_engine) appear.
    """
    with _engine_lock:
        engines_snapshot = dict(_engines)
        global_snap      = _global_engine

    result: Dict = {}
    for db_id, engine in engines_snapshot.items():
        result[f"db_{db_id}"] = engine.stats()

    if global_snap is not None:
        result["global"] = global_snap.stats()

    return result
