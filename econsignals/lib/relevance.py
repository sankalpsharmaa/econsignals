"""Relevance scoring engine for EconSignals.

Scores papers on a 0-1 scale using a weighted combination of JEL code match,
keyword overlap, author proximity, source prestige, recency, and social signal.
An India boost of +0.15 is applied when the paper has clear India relevance.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# File lives at: <proj_root>/econsignals/lib/relevance.py
# parents[0] = lib, parents[1] = econsignals, parents[2] = project root
PROJ_ROOT: Path = Path(__file__).resolve().parents[2]

_JEL_WEIGHTS_PATH: Path = PROJ_ROOT / "profile" / "jel_weights.json"
_IDENTITY_PATH: Path = PROJ_ROOT / "profile" / "identity.md"

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

_W_JEL: float = 0.30
_W_KW: float = 0.20
_W_AUTHOR: float = 0.25
_W_PRESTIGE: float = 0.10
_W_RECENCY: float = 0.10
_W_SOCIAL: float = 0.05
_INDIA_BOOST: float = 0.15

_RECENCY_PEAK_DAYS: int = 7
_RECENCY_ZERO_DAYS: int = 90

# ---------------------------------------------------------------------------
# Source prestige tables
# ---------------------------------------------------------------------------

_TOP5_JOURNALS: frozenset[str] = frozenset({
    "american economic review", "aer",
    "quarterly journal of economics", "qje",
    "econometrica",
    "journal of political economy", "jpe",
    "review of economic studies", "restud",
})

_TOP_FIELD_JOURNALS: frozenset[str] = frozenset({
    "journal of development economics", "jde",
    "journal of urban economics", "jue",
    "journal of human resources", "jhr",
    "journal of labor economics", "jle",
    "journal of public economics", "jpube", "journal of public economics",
})

_PRESTIGE_SOURCES: dict[str, float] = {
    "nber": 1.0,
    "openalex": 0.5,   # treated as generic WP unless journal detected
    "semantic_scholar": 0.5,
    "repec_nep": 0.5,
    "crossref": 0.35,
    "worldbank": 0.5,
    "imf": 0.5,
    "iza": 0.5,
    "bread": 0.5,
    "igc": 0.4,
    "india_think_tanks": 0.4,
    "rbi": 0.4,
    "mospi": 0.4,
    "jpal_sa": 0.4,
    "bluesky": 0.2,
    "mastodon": 0.2,
    "twitter_bridge": 0.2,
}

# ---------------------------------------------------------------------------
# India detection
# ---------------------------------------------------------------------------

_INDIA_INSTITUTIONS: str = (
    r"india|iit|iim\b|isi\b|jnu\b|delhi|mumbai|ncaer|"
    r"\bcpr\b|icrier|bangalore|kolkata|chennai|hyderabad|pune"
)
_INDIA_PATTERNS: re.Pattern[str] = re.compile(
    r"\bindia\b|" + _INDIA_INSTITUTIONS,
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Module-level cache (populated on first use)
# ---------------------------------------------------------------------------

_jel_weights_cache: dict[str, float] | None = None
_interest_kw_cache: set[str] | None = None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_jel_weights() -> dict[str, float]:
    """Load JEL weights from profile/jel_weights.json.

    Returns:
        Mapping of JEL code -> weight, including a "_default" key.

    Example:
        >>> weights = load_jel_weights()
        >>> weights["O12"]
        1.0
        >>> weights["_default"]
        0.1
    """
    global _jel_weights_cache
    if _jel_weights_cache is not None:
        return _jel_weights_cache
    try:
        with _JEL_WEIGHTS_PATH.open("r", encoding="utf-8") as fh:
            data: dict[str, float] = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"_default": 0.1}
    _jel_weights_cache = data
    return data


def load_interest_keywords() -> set[str]:
    """Parse profile/identity.md and extract the Topics of Interest keywords.

    Reads every bullet line under the "## Topics of Interest" section,
    splits on commas and whitespace, and lower-cases all tokens.

    Returns:
        Set of lowercase keyword strings.

    Example:
        >>> kw = load_interest_keywords()
        >>> "urbanization" in kw
        True
    """
    global _interest_kw_cache
    if _interest_kw_cache is not None:
        return _interest_kw_cache

    keywords: set[str] = set()
    try:
        text = _IDENTITY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        _interest_kw_cache = keywords
        return keywords

    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Topics of Interest"):
            in_section = True
            continue
        if in_section:
            # Stop at the next section header
            if stripped.startswith("##"):
                break
            # Strip leading bullet markers and parse comma-separated tokens
            content = re.sub(r"^[-*]\s*", "", stripped)
            if content:
                for token in re.split(r"[,\s]+", content):
                    token = token.strip().lower()
                    if token:
                        keywords.add(token)

    _interest_kw_cache = keywords
    return keywords


# ---------------------------------------------------------------------------
# Individual scorers
# ---------------------------------------------------------------------------


def score_jel(jel_codes: list[str], weights: dict[str, float]) -> float:
    """Score based on JEL code match using hierarchical lookup.

    For each JEL code tries the full code first ("O12"), then successive
    prefixes ("O1", "O"), then falls back to "_default". The score is the
    maximum weight across all of the paper's JEL codes.

    Args:
        jel_codes: List of JEL code strings (e.g. ["O12", "R31"]).
        weights: Mapping from JEL code -> float, with a "_default" key.

    Returns:
        Float in [0, 1]. Returns 0.0 if jel_codes is empty.

    Example:
        >>> score_jel(["O12", "Z99"], {"O12": 1.0, "_default": 0.1})
        1.0
    """
    if not jel_codes:
        return 0.0

    default = weights.get("_default", 0.1)
    best: float = 0.0

    for code in jel_codes:
        code = code.strip()
        if not code:
            continue
        # Try increasingly short prefixes: full code down to 1 char
        found: float = default
        for length in range(len(code), 0, -1):
            prefix = code[:length]
            if prefix in weights:
                found = weights[prefix]
                break
        if found > best:
            best = found

    return min(1.0, best)


def score_keywords(title: str, abstract: str, interest_kw: set[str]) -> float:
    """Token overlap between paper text and interest keywords.

    Tokenizes the combined title and abstract, then counts how many of the
    interest keywords appear. Score = matches / len(interest_kw), capped at 1.0.

    Args:
        title: Paper title string (may be None-ish; handled defensively).
        abstract: Paper abstract string.
        interest_kw: Set of lowercase interest keyword tokens.

    Returns:
        Float in [0, 1]. Returns 0.0 if interest_kw is empty.

    Example:
        >>> score_keywords("Housing in India", "", {"housing", "india"})
        1.0
    """
    if not interest_kw:
        return 0.0

    combined = f"{title or ''} {abstract or ''}".lower()
    # Tokenize to words (allow hyphens within words)
    tokens: set[str] = set(re.findall(r"[a-z][\w-]*", combined))

    matches = len(interest_kw & tokens)
    return min(1.0, matches / len(interest_kw))


def score_author_proximity(paper_id: int, db: sqlite3.Connection) -> float:
    """Score based on the most prominent author relationship to the user.

    Priority: tracked (1.0) > auto-discovered (0.7) > unknown (0.0).
    The co-author-of-tracked tier (0.5) is approximated by checking whether
    any non-tracked author shares a paper with any tracked author.

    Args:
        paper_id: Primary key of the paper in the DB.
        db: Open sqlite3 connection.

    Returns:
        Float in [0, 1].
    """
    rows = db.execute(
        """
        SELECT a.is_tracked, a.auto_discovered
        FROM authors a
        JOIN paper_authors pa ON pa.author_id = a.id
        WHERE pa.paper_id = ?
        """,
        (paper_id,),
    ).fetchall()

    if not rows:
        return 0.0

    best: float = 0.0
    has_unknown = False

    for row in rows:
        is_tracked: int = row[0] or 0
        auto_disc: int = row[1] or 0

        if is_tracked:
            return 1.0  # Can't do better
        elif auto_disc:
            best = max(best, 0.7)
        else:
            has_unknown = True

    # Check co-author-of-tracked: unknown authors who share a paper with a
    # tracked author via any other paper.
    if has_unknown and best < 0.5:
        coauthor_count = db.execute(
            """
            SELECT COUNT(DISTINCT pa2.author_id)
            FROM paper_authors pa1
            JOIN paper_authors pa2 ON pa2.paper_id = pa1.paper_id
            JOIN authors a2 ON a2.id = pa2.author_id
            WHERE pa1.paper_id = ?
              AND a2.is_tracked = 1
            """,
            (paper_id,),
        ).fetchone()[0]
        if coauthor_count > 0:
            best = max(best, 0.5)

    return best


def score_source_prestige(paper_id: int, db: sqlite3.Connection) -> float:
    """Score based on the publication venue.

    Checks the paper_sources table for both the source name and any journal
    name present in the raw_metadata. Top-5 journals and NBER = 1.0,
    top field journals = 0.8, other WP series = 0.5, think tanks = 0.4,
    blog/social = 0.2.

    Args:
        paper_id: Primary key of the paper.
        db: Open sqlite3 connection.

    Returns:
        Float in [0, 1]. Returns 0.3 as a conservative default if no
        source records are found.
    """
    rows = db.execute(
        "SELECT source, source_url, raw_metadata FROM paper_sources WHERE paper_id = ?",
        (paper_id,),
    ).fetchall()

    if not rows:
        return 0.3  # Unknown source

    best: float = 0.0

    for row in rows:
        source: str = (row[0] or "").lower().strip()
        source_url: str = (row[1] or "").lower().strip()
        raw_meta_str: str | None = row[2]

        # Check source-level prestige
        src_score = _PRESTIGE_SOURCES.get(source, 0.3)
        # Repository URLs surfaced via Crossref are often low-signal for this
        # feed; downweight to avoid Zenodo-heavy briefs crowding out econ work.
        if source_url:
            if "zenodo.org" in source_url:
                src_score = min(src_score, 0.1)
            elif "osf.io" in source_url or "figshare.com" in source_url:
                src_score = min(src_score, 0.2)

        # Check for journal name in raw_metadata
        journal_score: float = 0.0
        if raw_meta_str:
            try:
                meta: dict[str, Any] = json.loads(raw_meta_str)
                journal_score = _journal_prestige_from_meta(meta)
            except (json.JSONDecodeError, TypeError):
                pass

        best = max(best, src_score, journal_score)

    return min(1.0, best)


def _journal_prestige_from_meta(meta: dict[str, Any]) -> float:
    """Extract journal prestige score from raw_metadata dict."""
    # Common keys across OpenAlex, CrossRef, etc.
    venue_candidates: list[str] = []
    for key in ("journal", "venue", "journal_name", "container_title",
                "host_venue", "primary_location"):
        val = meta.get(key)
        if isinstance(val, str):
            venue_candidates.append(val.lower())
        elif isinstance(val, dict):
            # OpenAlex host_venue / primary_location structure
            display = val.get("display_name") or val.get("name") or ""
            if display:
                venue_candidates.append(display.lower())

    for venue in venue_candidates:
        for name in _TOP5_JOURNALS:
            if name in venue:
                return 1.0
        for name in _TOP_FIELD_JOURNALS:
            if name in venue:
                return 0.8

    return 0.0


def score_recency(published_at: str | None) -> float:
    """Linear decay from 1.0 (within 7 days) to 0.0 (90+ days old).

    Args:
        published_at: ISO 8601 date or datetime string (e.g. "2024-03-15" or
                      "2024-03-15T00:00:00Z"). None or unparseable returns 0.5.

    Returns:
        Float in [0, 1].

    Example:
        >>> import datetime
        >>> today = datetime.date.today().isoformat()
        >>> score_recency(today)
        1.0
    """
    if not published_at:
        return 0.5  # Unknown publication date; give neutral score

    pub_date: datetime | None = _parse_date(published_at)
    if pub_date is None:
        return 0.5

    today = datetime.now(timezone.utc)
    age_days = (today - pub_date).days

    if age_days <= _RECENCY_PEAK_DAYS:
        return 1.0
    if age_days >= _RECENCY_ZERO_DAYS:
        return 0.0

    # Linear decay between peak and zero
    span = _RECENCY_ZERO_DAYS - _RECENCY_PEAK_DAYS
    elapsed = age_days - _RECENCY_PEAK_DAYS
    return max(0.0, 1.0 - elapsed / span)


def _parse_date(date_str: str) -> datetime | None:
    """Try common ISO formats and return a timezone-aware datetime or None."""
    formats = (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m",
        "%Y",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def score_social(paper_id: int, db: sqlite3.Connection) -> float:
    """Score based on social media attention.

    3+ linked social_items = 1.0, 1-2 = 0.5, 0 = 0.0.

    Args:
        paper_id: Primary key of the paper.
        db: Open sqlite3 connection.

    Returns:
        0.0, 0.5, or 1.0.
    """
    count: int = db.execute(
        "SELECT COUNT(*) FROM social_items WHERE paper_id = ?",
        (paper_id,),
    ).fetchone()[0]

    if count >= 3:
        return 1.0
    if count >= 1:
        return 0.5
    return 0.0


def india_boost(
    title: str, abstract: str, paper_id: int, db: sqlite3.Connection
) -> float:
    """Return 0.15 if the paper has clear India relevance, else 0.0.

    Checks the title, abstract, and author affiliations / country codes.

    Args:
        title: Paper title.
        abstract: Paper abstract.
        paper_id: Primary key of the paper.
        db: Open sqlite3 connection.

    Returns:
        0.15 or 0.0.
    """
    combined = f"{title or ''} {abstract or ''}"
    if _INDIA_PATTERNS.search(combined):
        return _INDIA_BOOST

    # Check authors
    rows = db.execute(
        """
        SELECT a.country, a.affiliation
        FROM authors a
        JOIN paper_authors pa ON pa.author_id = a.id
        WHERE pa.paper_id = ?
        """,
        (paper_id,),
    ).fetchall()

    for row in rows:
        country: str = (row[0] or "").strip().upper()
        affiliation: str = row[1] or ""

        if country == "IN":
            return _INDIA_BOOST
        if _INDIA_PATTERNS.search(affiliation):
            return _INDIA_BOOST

    return 0.0


# ---------------------------------------------------------------------------
# Composite scorer
# ---------------------------------------------------------------------------


def score_paper(
    paper: dict,
    db: sqlite3.Connection,
    jel_weights: dict[str, float],
    interest_kw: set[str],
) -> float:
    """Compute the final relevance score for a paper dict.

    Weighted sum of six signals plus an optional India boost, clamped to [0, 1].

    Args:
        paper: Paper dict (as returned by get_recent_papers). Must contain at
               minimum an "id" field. All other fields are accessed defensively.
        db: Open sqlite3 connection.
        jel_weights: Output of load_jel_weights().
        interest_kw: Output of load_interest_keywords().

    Returns:
        Float in [0, 1].
    """
    paper_id: int = paper["id"]
    title: str = paper.get("title") or ""
    abstract: str = paper.get("abstract") or ""

    jel_codes: list[str] = paper.get("jel_codes") or []
    if isinstance(jel_codes, str):
        # Defensive: still a JSON string
        try:
            jel_codes = json.loads(jel_codes)
        except (json.JSONDecodeError, TypeError):
            jel_codes = []

    s_jel = score_jel(jel_codes, jel_weights)
    s_kw = score_keywords(title, abstract, interest_kw)
    s_author = score_author_proximity(paper_id, db)
    s_prestige = score_source_prestige(paper_id, db)
    s_recency = score_recency(paper.get("published_at"))
    s_social = score_social(paper_id, db)
    boost = india_boost(title, abstract, paper_id, db)

    weighted = (
        _W_JEL * s_jel
        + _W_KW * s_kw
        + _W_AUTHOR * s_author
        + _W_PRESTIGE * s_prestige
        + _W_RECENCY * s_recency
        + _W_SOCIAL * s_social
    )

    return min(1.0, weighted + boost)


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def score_all_papers(days: int = 7) -> int:
    """Score all papers ingested in the last N days and persist scores to DB.

    Loads JEL weights and interest keywords once, then scores each paper
    using a single shared DB connection. Updates relevance_score in the DB
    for every paper processed.

    Args:
        days: Look-back window in calendar days (passed to get_recent_papers).

    Returns:
        Number of papers scored.
    """
    from econsignals.lib.db import get_db, get_recent_papers, update_paper_relevance

    jel_weights = load_jel_weights()
    interest_kw = load_interest_keywords()

    # Use a large limit to cover all papers in the window
    papers = get_recent_papers(days=days, limit=10_000)

    db = get_db()
    count = 0
    try:
        for paper in papers:
            score = score_paper(paper, db, jel_weights, interest_kw)
            update_paper_relevance(paper["id"], score)
            count += 1
    finally:
        db.close()

    return count


# ---------------------------------------------------------------------------
# Feedback learning
# ---------------------------------------------------------------------------


def update_jel_weights(paper_id: int, is_interesting: bool) -> None:
    """EMA update of jel_weights.json based on explicit user feedback.

    If the user marks a paper as interesting, the JEL codes of that paper
    move toward 1.0 with alpha=0.1. If marked uninteresting, they move
    toward 0.0 with alpha=0.05. New codes are inserted at the current
    default weight before being updated.

    Args:
        paper_id: Primary key of the paper to learn from.
        is_interesting: True if the user found the paper relevant.
    """
    from econsignals.lib.db import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT jel_codes FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
    finally:
        db.close()

    if row is None:
        return

    jel_raw = row[0]
    if not jel_raw:
        return

    try:
        jel_codes: list[str] = json.loads(jel_raw) if isinstance(jel_raw, str) else jel_raw
    except (json.JSONDecodeError, TypeError):
        return

    if not jel_codes:
        return

    weights = load_jel_weights()
    target = 1.0 if is_interesting else 0.0
    alpha = 0.1 if is_interesting else 0.05
    default = weights.get("_default", 0.1)

    for code in jel_codes:
        code = code.strip()
        if not code:
            continue
        current = weights.get(code, default)
        weights[code] = round(current + alpha * (target - current), 6)

    # Persist
    try:
        _JEL_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _JEL_WEIGHTS_PATH.open("w", encoding="utf-8") as fh:
            json.dump(weights, fh, indent=2)
    except OSError:
        return

    # Invalidate cache so next load picks up the new weights
    global _jel_weights_cache
    _jel_weights_cache = None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    count = score_all_papers(days=30)
    print(json.dumps({"status": "ok", "scored": count}))
