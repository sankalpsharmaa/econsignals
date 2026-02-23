"""IMF working paper sensor for EconSignals.

Collects recent IMF working papers via the IMF eLibrary search API and,
when that is unavailable, via HTML scraping of the IMF Publications page.

Primary approach: IMF eLibrary REST API at
    https://www.elibrary.imf.org/search?q=&content=title&start=0&rows=25
    &sortField=relevance&sortOrder=desc

Fallback: HTML scraping of
    https://www.imf.org/en/Publications/WP

State is stored under the "imf" key in watches/papers/state.json, recording
the last successfully fetched date so old papers are not re-ingested on every
run.

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
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py convention)
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ELIBRARY_API = "https://www.elibrary.imf.org/search"
_WP_LISTING_URL = "https://www.imf.org/en/Publications/WP"
_WP_BASE_URL = "https://www.imf.org"

_ROWS_PER_PAGE = 25
_MAX_PAGES = 4
_DEFAULT_LOOKBACK_DAYS = 14

_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

# Regex helpers
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
_RE_JEL_LABEL = re.compile(
    r"\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)", re.IGNORECASE
)
_RE_JEL_CODE = re.compile(r"[A-Z][0-9]{1,2}")
_RE_WP_HREF = re.compile(r"/en/Publications/WP/Issues/\d{4}/\d{2}/\d{2}/[^\"'\s>]+")


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


def _strip_html(html: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace.

    Args:
        html: Raw HTML fragment.

    Returns:
        Plain-text string.

    Examples:
        >>> _strip_html("<p>Hello &amp; world</p>")
        'Hello & world'
    """
    if not html:
        return ""
    html = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        " ",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL classification codes from a text block.

    Args:
        text: Plain text or raw HTML.

    Returns:
        Deduplicated list of JEL code strings.
    """
    codes: list[str] = []
    seen: set[str] = set()
    for m in _RE_JEL_LABEL.finditer(text):
        for code in _RE_JEL_CODE.findall(m.group(1)):
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _url_hash(url: str) -> str:
    """Return an 8-character hex digest of a URL for use as a fallback ID.

    Args:
        url: URL string.

    Returns:
        8-character hex string.
    """
    return hashlib.md5(url.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# eLibrary API parsing
# ---------------------------------------------------------------------------


def _parse_elibrary_record(rec: dict) -> dict | None:
    """Convert a raw IMF eLibrary API record to the standard paper dict.

    Returns None if the record lacks a title.

    Args:
        rec: Raw dict from the eLibrary search API ``results`` list.

    Returns:
        Standard paper dict for BaseSensor.collect(), or None.
    """
    title = (
        rec.get("title") or rec.get("name") or rec.get("displayTitle") or ""
    ).strip()
    if not title:
        return None

    # source_id: prefer WP number, fall back to URL hash
    wp_number = (
        rec.get("seriesNumber")
        or rec.get("wpNumber")
        or rec.get("reportNumber")
        or ""
    ).strip()
    source_url_raw = (rec.get("url") or rec.get("link") or "").strip()
    if wp_number:
        source_id = f"imf-wp-{wp_number}"
    elif source_url_raw:
        source_id = _url_hash(source_url_raw)
    else:
        source_id = _url_hash(title)

    # Authors
    raw_authors = rec.get("authors") or rec.get("author") or []
    authors: list[str] = []
    if isinstance(raw_authors, list):
        for a in raw_authors:
            if isinstance(a, str):
                name = a.strip()
            elif isinstance(a, dict):
                name = (
                    a.get("name") or a.get("display_name") or a.get("fullName") or ""
                ).strip()
            else:
                name = ""
            if name:
                authors.append(name)
    elif isinstance(raw_authors, str) and raw_authors.strip():
        authors = [n.strip() for n in re.split(r"[;,]", raw_authors) if n.strip()]

    # Abstract
    abstract_raw = (
        rec.get("abstract") or rec.get("description") or rec.get("summary") or ""
    )
    abstract = _strip_html(abstract_raw).strip() or None

    # Date
    date_raw = (
        rec.get("pubDate")
        or rec.get("publishedDate")
        or rec.get("publicationDate")
        or rec.get("date")
        or ""
    )
    published_at = _parse_date(date_raw)

    # JEL codes
    jel_codes: list[str] = []
    jel_raw = rec.get("jel") or rec.get("jelCodes") or []
    if isinstance(jel_raw, list):
        for code in jel_raw:
            if isinstance(code, str):
                jel_codes.append(code.strip())
    elif isinstance(jel_raw, str) and jel_raw:
        jel_codes = [c.strip() for c in re.split(r"[,;\s]+", jel_raw) if c.strip()]
    if not jel_codes and abstract_raw:
        jel_codes = _extract_jel_codes(abstract_raw)

    # Keywords
    kw_raw = rec.get("keywords") or rec.get("topics") or []
    keywords: list[str] = []
    if isinstance(kw_raw, list):
        for k in kw_raw:
            if isinstance(k, str) and k.strip():
                keywords.append(k.strip())
            elif isinstance(k, dict):
                kw = (k.get("name") or k.get("label") or "").strip()
                if kw:
                    keywords.append(kw)
    elif isinstance(kw_raw, str) and kw_raw:
        keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]

    source_url = source_url_raw or None

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": rec.get("doi") or None,
        "url": source_url,
        "published_at": published_at,
        "paper_type": "working_paper",
        "jel_codes": jel_codes,
        "keywords": keywords,
        "source_id": source_id,
        "source_url": source_url,
        "raw_metadata": rec,
    }


# ---------------------------------------------------------------------------
# HTML scraping fallback helpers
# ---------------------------------------------------------------------------


def _parse_listing_html(html: str) -> list[dict]:
    """Extract paper stubs from the IMF WP listing page HTML.

    Looks for hrefs matching the IMF working paper URL pattern and extracts
    whatever title/author text surrounds the link.

    Args:
        html: Raw HTML of the WP listing page.

    Returns:
        List of minimal dicts with: url, title, authors_raw.
    """
    stubs: list[dict] = []
    seen_urls: set[str] = set()

    # Each result block: find <a href="/en/Publications/WP/Issues/...">
    # Pull the link text as title; look for a sibling author text nearby.
    blocks = re.split(r"(?=<a\s[^>]*href=[\"']/en/Publications/WP/Issues/)", html)

    for block in blocks:
        m_href = re.match(
            r'<a\s[^>]*href=["\'](/en/Publications/WP/Issues/[^"\'>\s]+)["\']',
            block,
        )
        if not m_href:
            continue

        relative_url = m_href.group(1)
        full_url = _WP_BASE_URL + relative_url

        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # Title: text immediately inside the anchor tag (up to ~300 chars)
        title_match = re.search(
            r'<a\s[^>]*href=["\'][^"\']+["\'][^>]*>(.*?)</a>', block, re.DOTALL
        )
        if title_match:
            title = _strip_html(title_match.group(1)).strip()
        else:
            title = ""

        # Authors: heuristically look in the next ~400 chars after the anchor
        tail = block[:400]
        author_match = re.search(
            r'(?:author|by)[:\s]*([A-Z][a-zA-Z\s,\.]+(?:and\s+[A-Z][a-zA-Z\s,\.]+)*)',
            tail,
            re.IGNORECASE,
        )
        authors_raw = author_match.group(1).strip() if author_match else ""

        # Date from URL: /Issues/YYYY/MM/DD/
        date_match = re.search(r"/Issues/(\d{4})/(\d{2})/(\d{2})/", relative_url)
        date_raw = (
            f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
            if date_match else ""
        )

        stubs.append({
            "url": full_url,
            "title": title,
            "authors_raw": authors_raw,
            "date_raw": date_raw,
        })

    return stubs


def _stub_to_paper(stub: dict) -> dict | None:
    """Convert a listing stub to a standard paper dict.

    Returns None if the stub has no title.

    Args:
        stub: Dict from _parse_listing_html.

    Returns:
        Standard paper dict, or None.
    """
    title = stub.get("title", "").strip()
    if not title:
        return None

    url = stub.get("url", "")
    source_id = _url_hash(url) if url else _url_hash(title)
    authors_raw = stub.get("authors_raw", "")
    authors: list[str] = []
    if authors_raw:
        authors = [
            p.strip()
            for p in re.split(r"[,;]|\band\b", authors_raw, flags=re.IGNORECASE)
            if p.strip() and 2 < len(p.strip()) < 100
        ]

    published_at = _parse_date(stub.get("date_raw", ""))

    return {
        "title": title,
        "authors": authors,
        "abstract": None,
        "doi": None,
        "url": url or None,
        "published_at": published_at,
        "paper_type": "working_paper",
        "jel_codes": [],
        "keywords": [],
        "source_id": source_id,
        "source_url": url or None,
        "raw_metadata": {"source": "imf_html_scrape", **stub},
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class IMFSensor(BaseSensor):
    """Fetch recent IMF working papers from the eLibrary API or HTML scraping.

    Primary approach uses the IMF eLibrary search API, which returns JSON
    results. Falls back to HTML scraping of the IMF Publications WP listing
    if the API is unavailable or returns no usable results.

    All parsing is defensive: any parse error results in that item being
    skipped and logged. A network-wide failure returns an empty list.

    Attributes:
        name: Sensor identifier used for DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 0.5 requests/second (2 s between requests).
    """

    name = "imf"
    watch = "papers"
    rate_limit = 0.5

    ELIBRARY_API = _ELIBRARY_API
    WP_LISTING_URL = _WP_LISTING_URL

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return the ISO date to use as a lower-bound publication filter.

        Reads imf.last_fetched from watches/papers/state.json. Falls back to
        _DEFAULT_LOOKBACK_DAYS days ago on first run.

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
    # eLibrary API approach
    # ------------------------------------------------------------------

    def _build_api_url(self, start: int) -> str:
        """Construct a paginated eLibrary search URL for IMF working papers.

        Args:
            start: Zero-based result offset.

        Returns:
            Fully-qualified URL string.
        """
        params: dict[str, str | int] = {
            "q": "",
            "content": "title",
            "start": start,
            "rows": _ROWS_PER_PAGE,
            "sortField": "score",
            "sortOrder": "desc",
            "fl_type": "Working Paper",
        }
        return f"{_ELIBRARY_API}?{urlencode(params)}"

    def _collect_via_api(self, from_date: str) -> list[dict] | None:
        """Fetch IMF working papers using the eLibrary search API.

        Paginates up to _MAX_PAGES pages. Stops when all papers on a page
        predate from_date or when fewer results than expected are returned.

        Args:
            from_date: Lower bound date string 'YYYY-MM-DD'.

        Returns:
            List of parsed paper dicts, or None if the API appears broken.
        """
        papers: list[dict] = []
        seen_ids: set[str] = set()

        for page in range(_MAX_PAGES):
            start = page * _ROWS_PER_PAGE
            url = self._build_api_url(start)

            try:
                data = self.fetch_json(url)
            except Exception as exc:
                print(f"[imf] API page {page + 1} failed: {exc}", file=sys.stderr)
                if page == 0:
                    return None
                break

            if not isinstance(data, dict):
                print(
                    f"[imf] API page {page + 1}: unexpected type {type(data).__name__}",
                    file=sys.stderr,
                )
                if page == 0:
                    return None
                break

            # eLibrary may nest results under various keys
            records = (
                data.get("response", {}).get("docs")
                or data.get("results")
                or data.get("docs")
                or data.get("items")
                or []
            )
            if not isinstance(records, list) or not records:
                if page == 0:
                    return None
                break

            page_papers: list[dict] = []
            all_old = True

            for rec in records:
                if not isinstance(rec, dict):
                    continue
                try:
                    parsed = _parse_elibrary_record(rec)
                except Exception as exc:
                    print(f"[imf] parse error: {exc}", file=sys.stderr)
                    continue

                if not parsed:
                    continue

                sid = parsed["source_id"]
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)

                pub = parsed.get("published_at") or ""
                if pub and pub < from_date:
                    continue

                all_old = False
                page_papers.append(parsed)

            papers.extend(page_papers)
            print(
                f"[imf] API page {page + 1}: {len(records)} records, "
                f"{len(page_papers)} within date window",
                file=sys.stderr,
            )

            if len(records) < _ROWS_PER_PAGE:
                break
            if all_old and page_papers == [] and records:
                break

        return papers

    # ------------------------------------------------------------------
    # HTML scraping fallback
    # ------------------------------------------------------------------

    def _collect_via_scrape(self, from_date: str) -> list[dict]:
        """Scrape the IMF WP HTML listing as a fallback.

        Args:
            from_date: Lower bound date string 'YYYY-MM-DD'.

        Returns:
            List of parsed paper dicts. Empty list on any error.
        """
        print(f"[imf] scraping listing: {_WP_LISTING_URL}", file=sys.stderr)
        try:
            raw = self.fetch_url(_WP_LISTING_URL)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[imf] failed to fetch listing page: {exc}", file=sys.stderr)
            return []

        stubs = _parse_listing_html(html)
        if not stubs:
            print("[imf] scraping: no paper links found on listing page", file=sys.stderr)
            return []

        papers: list[dict] = []
        for stub in stubs:
            # Date filter
            date_raw = stub.get("date_raw", "")
            if date_raw and date_raw < from_date:
                continue

            parsed = _stub_to_paper(stub)
            if parsed:
                papers.append(parsed)

        print(f"[imf] scraping: collected {len(papers)} papers", file=sys.stderr)
        return papers

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Fetch recent IMF working papers.

        Steps:
        1. Determine from_date via watch state (default: 14 days ago).
        2. Attempt the eLibrary API; parse and filter by date.
        3. If the API returns nothing usable, fall back to HTML scraping.
        4. Update watch state with today's date.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
            Returns empty list on total failure.
        """
        from_date = self._from_date()
        print(
            f"[imf] collecting working papers published on or after {from_date}",
            file=sys.stderr,
        )

        papers: list[dict] | None = None

        try:
            papers = self._collect_via_api(from_date)
        except Exception as exc:
            print(f"[imf] API collection raised unexpectedly: {exc}", file=sys.stderr)
            papers = None

        if papers is None:
            print("[imf] API unavailable, falling back to HTML scraping", file=sys.stderr)
            try:
                papers = self._collect_via_scrape(from_date)
            except Exception as exc:
                print(f"[imf] HTML scraping also failed: {exc}", file=sys.stderr)
                papers = []

        print(f"[imf] collected {len(papers)} papers", file=sys.stderr)
        self._update_state()
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = IMFSensor()
    sensor.run()
