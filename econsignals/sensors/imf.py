"""IMF working paper sensor for EconSignals.

Collects recent IMF working papers via Exa search, restricted to the imf.org
domain. Earlier eLibrary-API and HTML-scrape tiers were removed: the eLibrary
endpoint 403s behind CloudFront and the Publications listing is a JS SPA that
serves no paper links in its raw HTML, so neither tier ever produced a record.

Source: Exa search (https://api.exa.ai/search) filtered to include_domains
    ['imf.org'], category 'research paper'. Requires EXA_API_KEY.

State is stored under the "imf" key in watches/papers/state.json, recording the
last successful fetch date. The date is used only to bound the Exa query
loosely; client-side date dropping is intentionally avoided so that papers
newly discovered but bearing an older publication date are still ingested.
Deduplication (lib/dedup) suppresses already-seen papers.

Usage:
    python sensors/imf.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Package imports
# ---------------------------------------------------------------------------

from econsignals.sensors._base import BaseSensor, PROJ_ROOT
from econsignals.sensors._exa import exa_search

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_LOOKBACK_DAYS = 14

_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

# Regex helpers for date parsing
_RE_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_RE_MONTH_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[\s,]+(\d{4})\b",
    re.IGNORECASE,
)
_MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2,
    "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# A real IMF working paper exposes its WP number in one of three forms:
# a dated detail URL, a "wpiea<year><num>" PDF slug, or a "WP/NN/NNN" title tag.
_RE_DATED_WP_URL = re.compile(r"/issues/\d{4}/\d{2}/\d{2}/")
_RE_WPIEA_SLUG = re.compile(r"wpiea\d+")
_RE_WP_NUMBER = re.compile(r"\bwp/\d{1,2}/\d{1,4}\b")

# Trailing display cruft on some Exa titles, e.g. ", WP/25/231, November 2025".
_RE_TITLE_CRUFT = re.compile(
    r",\s*WP/\d{1,2}/\d{1,4}\s*,\s*[A-Za-z]+\s+\d{4}\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/papers/state.json, returning empty dict on failure.

    Returns:
        Full state dict. Empty dict if file is absent or corrupt.
    """
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist state dict to watches/papers/state.json.

    Args:
        state: Dict to serialise as JSON.
    """
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=_STATE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp_path, _STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> str | None:
    """Normalise a raw date string to ISO YYYY-MM-DD.

    Tries ISO prefix, then natural-language month+year.

    Args:
        raw: Raw date string, or None.

    Returns:
        'YYYY-MM-DD' string, or None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()

    m = _RE_ISO_DATE.search(raw)
    if m:
        return m.group(1)

    m2 = _RE_MONTH_YEAR.search(raw)
    if m2:
        month = _MONTH_MAP.get(m2.group(1).lower())
        year = int(m2.group(2))
        if month and 1900 <= year <= 2100:
            return f"{year:04d}-{month:02d}-01"

    return None


def _url_hash(url: str) -> str:
    """Return an 8-character hex digest of a URL for use as a fallback ID.

    Args:
        url: URL string.

    Returns:
        8-character hex string.
    """
    return hashlib.md5(url.encode()).hexdigest()[:8]


def _clean_title(title: str) -> str:
    """Strip trailing display cruft from an Exa-supplied title.

    Removes the ", WP/NN/NNN, Month YYYY" suffix that Exa sometimes appends.
    Called only after a record has passed the quality gate, so the stripped
    WP number is not needed as a signal at this point.

    Args:
        title: Raw title string.

    Returns:
        Cleaned title string.
    """
    return _RE_TITLE_CRUFT.sub("", title).strip()


def _accept_exa_paper(parsed: dict) -> bool:
    """Decide whether a parsed Exa record is a genuine IMF working paper.

    Quality gate (audit issues 4): reject the "IMF Working Papers" section-page
    heading and any too-short title, then require either a recognisable WP
    number (dated detail URL, wpiea PDF slug, or WP/NN/NNN title tag) or some
    real content (authors or abstract). Reads no date field, so a paper newly
    discovered but bearing an older publication date is still accepted.

    Args:
        parsed: Standard paper dict from _parse_exa_record.

    Returns:
        True if the record should be ingested, False otherwise.
    """
    title = (parsed.get("title") or "").strip()

    # Reject the IMF WP landing/section page and degenerate stub titles.
    if title.lower() == "imf working papers" or len(title) < 20:
        return False

    # Look for a WP number in the URL or the raw title (case-insensitive).
    url = (parsed.get("url") or "").lower()
    title_low = title.lower()
    has_wp_number = bool(
        _RE_DATED_WP_URL.search(url)
        or _RE_WPIEA_SLUG.search(url)
        or _RE_WP_NUMBER.search(title_low)
    )

    # Secondary floor: a real paper usually carries authors or an abstract.
    has_content = bool(parsed.get("authors") or parsed.get("abstract"))

    return has_wp_number or has_content


