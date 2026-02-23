"""IZA Discussion Papers sensor for EconSignals.

Scrapes iza.org/publications/dp for working papers covering labour economics,
education, health, and development.

The IZA site renders a paginated HTML listing of discussion papers.  This
sensor parses those pages using only the standard library (html.parser and
regex), then optionally fetches individual paper abstract pages for fuller
metadata when listing-level data is thin.

Pagination uses: https://www.iza.org/publications/dp?page=N

State is stored under the "iza" key in watches/papers/state.json, tracking
the last successfully fetched date to avoid re-ingesting old papers on every
run.

Usage:
    python sensors/iza.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
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

_BASE_URL = "https://www.iza.org"
_LISTING_URL = "https://www.iza.org/publications/dp"

_MAX_PAGES = 10
_DEFAULT_LOOKBACK_DAYS = 30
_CARDS_PER_PAGE_MIN = 5  # if fewer returned, assume last page

_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# IZA DP number in path, e.g. /publications/dp/12345 or /dp/12345
_RE_DP_HREF = re.compile(r'/(?:publications/)?dp/(\d+)(?:/|$)')

# ISO date (YYYY-MM-DD)
_RE_ISO_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})')

# Natural language month+year, e.g. "January 2024"
_RE_MONTH_YEAR = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|'
    r'October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    r'[\s,]+(\d{4})\b',
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

# DOI pattern
_RE_DOI = re.compile(r'\b(10\.\d{4,}/\S+)', re.IGNORECASE)

# JEL code label + codes
_RE_JEL_LABEL = re.compile(
    r'\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)',
    re.IGNORECASE,
)
_RE_JEL_CODE = re.compile(r'[A-Z][0-9]{1,2}')


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> str | None:
    """Convert a raw date string to ISO YYYY-MM-DD, or return None.

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


