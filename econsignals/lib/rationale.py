"""Grounded "why this matters" rationale generator (flag-gated, default off).

Produces a one- to two-line, evidence-grounded reason a specific paper matches
THIS user's research profile (applied-micro / development / urban / India /
agricultural misallocation). The rationale is built only from material the user
actually overlaps with: the matched interest keywords (from
``relevance.load_interest_keywords``) and the titles of the nearest Zotero
neighbours. The model is instructed to cite that real overlap and to emit an
empty string when no genuine overlap exists, so the feature cannot manufacture a
relevance story for an off-topic paper.

Default behaviour is a no-op: ``rationale_for`` returns ``None`` unless the
feature is explicitly enabled with ``ECONSIGNALS_RATIONALE`` in {1,true,yes,on}
AND an ``ANTHROPIC_API_KEY`` is set. Requiring an explicit opt-in flag (not mere
key presence) keeps the free / local install free: a globally-set key for other
tools must never silently incur spend here. When enabled, a cheap model
(claude-haiku-4-5) is called via the stdlib ``urllib`` (no anthropic SDK), and
every paper is summarised at most once ever: results are cached per paper to
``data/rationale_cache.json``.

Cost note: with a key set, each new paper incurs one Haiku Messages API call.
The cache bounds this to one call per distinct paper, but the spend is real and
ongoing as new papers arrive.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from hashlib import sha1
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cache lives beside the SQLite DB: <proj_root>/data/rationale_cache.json.
# parents[0] = lib, parents[1] = econsignals, parents[2] = project root.
_CACHE_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "rationale_cache.json"

_API_URL: str = "https://api.anthropic.com/v1/messages"
_API_MODEL: str = "claude-haiku-4-5"
_API_VERSION: str = "2023-06-01"
_MAX_TOKENS: int = 120
_REQUEST_TIMEOUT: int = 30

# Anthropic's HTTPS endpoint needs valid CA roots; macOS Python often lacks the
# system trust store, so prefer certifi (a core dependency), mirroring
# sensors/_base.py. Without this the POST fails with CERTIFICATE_VERIFY_FAILED.
try:
    import certifi

    _SSL_CTX: ssl.SSLContext = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - certifi is in requirements
    _SSL_CTX = ssl.create_default_context()

# Cap grounding material so the prompt stays small and cheap.
_MAX_TERMS: int = 12
_MAX_NEIGHBORS: int = 5
_ABSTRACT_CHARS: int = 1200

# The system prompt anchors the persona and, critically, the anti-hallucination
# contract: cite only the real overlap, emit nothing when there is none.
_SYSTEM_PROMPT: str = (
    "You write a one- to two-sentence note explaining why a specific economics "
    "paper matters to ONE researcher: an applied microeconomist working on "
    "development, urban, and public economics in India, including agricultural "
    "misallocation. You are given the paper, the researcher's interest terms "
    "that the paper actually matched, and titles of the researcher's own library "
    "papers nearest to it. Ground the note ONLY in that real overlap: name the "
    "specific shared topic, method, or setting. Do not invent a connection. Do "
    "not restate the abstract. If there is no genuine overlap with the "
    "researcher's interests, output an empty string and nothing else."
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paper identity / cache keys
# ---------------------------------------------------------------------------


def _paper_key(paper: dict) -> str:
    """Return a stable cache key for a paper.

    Prefers the DOI, then a source identifier, then a hash of the title. This
    keys the cache so each distinct paper is summarised at most once ever.

    Args:
        paper: Paper dict with optional ``doi``, ``source_id`` / ``id``, ``title``.

    Returns:
        A non-empty cache-key string.
    """
    doi = (paper.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"

    for field in ("source_id", "id", "paper_id"):
        val = paper.get(field)
        if val not in (None, ""):
            return f"id:{val}"

    title = (paper.get("title") or "").strip().lower()
    return f"title:{sha1(title.encode('utf-8')).hexdigest()}"


# ---------------------------------------------------------------------------
# Cache (data/rationale_cache.json)
# ---------------------------------------------------------------------------


def _load_cache() -> dict[str, str]:
    """Load the rationale cache, returning an empty dict if absent or corrupt."""
    if not _CACHE_PATH.exists():
        return {}
    try:
        with _CACHE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _log.debug("Rationale cache load failed: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only string values; tolerate older / partial caches.
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def _save_cache(cache: dict[str, str]) -> None:
    """Persist the rationale cache to disk, ignoring write failures."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _CACHE_PATH.open("w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        _log.warning("Failed to save rationale cache: %s", exc)


