"""BREAD (Bureau for Research and Economic Analysis of Development) working papers sensor.

Scrapes the BREAD working papers listing at ibread.org for recent development
economics papers. BREAD has no public API or stable RSS feed, so this sensor
parses the HTML listing page using html.parser and regex.

BREAD is a high-quality development economics network; papers here are
selective and relevant to the development/India research profile.

State is persisted under the "bread" key in watches/papers/state.json,
tracking the last successfully fetched run so old papers are not re-ingested.

Usage:
    python sensors/bread.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

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

_BASE_URL = "https://ibread.org"
_LISTING_URLS: list[str] = [
    "https://ibread.org/bread/working-papers",
    "https://ibread.org/bread/papers",
]

_DEFAULT_LOOKBACK_DAYS = 60  # BREAD publishes infrequently
_MAX_DETAIL_FETCHES = 30     # Cap individual paper page fetches per run

_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

_BASE_KEYWORDS: list[str] = ["development economics", "BREAD"]

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

_RE_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_RE_MONTH_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[\s,]+(\d{4})\b",
    re.IGNORECASE,
)
_RE_YEAR_ONLY = re.compile(r"\b(20\d{2}|19\d{2})\b")
_RE_JEL_LABEL = re.compile(
    r"\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)",
    re.IGNORECASE,
)
_RE_JEL_CODE = re.compile(r"[A-Z][0-9]{1,2}")
_RE_DOI = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s\"'<>]+)",
    re.IGNORECASE,
)

_MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _strip_html(fragment: str) -> str:
    """Remove HTML tags and collapse whitespace, returning plain text.

    Args:
        fragment: Raw HTML string or plain text.

    Returns:
        Plain-text string with normalised whitespace.
    """
    if not fragment:
        return ""
    fragment = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        " ",
        fragment,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str | None) -> str | None:
    """Convert a raw date string to ISO YYYY-MM-DD, or return None.

    Tries ISO, natural language month+year, and bare four-digit year.

    Args:
        raw: Raw date string, or None.

    Returns:
        'YYYY-MM-DD' string, or None if unparseable.

    Examples:
        >>> _parse_date("March 2024")
        '2024-03-01'
        >>> _parse_date("2024-03-15")
        '2024-03-15'
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

    m3 = _RE_YEAR_ONLY.search(raw)
    if m3:
        year = int(m3.group(1))
        if 1990 <= year <= 2100:
            return f"{year}-01-01"

    return None


def _url_hash(url: str) -> str:
    """Return an 8-character hex digest of the URL for use in source_id.

    Args:
        url: The canonical URL string.

    Returns:
        8-character lowercase hex string.
    """
    return hashlib.sha1(url.encode()).hexdigest()[:8]


def _parse_authors(raw: str) -> list[str]:
    """Split a comma/semicolon/ampersand-delimited author string into a list.

    Args:
        raw: Raw author text.

    Returns:
        List of individual author name strings.
    """
    if not raw:
        return []
    normalised = re.sub(r"\band\b", ",", raw, flags=re.IGNORECASE)
    normalised = normalised.replace(";", ",").replace("&", ",")
    parts = [p.strip() for p in normalised.split(",") if p.strip()]
    return [p for p in parts if 2 < len(p) < 100]


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL classification codes from a text block.

    Args:
        text: Plain text to search.

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


# ---------------------------------------------------------------------------
# HTML parser for the BREAD listing page
# ---------------------------------------------------------------------------