# ---------------------------------------------------------------------------
# HTML strip / text extraction
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace, unescaping HTML entities.

    Args:
        html: Raw HTML fragment.

    Returns:
        Plain-text string with normalised whitespace.
    """
    if not html:
        return ""
    html = re.sub(
        r'<(script|style)[^>]*>.*?</(script|style)>',
        ' ',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r'<[^>]+>', ' ', html)
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


# ---------------------------------------------------------------------------
# HTML parser for the IZA publications listing page
# ---------------------------------------------------------------------------


class _IZAListingParser(HTMLParser):
    """Extract DP numbers and surface metadata from an IZA listing page.

    IZA renders discussion papers as list items or article cards.  Each entry
    typically contains a link whose href includes /dp/<number>, an <h4> or <h3>
    for the title, and <span> or <p> elements for authors and date.

    This parser is intentionally permissive and never raises.  It tracks state
    via a simple depth counter and a "currently inside a paper card" flag.

    Attributes:
        items: List of dicts, one per discovered paper link.
            Each dict has: dp_number (str), title (str), authors_raw (str),
            date_raw (str), abstract_url (str).
    """

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []

        self._in_card: bool = False
        self._current: dict | None = None
        self._depth: int = 0          # depth inside current card anchor/container

        # Title capture
        self._capture_title: bool = False
        self._title_tag: str = ""
        self._title_depth: int = 0
        self._buf: str = ""

        # Inline text fragments collected within the card (authors, date, etc.)
        self._text_fragments: list[str] = []
        self._capture_text: bool = False
        self._text_tag: str = ""

    # ------------------------------------------------------------------
    # HTMLParser callbacks
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = dict(attrs)

        href = attr_map.get("href") or ""
        m = _RE_DP_HREF.search(href)

        if m and not self._in_card:
            # Entering a new paper card via an anchor that contains a DP number
            self._in_card = True
            self._current = {
                "dp_number": m.group(1),
                "title": "",
                "authors_raw": "",
                "date_raw": "",
                "abstract_url": urljoin(_BASE_URL, href.split("?")[0]),
            }
            self._depth = 0
            self._text_fragments = []
            return

        if self._in_card:
            self._depth += 1

            # Capture the title from the first h3/h4/h2 inside the card
            if tag_l in ("h2", "h3", "h4") and not self._capture_title and not self._current.get("title"):
                self._capture_title = True
                self._title_tag = tag_l
                self._title_depth = self._depth
                self._buf = ""

            # Capture span/p text for authors and dates
            elif tag_l in ("span", "p", "div") and not self._capture_text:
                cls = attr_map.get("class") or ""
                # Only capture "small" containers (not huge divs likely to be
                # layout wrappers).  We accept anything whose class hints at
                # author/date/meta content.
                if any(kw in cls.lower() for kw in ("author", "date", "meta", "pub", "info")):
                    self._capture_text = True
                    self._text_tag = tag_l
                    self._buf = ""

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()

        if self._capture_title and tag_l == self._title_tag:
            if self._current is not None:
                self._current["title"] = _strip_html(self._buf).strip()
            self._capture_title = False
            self._buf = ""

        elif self._capture_text and tag_l == self._text_tag:
            text = _strip_html(self._buf).strip()
            if text:
                self._text_fragments.append(text)
            self._capture_text = False
            self._buf = ""

        elif self._in_card and tag_l == "a":
            # Closing the card anchor -- finalise the current item
            if self._current is not None and self._current.get("dp_number"):
                if not self._current.get("title"):
                    # Fall back: use first long text fragment as title
                    for frag in self._text_fragments:
                        if len(frag) > 20:
                            self._current["title"] = frag
                            break

                # Heuristic: first fragment that looks like names = authors;
                # first fragment that looks like a date = date.
                _RE_LIKELY_DATE = re.compile(
                    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})\b',
                    re.IGNORECASE,
                )
                for frag in self._text_fragments:
                    if not self._current["authors_raw"] and not _RE_LIKELY_DATE.search(frag) and len(frag) > 3:
                        self._current["authors_raw"] = frag
                    elif not self._current["date_raw"] and _RE_LIKELY_DATE.search(frag):
                        self._current["date_raw"] = frag

                if self._current.get("dp_number"):
                    self.items.append(self._current)

            self._in_card = False
            self._current = None
            self._capture_title = False
            self._capture_text = False
            self._text_fragments = []
            self._depth = 0
            self._buf = ""
            return

        if self._in_card:
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture_title or self._capture_text:
            self._buf += data

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        if self._capture_title or self._capture_text:
            self._buf += f"&{name};"

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        if self._capture_title or self._capture_text:
            self._buf += f"&#{name};"


# ---------------------------------------------------------------------------
# Individual paper page parsing
# ---------------------------------------------------------------------------


def _extract_from_paper_page(html: str) -> dict:
    """Extract abstract, authors, date, DOI, and JEL codes from an IZA paper page.

    Uses a sequence of defensive regex patterns against the raw HTML.  Returns
    an empty dict on failure so the caller continues with listing-level data.

    Args:
        html: Raw HTML of the individual IZA DP abstract page.

    Returns:
        Partial dict with any of: title (str), abstract (str),
        authors (list[dict with "name"]), published_at (str), doi (str),
        jel_codes (list[str]), keywords (list[str]).
    """
    result: dict = {}

    # --- Title ---
    for pat in (
        r'<h1[^>]*class="[^"]*(?:title|paper-title)[^"]*"[^>]*>(.*?)</h1>',
        r'"headline"\s*:\s*"([^"]{10,300})"',
        r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            t = _strip_html(m.group(1)).strip()
            if len(t) > 5:
                result["title"] = t
                break

    # --- Abstract ---
    for pat in (
        r'class="[^"]*abstract[^"]*"[^>]*>(.*?)</(?:div|section|p|article)>',
        r'<meta\s+(?:name|property)=["\'](?:description|og:description)["\']\s+content=["\'](.*?)["\']',
        r'"description"\s*:\s*"([^"]{80,})"',
    ):
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            candidate = _strip_html(m.group(1)).strip()
            if len(candidate) > 60:
                result["abstract"] = candidate[:4000]
                break

    # --- Authors ---
    authors: list[dict] = []
    seen_names: set[str] = set()

    for pat in (
        r'itemprop="author"[^>]*>.*?itemprop="name"[^>]*>(.*?)</',
        r'class="[^"]*author[^"]*"[^>]*>(.*?)</(?:span|div|a|p|li)>',
        r'"author"\s*:\s*\[([^\]]+)\]',
        r'<meta\s+name=["\']author["\']\s+content=["\'](.*?)["\']',
    ):
        for m in re.finditer(pat, html, re.IGNORECASE | re.DOTALL):
            raw_name = _strip_html(m.group(1)).strip()
            # Split comma-separated names if the pattern returned a list
            sub_names = [n.strip() for n in re.split(r',\s*(?=[A-Z])', raw_name) if n.strip()]
            for name in sub_names:
                if name and len(name) < 120 and name not in seen_names:
                    seen_names.add(name)
                    authors.append({"name": name})
        if authors:
            break
    if authors:
        result["authors"] = authors

    # --- Date ---
    for pat in (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<meta\s+(?:name|property)=["\'](?:pubdate|article:published_time)["\']\s+content=["\'](.*?)["\']',
        r'(?:Published|Date|Month)[:\s]*(?:<[^>]*>)+\s*([^<]{4,40})<',
        r'\bIZA\s+DP\s+No\.\s*\d+\s*[,\|]?\s*([A-Z][a-z]+\s+\d{4})',
    ):
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            dt = _parse_date(m.group(1).strip())
            if dt:
                result["published_at"] = dt
                break

    # --- DOI ---
    for pat in (
        r'"doi"\s*:\s*"([^"]+)"',
        r'doi\.org/(10\.\d{4,}/[^\s"<>]+)',
        r'DOI[:\s]+(10\.\d{4,}/[^\s"<>]+)',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            doi_candidate = m.group(1).strip().rstrip(".,")
            result["doi"] = doi_candidate
            break

    # --- JEL codes ---
    jel_codes: list[str] = []
    seen_jel: set[str] = set()
    for m in _RE_JEL_LABEL.finditer(html):
        for code in _RE_JEL_CODE.findall(m.group(1)):
            if code not in seen_jel:
                seen_jel.add(code)
                jel_codes.append(code)
    if jel_codes:
        result["jel_codes"] = jel_codes

    # --- Keywords ---
    keywords: list[str] = []
    for pat in (
        r'class="[^"]*(?:keyword|tag|topic)[^"]*"[^>]*>(.*?)</(?:a|span|li|div)>',
        r'"keywords"\s*:\s*"([^"]+)"',
    ):
        for m in re.finditer(pat, html, re.IGNORECASE | re.DOTALL):
            kw = _strip_html(m.group(1)).strip()
            # keywords field may be comma-separated
            for part in re.split(r'[,;]', kw):
                part = part.strip()
                if part and len(part) < 80 and part not in keywords:
                    keywords.append(part)
    if keywords:
        result["keywords"] = keywords

    return result


# ---------------------------------------------------------------------------
# Author string parsing
# ---------------------------------------------------------------------------


def _parse_authors(raw: str) -> list[dict]:
    """Split a delimited author string into a list of author dicts.

    Args:
        raw: Raw author text, e.g. "Daron Acemoglu, David Autor and Pascual Restrepo".

    Returns:
        List of dicts with a single "name" key per author.
    """
    if not raw:
        return []
    normalised = re.sub(r'\band\b', ',', raw, flags=re.IGNORECASE)
    normalised = normalised.replace(';', ',').replace('&', ',')
    parts = [p.strip() for p in normalised.split(',') if p.strip()]
    return [{"name": p} for p in parts if 2 < len(p) < 100]


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/papers/state.json, returning empty dict on failure."""
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist state dict to watches/papers/state.json."""
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


class IZASensor(BaseSensor):
    """Scrape IZA Discussion Papers (iza.org/publications/dp).

    The IZA website has no stable public API for discussion papers.  This
    sensor fetches HTML listing pages, parses paper cards with html.parser and
    regex, and optionally retrieves individual abstract pages to fill in missing
    metadata (abstract, authors, DOI, JEL codes).

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category (papers).
        rate_limit: 2.0 requests/second.
    """

    name = "iza"
    watch = "papers"
    rate_limit = 2.0

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return the ISO date lower-bound for publication filtering.

        Reads iza.last_fetched from watches/papers/state.json.  Falls back to
        _DEFAULT_LOOKBACK_DAYS days ago on first run.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = (state.get("iza") or {}).get("last_fetched")
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
        """Record today's date as the last successful IZA fetch in state."""
        state = _load_state()
        iza_state = state.get("iza") or {}
        iza_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state["iza"] = iza_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[iza] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Listing page fetch and parse
    # ------------------------------------------------------------------

    def _listing_url(self, page: int) -> str:
        """Build a paginated IZA DP listing URL.

        Args:
            page: Zero-based page number.

        Returns:
            Fully-qualified URL string.
        """
        if page == 0:
            return _LISTING_URL
        return f"{_LISTING_URL}?page={page}"

    def _fetch_listing_page(self, url: str) -> list[dict]:
        """Fetch a single listing page and parse paper cards from the HTML.

        Falls back to a regex sweep for DP hrefs when the HTMLParser yields
        no items, so that minor site layout changes do not cause silent data
        loss.

        Args:
            url: Full listing URL to fetch.

        Returns:
            List of raw card dicts, possibly empty on error.
        """
        try:
            raw = self.fetch_url(url)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[iza] failed to fetch listing {url}: {exc}", file=sys.stderr)
            return []

        parser = _IZAListingParser()
        try:
            parser.feed(html)
        except Exception as exc:
            print(f"[iza] HTML parse error on {url}: {exc}", file=sys.stderr)

        if parser.items:
            return parser.items

        # Fallback: regex sweep for all DP hrefs on the page
        fallback_items: list[dict] = []
        seen_dp: set[str] = set()
        for m in _RE_DP_HREF.finditer(html):
            dp_num = m.group(1)
            if dp_num not in seen_dp:
                seen_dp.add(dp_num)
                abstract_url = f"{_BASE_URL}/publications/dp/{dp_num}"
                fallback_items.append({
                    "dp_number": dp_num,
                    "title": "",
                    "authors_raw": "",
                    "date_raw": "",
                    "abstract_url": abstract_url,
                })
        return fallback_items

    # ------------------------------------------------------------------
    # Paper-page detail fetch
    # ------------------------------------------------------------------

    def _fetch_paper_detail(self, dp_number: str, abstract_url: str) -> dict:
        """Fetch and parse an individual IZA abstract page for extra metadata.

        Args:
            dp_number: IZA DP number string, e.g. '12345'.
            abstract_url: URL of the abstract page.

        Returns:
            Partial paper dict with any of: title, abstract, authors,
            published_at, doi, jel_codes, keywords.
        """
        try:
            raw = self.fetch_url(abstract_url)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(
                f"[iza] failed to fetch paper page dp/{dp_number}: {exc}",
                file=sys.stderr,
            )
            return {}

        try:
            return _extract_from_paper_page(html)
        except Exception as exc:
            print(
                f"[iza] parse error on paper page dp/{dp_number}: {exc}",
                file=sys.stderr,
            )
            return {}

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Scrape IZA Discussion Papers and return standard paper dicts.

        Steps:
        1. Determine from_date via watch state (default: 30 days ago).
        2. Paginate the IZA DP listing up to _MAX_PAGES pages.
        3. For cards missing title/abstract/authors, fetch the individual
           abstract page (subject to a per-run request budget).
        4. Build the standard paper dict for each item and return.
        5. Update watch state on success.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
            Each dict has: title, authors (list[dict with "name"]), abstract,
            url, doi, source, source_id (IZA DP number), published_at,
            paper_type="working_paper".
        """
        from_date = self._from_date()
        print(
            f"[iza] collecting discussion papers on or after {from_date}",
            file=sys.stderr,
        )

        papers: list[dict] = []
        seen_dp: set[str] = set()

        # Budget for fetching individual abstract pages
        detail_budget = 30

        for page in range(_MAX_PAGES):
            url = self._listing_url(page)
            cards = self._fetch_listing_page(url)

            print(
                f"[iza] page={page}: {len(cards)} cards",
                file=sys.stderr,
            )

            if not cards:
                break

            stop_early = False
            page_new = 0

            for card in cards:
                dp_number = card.get("dp_number", "").strip()
                if not dp_number or dp_number in seen_dp:
                    continue

                abstract_url: str = card.get(
                    "abstract_url",
                    f"{_BASE_URL}/publications/dp/{dp_number}",
                )
                title: str = card.get("title", "").strip()
                authors_raw: str = card.get("authors_raw", "")
                date_raw: str = card.get("date_raw", "")

                authors = _parse_authors(authors_raw)
                published_at = _parse_date(date_raw)

                # If listing gave us a date that predates our window, the
                # listing is sorted newest-first so stop scanning this page.
                if published_at and published_at < from_date:
                    stop_early = True
                    break

                # Fetch individual page when key fields are absent
                detail: dict = {}
                needs_detail = not title or not authors or not published_at
                if needs_detail and detail_budget > 0:
                    detail = self._fetch_paper_detail(dp_number, abstract_url)
                    detail_budget -= 1

                # Merge detail into listing-level data (listing takes priority
                # for fields it already provided)
                if not title:
                    title = detail.get("title", "")
                if not authors:
                    authors = detail.get("authors", [])
                if not published_at:
                    published_at = detail.get("published_at")

                abstract: str | None = detail.get("abstract")
                doi: str | None = detail.get("doi")
                jel_codes: list[str] = detail.get("jel_codes", [])
                keywords: list[str] = detail.get("keywords", [])

                # Skip entries that still have no title after detail fetch
                if not title:
                    continue

                # Always fetch abstract page if we still lack abstract,
                # as long as we have budget and haven't fetched it yet
                if not abstract and not needs_detail and detail_budget > 0:
                    detail = self._fetch_paper_detail(dp_number, abstract_url)
                    detail_budget -= 1
                    abstract = detail.get("abstract")
                    if not doi:
                        doi = detail.get("doi")
                    if not jel_codes:
                        jel_codes = detail.get("jel_codes", [])
                    if not keywords:
                        keywords = detail.get("keywords", [])

                seen_dp.add(dp_number)

                papers.append({
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "url": abstract_url,
                    "doi": doi,
                    "source": "iza",
                    "source_id": dp_number,
                    "source_url": abstract_url,
                    "published_at": published_at,
                    "paper_type": "working_paper",
                    "jel_codes": jel_codes,
                    "keywords": keywords,
                    "raw_metadata": {
                        "dp_number": dp_number,
                        "authors_raw": authors_raw,
                        "date_raw": date_raw,
                        "source": "iza_html_scrape",
                    },
                })
                page_new += 1

            print(f"[iza]   added {page_new} papers from page {page}", file=sys.stderr)

            if stop_early:
                print(
                    f"[iza] stopping early: reached papers older than {from_date}",
                    file=sys.stderr,
                )
                break

            if len(cards) < _CARDS_PER_PAGE_MIN:
                break

        self._update_state()
        print(f"[iza] collected {len(papers)} discussion papers total", file=sys.stderr)
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = IZASensor()
    sensor.run()