def _parse_exa_record(rec: dict) -> dict | None:
    """Convert an Exa result item to the standard paper dict.

    Args:
        rec: Raw result dict from Exa API.

    Returns:
        Standard paper dict, or None when required fields are missing.
    """
    title = (rec.get("title") or "").strip()
    if not title:
        return None

    url = (rec.get("url") or rec.get("id") or "").strip()
    exa_id = (rec.get("id") or "").strip()
    if exa_id:
        sid_suffix = _url_hash(exa_id)
    elif url:
        sid_suffix = _url_hash(url)
    else:
        sid_suffix = _url_hash(title)
    source_id = f"imf-exa-{sid_suffix}"

    raw_authors = rec.get("author") or rec.get("authors") or []
    authors: list[str] = []
    if isinstance(raw_authors, str) and raw_authors.strip():
        authors = [a.strip() for a in re.split(r"[,;]|\band\b", raw_authors) if a.strip()]
    elif isinstance(raw_authors, list):
        for a in raw_authors:
            if isinstance(a, str) and a.strip():
                authors.append(a.strip())
            elif isinstance(a, dict):
                name = (
                    a.get("name")
                    or a.get("author")
                    or a.get("display_name")
                    or ""
                ).strip()
                if name:
                    authors.append(name)

    text_raw = rec.get("text") or rec.get("summary") or rec.get("description") or ""
    abstract = text_raw.strip() if isinstance(text_raw, str) and text_raw.strip() else None

    date_raw = (
        rec.get("publishedDate")
        or rec.get("published_date")
        or rec.get("publishedAt")
        or rec.get("date")
        or ""
    )
    published_at = _parse_date(str(date_raw))

    doi = (rec.get("doi") or "").strip() or None
    if not doi and url:
        m_doi = re.search(r"doi\.org/(10\.\d{4,9}/\S+)", url, flags=re.IGNORECASE)
        if m_doi:
            doi = m_doi.group(1).rstrip(").,;")

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "url": url or None,
        "published_at": published_at,
        "paper_type": "working_paper",
        "jel_codes": [],
        "keywords": [],
        "source_id": source_id,
        "source_url": url or None,
        "raw_metadata": {"source": "imf_exa", **rec},
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class IMFSensor(BaseSensor):
    """Fetch recent IMF working papers via Exa search.

    Queries Exa for IMF working papers restricted to the imf.org domain, then
    applies a quality gate that rejects the "IMF Working Papers" section page
    and keeps records carrying a WP number or real authors/abstract. No
    client-side publication-date filter is applied; dedup suppresses repeats.

    All parsing is defensive: any parse error results in that item being
    skipped and logged. A missing EXA_API_KEY or network failure returns an
    empty list.

    Attributes:
        name: Sensor identifier used for DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 0.5 requests/second (2 s between requests).
    """

    name = "imf"
    watch = "papers"
    rate_limit = 0.5

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return the ISO date used to phrase the Exa query loosely.

        Reads imf.last_fetched from watches/papers/state.json. Falls back to
        _DEFAULT_LOOKBACK_DAYS days ago on first run. This date is only woven
        into the natural-language query text; it is never used as a hard
        client-side filter, so older-dated but newly-discovered papers survive.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = (state.get("imf") or {}).get("last_fetched")
        if last_fetched:
            try:
                datetime.fromisoformat(last_fetched)
                return last_fetched
            except ValueError:
                pass
        return (
            datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

    def _update_state(self) -> None:
        """Record today's date as the last successful IMF fetch in state."""
        state = _load_state()
        imf_state = state.get("imf") or {}
        imf_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state["imf"] = imf_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[imf] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Exa collection
    # ------------------------------------------------------------------

    def _collect_via_exa(self, from_date: str) -> list[dict]:
        """Fetch IMF working papers via Exa, restricted to imf.org.

        Args:
            from_date: Lower-bound date string 'YYYY-MM-DD', woven into the
                query text only (no hard client-side date filter).

        Returns:
            List of parsed paper dicts. Empty list on missing key/error/no data.
        """
        query = (
            "Recent IMF working papers published on or after "
            f"{from_date}"
        )
        print("[imf] querying Exa for IMF working papers", file=sys.stderr)

        # include_domains pins results to imf.org; no start_published_date is
        # passed, since Exa filters on the article's original publication date,
        # which would drop papers newly discovered but dated before from_date.
        raw_results = exa_search(
            query,
            num_results=25,
            max_characters=1200,
            include_domains=["imf.org"],
            log_prefix="[imf]",
        )

        if not raw_results:
            if not (os.environ.get("EXA_API_KEY") or "").strip():
                print("[imf] Exa skipped: EXA_API_KEY not set", file=sys.stderr)
            else:
                print("[imf] Exa returned no results", file=sys.stderr)
            return []

        papers: list[dict] = []
        seen_ids: set[str] = set()

        for rec in raw_results:
            parsed = _parse_exa_record(rec)
            if not parsed:
                continue

            # Quality gate: reject section pages and content-less stubs.
            if not _accept_exa_paper(parsed):
                continue

            # Strip trailing WP-number/date cruft from the display title.
            parsed["title"] = _clean_title(parsed["title"])

            sid = parsed["source_id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            papers.append(parsed)

        print(
            f"[imf] Exa: {len(raw_results)} raw results, {len(papers)} usable papers",
            file=sys.stderr,
        )
        return papers

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Fetch recent IMF working papers via Exa.

        Steps:
        1. Determine from_date via watch state (default: 14 days ago).
        2. Query Exa, restricted to imf.org, and apply the quality gate.
        3. Advance watch state only if papers were collected.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
            Returns empty list on total failure.
        """
        from_date = self._from_date()
        print(
            f"[imf] collecting working papers (from_date hint {from_date})",
            file=sys.stderr,
        )

        try:
            papers = self._collect_via_exa(from_date)
        except Exception as exc:
            print(f"[imf] Exa collection failed: {exc}", file=sys.stderr)
            papers = []

        print(f"[imf] collected {len(papers)} papers", file=sys.stderr)

        # Only advance state on a non-empty collection, so a failed or empty
        # run does not ratchet the lower bound past an un-captured window.
        if papers:
            self._update_state()
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = IMFSensor()
    sensor.run()