class _BreadListingParser(HTMLParser):
    """Extract paper entries from a BREAD working papers listing page.

    BREAD renders papers as a list of entries, each typically containing:
      - An <a href="/bread/working-papers/NNN"> link with the title
      - Author names in a nearby <p> or <span>
      - An optional date or paper number

    This parser is permissive and never raises; failures produce empty items.

    Attributes:
        items: List of partial paper dicts extracted from the page.
            Each has: url, title, authors_raw, date_raw, paper_number.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.items: list[dict] = []

        # Mutable traversal state
        self._in_entry: bool = False
        self._entry_depth: int = 0
        self._current: dict | None = None
        self._p_texts: list[str] = []
        self._capture: bool = False
        self._buf: str = ""
        self._global_depth: int = 0

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _is_paper_href(self, href: str) -> bool:
        """Return True if href looks like a BREAD paper page.

        Args:
            href: Raw href attribute value.

        Returns:
            True when the href points to a BREAD working paper.
        """
        return bool(
            re.search(
                r"/(?:bread/)?(?:working[-_]?papers?|papers?)/[\w\-]+",
                href,
                re.IGNORECASE,
            )
        ) and not href.endswith((".pdf", ".zip"))

    # -----------------------------------------------------------------------
    # HTMLParser callbacks
    # -----------------------------------------------------------------------

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._global_depth += 1
        attr_map = dict(attrs)
        tag = tag.lower()

        if tag == "a":
            href = attr_map.get("href") or ""
            if self._is_paper_href(href):
                full_url = urljoin(self.base_url, href)
                self._in_entry = True
                self._current = {
                    "url": full_url,
                    "title": "",
                    "authors_raw": "",
                    "date_raw": "",
                    "paper_number": "",
                }
                self._entry_depth = self._global_depth
                self._p_texts = []
                self._capture = True
                self._buf = ""
                return

        if self._in_entry:
            if tag in ("p", "span", "div", "li"):
                self._capture = True
                self._buf = ""

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if self._capture and tag in ("p", "span", "div", "li", "a"):
            text = _strip_html(self._buf).strip()
            if text:
                if self._current and not self._current["title"] and tag == "a":
                    self._current["title"] = text
                elif text:
                    self._p_texts.append(text)
            self._capture = False
            self._buf = ""

        if self._in_entry and tag == "a" and self._global_depth <= self._entry_depth:
            if self._current and self._current.get("title"):
                # Heuristic assignment of collected text fragments
                if len(self._p_texts) >= 1:
                    self._current["authors_raw"] = self._p_texts[0]
                if len(self._p_texts) >= 2:
                    self._current["date_raw"] = self._p_texts[1]
                self.items.append(self._current)
            self._in_entry = False
            self._current = None
            self._p_texts = []

        self._global_depth = max(0, self._global_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf += data

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        if self._capture:
            self._buf += f"&{name};"

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        if self._capture:
            self._buf += f"&#{name};"


# ---------------------------------------------------------------------------
# Individual paper page extraction
# ---------------------------------------------------------------------------


def _extract_paper_detail(html: str) -> dict:
    """Extract metadata from an individual BREAD paper page.

    Tries several defensive patterns for abstract, authors, date,
    DOI, and JEL codes. Returns an empty dict on failure.

    Args:
        html: Raw decoded HTML of the paper page.

    Returns:
        Partial dict with any of: title, abstract, authors (list[str]),
        published_at (str), doi (str), jel_codes (list[str]),
        paper_number (str).
    """
    result: dict = {}

    # --- Title ---
    for pattern in (
        r"<h1[^>]*>(.*?)</h1>",
        r"<title>(.*?)</title>",
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            title = _strip_html(m.group(1)).strip()
            # Filter out generic site titles
            if len(title) > 10 and "BREAD" not in title.upper().split():
                result["title"] = title
                break

    # --- Abstract ---
    for pattern in (
        r'class="[^"]*(?:abstract|summary|description)[^"]*"[^>]*>(.*?)</(?:div|section|p)>',
        r'<meta\s+(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\'](.*?)["\']',
        r'(?:Abstract|Summary)[:\s]*</[^>]+>\s*<p[^>]*>(.*?)</p>',
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            candidate = _strip_html(m.group(1)).strip()
            if len(candidate) > 80:
                result["abstract"] = candidate[:4000]
                break

    # --- Authors ---
    authors: list[str] = []
    for pattern in (
        r'class="[^"]*(?:author|authors|byline)[^"]*"[^>]*>(.*?)</(?:span|div|p|ul)>',
        r'itemprop="author"[^>]*>(.*?)</(?:span|div|a|p)>',
        r'<meta\s+name=["\']author["\']\s+content=["\'](.*?)["\']',
    ):
        for m in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
            name = _strip_html(m.group(1)).strip()
            if name and len(name) < 120 and name not in authors:
                authors.append(name)
        if authors:
            break
    # If we got a single blob, split it
    if len(authors) == 1:
        authors = _parse_authors(authors[0]) or authors
    if authors:
        result["authors"] = authors

    # --- Date ---
    for pattern in (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<meta\s+(?:name|property)=["\'](?:pubdate|article:published_time)["\'][^>]*content=["\'](.*?)["\']',
        r'(?:Published|Date|Year)[:\s]*<[^>]*>([^<]{4,30})<',
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            dt = _parse_date(m.group(1).strip())
            if dt:
                result["published_at"] = dt
                break

    # --- DOI ---
    m_doi = _RE_DOI.search(html)
    if m_doi:
        result["doi"] = m_doi.group(1).rstrip(".,;)")

    # --- JEL codes ---
    jel = _extract_jel_codes(_strip_html(html))
    if jel:
        result["jel_codes"] = jel

    # --- BREAD paper number (e.g. "#NNN" or "No. NNN") ---
    num_m = re.search(
        r"(?:BREAD\s+(?:Working\s+)?Paper\s*(?:#|No\.?\s*))(\d{3,4})",
        html,
        re.IGNORECASE,
    )
    if num_m:
        result["paper_number"] = num_m.group(1)

    return result


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load the papers watch state JSON, returning empty dict on failure.

    Returns:
        Full state dict, or empty dict if the file is absent or corrupt.
    """
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist the state dict to watches/papers/state.json.

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
# Sensor
# ---------------------------------------------------------------------------