# ---------------------------------------------------------------------------
# Grounding terms
# ---------------------------------------------------------------------------


def matched_interest_terms(paper: dict, interest_kw: set[str] | None = None) -> list[str]:
    """Return the user's interest phrases that the paper text actually contains.

    Whole-word / phrase matches the user's interest keywords against the paper's
    title and abstract, so the prompt is grounded in real overlap rather than the
    full interest list. Loads the keyword set lazily from ``relevance`` when not
    supplied.

    Args:
        paper: Paper dict with ``title`` and ``abstract``.
        interest_kw: Optional pre-loaded interest phrases; loaded if None.

    Returns:
        Ordered, de-duplicated list of matched phrases (capped at ``_MAX_TERMS``).
    """
    if interest_kw is None:
        from econsignals.lib.relevance import load_interest_keywords

        interest_kw = load_interest_keywords()

    if not interest_kw:
        return []

    import re

    combined = f"{paper.get('title') or ''} {paper.get('abstract') or ''}".lower()
    matched: list[str] = []
    for phrase in sorted(interest_kw):
        if re.search(rf"\b{re.escape(phrase)}\b", combined):
            matched.append(phrase)
    return matched[:_MAX_TERMS]


# ---------------------------------------------------------------------------
# Prompt building (pure; unit-testable without network)
# ---------------------------------------------------------------------------


def build_request_payload(
    paper: dict,
    matched_terms: list[str],
    neighbors: list[str] | None,
) -> dict:
    """Build the Anthropic Messages API request body for one paper.

    The payload grounds the model on the paper, the matched interest terms, and
    the nearest Zotero neighbour titles. Exposed separately so tests can inspect
    the grounding content without making a network call.

    Args:
        paper: Paper dict with ``title`` and ``abstract``.
        matched_terms: Interest phrases the paper actually matched.
        neighbors: Titles of the nearest Zotero library papers (may be None).

    Returns:
        A dict ready to JSON-encode as the Messages API request body.
    """
    title = (paper.get("title") or "").strip()
    abstract = (paper.get("abstract") or "").strip()[:_ABSTRACT_CHARS]

    terms_line = ", ".join(matched_terms) if matched_terms else "(none matched)"
    neighbor_titles = [t.strip() for t in (neighbors or []) if (t or "").strip()][
        :_MAX_NEIGHBORS
    ]
    if neighbor_titles:
        neighbors_block = "\n".join(f"- {t}" for t in neighbor_titles)
    else:
        neighbors_block = "(none)"

    user_content = (
        f"PAPER TITLE: {title}\n"
        f"PAPER ABSTRACT: {abstract or '(no abstract)'}\n\n"
        f"MATCHED INTEREST TERMS: {terms_line}\n\n"
        f"NEAREST LIBRARY PAPERS:\n{neighbors_block}\n\n"
        "Write the grounded one- to two-sentence note now, citing the specific "
        "overlap above. If there is no genuine overlap, output an empty string."
    )

    return {
        "model": _API_MODEL,
        "max_tokens": _MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }


# ---------------------------------------------------------------------------
# API call (lazy; only reached when ANTHROPIC_API_KEY is set)
# ---------------------------------------------------------------------------


def _extract_text(response: dict) -> str:
    """Concatenate the text blocks of a Messages API response.

    Args:
        response: Decoded Messages API response body.

    Returns:
        The joined text of every ``type == "text"`` content block, stripped.
    """
    blocks = response.get("content") or []
    parts = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(parts).strip()


def _call_anthropic(payload: dict, api_key: str) -> str | None:
    """POST one rationale request to the Anthropic Messages API.

    Args:
        payload: Request body from ``build_request_payload``.
        api_key: Anthropic API key.

    Returns:
        The model's text reply (possibly empty), or None on any transport or
        decode failure.
    """
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT, context=_SSL_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.warning("Anthropic rationale request failed: %s", exc)
        return None
    return _extract_text(body)


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def _enabled_api_key() -> str | None:
    """Return the Anthropic key only when the rationale feature is explicitly on.

    Two gates, both required, so a globally-set ANTHROPIC_API_KEY (commonly
    present for other tools) never silently incurs spend: the user must opt in
    with ECONSIGNALS_RATIONALE in {1,true,yes,on} AND have a key set.

    Returns:
        The API key string when enabled and present, else None.
    """
    flag = os.environ.get("ECONSIGNALS_RATIONALE", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None
    return os.environ.get("ANTHROPIC_API_KEY", "").strip() or None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rationale_for(
    paper: dict,
    matched_terms: list[str] | None = None,
    neighbors: list[str] | None = None,
) -> str | None:
    """Return a grounded "why this matters" note for a paper, or None.

    Default no-op: returns None unless ``ANTHROPIC_API_KEY`` is set. When set,
    returns a cached rationale if present, else calls claude-haiku-4-5 once and
    caches the result keyed by the paper's DOI / source id / title hash. An empty
    model reply (no genuine overlap) is cached as "" and surfaced as None, so the
    paper is not re-queried on later runs.

    Args:
        paper: Paper dict with at least ``title``; ``abstract``, ``doi``,
            ``source_id`` used when present.
        matched_terms: Interest phrases the paper matched; computed from the
            user's profile when None.
        neighbors: Titles of the nearest Zotero library papers (optional).

    Returns:
        A short rationale string, or None when disabled, on failure, or when the
        model reports no genuine overlap.
    """
    api_key = _enabled_api_key()
    if not api_key:
        return None

    key = _paper_key(paper)
    cache = _load_cache()
    if key in cache:
        cached = cache[key]
        return cached or None

    if matched_terms is None:
        matched_terms = matched_interest_terms(paper)

    payload = build_request_payload(paper, matched_terms, neighbors)
    text = _call_anthropic(payload, api_key)
    if text is None:
        # Transport failure: do not cache, so a later run can retry.
        return None

    # Cache the verdict (including "" for no-overlap) so each paper is paid for
    # at most once ever.
    cache[key] = text
    _save_cache(cache)
    return text or None


def rationale_batch(
    papers: list[dict],
    neighbors_by_key: dict[str, list[str]] | None = None,
) -> dict[str, str | None]:
    """Generate rationales for many papers, reusing one cache read/write cycle.

    Loads the cache once, fills missing entries via the API (only when a key is
    set), and writes the cache once at the end. Returns a mapping keyed the same
    way as the on-disk cache so callers can look results up per paper.

    Args:
        papers: List of paper dicts.
        neighbors_by_key: Optional map from paper cache key (``_paper_key``) to
            that paper's nearest Zotero neighbour titles.

    Returns:
        Mapping of paper cache key -> rationale string or None.
    """
    api_key = _enabled_api_key()
    if not api_key:
        return {_paper_key(p): None for p in papers}

    cache = _load_cache()
    dirty = False
    out: dict[str, str | None] = {}

    for paper in papers:
        key = _paper_key(paper)
        if key in cache:
            out[key] = cache[key] or None
            continue

        matched_terms = matched_interest_terms(paper)
        neighbors = (neighbors_by_key or {}).get(key)
        payload = build_request_payload(paper, matched_terms, neighbors)
        text = _call_anthropic(payload, api_key)
        if text is None:
            out[key] = None  # transport failure; retry on a later run
            continue

        cache[key] = text
        dirty = True
        out[key] = text or None

    if dirty:
        _save_cache(cache)
    return out