class BreadSensor(BaseSensor):
    """Collect BREAD working papers from ibread.org.

    BREAD (Bureau for Research and Economic Analysis of Development) is a
    selective network producing high-quality development economics research.
    This sensor parses the public HTML listing and optionally fetches
    individual paper pages for abstracts and metadata.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 2 requests per second (polite for a small academic site).
    """

    name = "bread"
    watch = "papers"
    rate_limit = 0.5  # 2-second interval between requests

    BASE_URL = _BASE_URL
    LISTING_URLS = _LISTING_URLS

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return the ISO date lower-bound for filtering papers by date.

        Reads bread.last_fetched from watches/papers/state.json. Falls back
        to _DEFAULT_LOOKBACK_DAYS days ago on the first run.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = (state.get("bread") or {}).get("last_fetched")
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
        """Record today as the last successful BREAD fetch in state."""
        state = _load_state()
        bread_state = state.get("bread") or {}
        bread_state["last_fetched"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d"
        )
        state["bread"] = bread_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[bread] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Listing page fetch
    # ------------------------------------------------------------------

    def _fetch_listing(self, url: str) -> list[dict]:
        """Fetch one BREAD listing URL and parse paper entries from the HTML.

        Tries the URL and falls back gracefully on any network or parse error.

        Args:
            url: Full URL to the listing page.

        Returns:
            List of raw entry dicts from _BreadListingParser.
        """
        try:
            raw = self.fetch_url(url, timeout=30)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[bread] failed to fetch {url}: {exc}", file=sys.stderr)
            return []

        parser = _BreadListingParser(base_url=_BASE_URL)
        try:
            parser.feed(html)
        except Exception as exc:
            print(f"[bread] HTML parse error on {url}: {exc}", file=sys.stderr)

        return parser.items

    # ------------------------------------------------------------------
    # Generic fallback listing parse
    # ------------------------------------------------------------------

    def _generic_listing_parse(self, html: str) -> list[dict]:
        """Fallback parser: scan for any link that looks like a paper page.

        Used when _BreadListingParser finds nothing, which can happen if the
        site structure changes.

        Args:
            html: Decoded HTML of the listing page.

        Returns:
            List of minimal entry dicts (url, title, authors_raw, date_raw).
        """
        items: list[dict] = []
        seen: set[str] = set()

        for m in re.finditer(
            r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            href = m.group(1).strip()
            if not re.search(r"/(?:working[-_]?papers?|papers?)/", href, re.IGNORECASE):
                continue
            if href.endswith((".pdf", ".zip")):
                continue
            full_url = urljoin(_BASE_URL, href)
            if full_url in seen:
                continue
            seen.add(full_url)

            title = _strip_html(m.group(2)).strip()
            if len(title) < 10:
                continue

            # Grab 300 chars of surrounding text for date/author hints
            start = max(0, m.start() - 150)
            end = min(len(html), m.end() + 150)
            context = _strip_html(html[start:end])

            items.append({
                "url": full_url,
                "title": title,
                "authors_raw": "",
                "date_raw": context,
                "paper_number": "",
            })

        return items

    # ------------------------------------------------------------------
    # Individual paper detail
    # ------------------------------------------------------------------

    def _fetch_paper_detail(self, url: str) -> dict:
        """Fetch and parse an individual BREAD paper page.

        Returns an empty dict on any network or parse error.

        Args:
            url: Full URL of the paper page.

        Returns:
            Partial metadata dict from _extract_paper_detail().
        """
        try:
            raw = self.fetch_url(url, timeout=30)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[bread] failed to fetch paper {url}: {exc}", file=sys.stderr)
            return {}

        try:
            return _extract_paper_detail(html)
        except Exception as exc:
            print(
                f"[bread] parse error on paper {url}: {exc}", file=sys.stderr
            )
            return {}

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Scrape BREAD working papers and return standard paper dicts.

        Steps:
        1. Determine from_date via watch state (default: 60 days ago).
        2. Try each listing URL in LISTING_URLS until entries are found.
        3. Fall back to the generic listing parser if the HTML parser yields nothing.
        4. For each entry, fetch the individual paper page to fill in missing
           metadata (abstract, authors, date, DOI), up to _MAX_DETAIL_FETCHES.
        5. Build the standard paper dict; skip items without a title.
        6. Update watch state on success.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        from_date = self._from_date()
        print(
            f"[bread] collecting papers on or after {from_date}", file=sys.stderr
        )

        # --- Find listing entries ---
        raw_entries: list[dict] = []
        for listing_url in self.LISTING_URLS:
            print(f"[bread] fetching listing: {listing_url}", file=sys.stderr)
            raw_entries = self._fetch_listing(listing_url)
            if raw_entries:
                break

        # Generic fallback if dedicated parser found nothing
        if not raw_entries:
            for listing_url in self.LISTING_URLS:
                try:
                    raw = self.fetch_url(listing_url, timeout=30)
                    html = raw.decode("utf-8", errors="replace")
                    raw_entries = self._generic_listing_parse(html)
                    if raw_entries:
                        break
                except Exception as exc:
                    print(
                        f"[bread] generic parse fallback failed for {listing_url}: {exc}",
                        file=sys.stderr,
                    )

        print(f"[bread] listing entries found: {len(raw_entries)}", file=sys.stderr)

        papers: list[dict] = []
        seen_urls: set[str] = set()
        detail_budget = _MAX_DETAIL_FETCHES

        for entry in raw_entries:
            url = entry.get("url", "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title = entry.get("title", "").strip()
            authors_raw = entry.get("authors_raw", "").strip()
            date_raw = entry.get("date_raw", "").strip()

            # --- Fetch detail page ---
            detail: dict = {}
            if detail_budget > 0:
                detail = self._fetch_paper_detail(url)
                detail_budget -= 1

            # Merge listing data with detail data
            if not title and detail.get("title"):
                title = detail["title"]
            if not title:
                continue  # Cannot ingest without a title

            authors = _parse_authors(authors_raw)
            if not authors and detail.get("authors"):
                authors = detail["authors"]

            published_at = _parse_date(date_raw)
            if not published_at and detail.get("published_at"):
                published_at = detail["published_at"]

            # Date filter: skip papers older than from_date when date is known
            if published_at and published_at < from_date:
                print(
                    f"[bread] skipping old paper ({published_at}): {title[:60]}",
                    file=sys.stderr,
                )
                continue

            doi: str | None = detail.get("doi")
            jel_codes: list[str] = detail.get("jel_codes", [])
            if not jel_codes:
                jel_codes = _extract_jel_codes(title)

            # Build source_id from paper number if available, else URL hash
            paper_number = entry.get("paper_number") or detail.get("paper_number")
            if paper_number:
                source_id = f"bread:{paper_number}"
            else:
                source_id = f"bread:{_url_hash(url)}"

            keywords = list(_BASE_KEYWORDS)

            papers.append({
                "title": title,
                "authors": authors,
                "abstract": detail.get("abstract"),
                "doi": doi,
                "url": url,
                "published_at": published_at,
                "paper_type": "working_paper",
                "jel_codes": jel_codes,
                "keywords": keywords,
                "source_id": source_id,
                "source_url": url,
                "raw_metadata": {
                    "source": "bread_html_scrape",
                    "listing_url": url,
                    "paper_number": paper_number,
                    "authors_raw": authors_raw,
                    "date_raw": date_raw,
                },
            })

        self._update_state()
        print(f"[bread] collected {len(papers)} papers", file=sys.stderr)
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = BreadSensor()
    sensor.run()
