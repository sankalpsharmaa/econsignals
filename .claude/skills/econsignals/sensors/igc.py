"""IGC (International Growth Centre) publications sensor for EconSignals.

Scrapes theigc.org/publications for working papers and policy briefs covering
growth and development in low-income countries.

The IGC site is a Drupal CMS with no stable public API or RSS feed.  This
sensor parses the HTML listing pages using only the standard library (html.parser
and regex), then optionally fetches individual paper pages for abstracts when
they are absent from the listing.

Pagination uses: https://www.theigc.org/publications?type=working-paper&page=N

State is stored under the "igc" key in watches/india/state.json, tracking the
last successfully fetched date so old papers are not re-ingested on every run.

Usage:
    python sensors/igc.py
"""

from __future__ import annotations

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
from urllib.parse import urljoin, urlparse

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

_BASE_URL = "https://www.theigc.org"
_PUBLICATIONS_URL = "https://www.theigc.org/publications"

# IGC publication type slugs used in the query string filter
_PUB_TYPES: list[tuple[str, str]] = [
    ("working-paper", "working_paper"),
    ("policy-brief", "policy_brief"),
]

_MAX_PAGES = 5          # per publication type
_DEFAULT_LOOKBACK_DAYS = 30

_STATE_PATH = PROJ_ROOT / "watches" / "india" / "state.json"

# Sentinel keywords always applied to IGC papers
_BASE_KEYWORDS: list[str] = ["development", "growth", "IGC"]

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Matches a /publications/<slug> href (not a query-string URL)
_RE_PUB_HREF = re.compile(r'^/publications/([a-z0-9][a-z0-9\-]{2,})$')

# ISO date (YYYY-MM-DD) in text or attribute values
_RE_ISO_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})')

# Natural language month+year, e.g. "January 2024" or "Jan 2024"
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

# JEL code pattern
_RE_JEL_LABEL = re.compile(r'\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)', re.IGNORECASE)
_RE_JEL_CODE = re.compile(r'[A-Z][0-9]{1,2}')


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> str | None:
    """Convert a raw date string to ISO YYYY-MM-DD, or return None.

    Tries ISO, ISO datetime prefix, and natural language month+year.

    Args:
        raw: Raw date string, or None.

    Returns:
        'YYYY-MM-DD' string, or None if unparseable.

    Examples:
        >>> _parse_date("2024-03-15")
        '2024-03-15'
        >>> _parse_date("March 2024")
        '2024-03-01'
        >>> _parse_date(None)
    """
    if not raw:
        return None
    raw = raw.strip()

    # ISO prefix
    m = _RE_ISO_DATE.search(raw)
    if m:
        return m.group(1)

    # "March 2024" / "Mar 2024"
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

    Examples:
        >>> _strip_html("<p>Hello &amp; world</p>")
        'Hello & world'
    """
    if not html:
        return ""
    # Remove script/style blocks
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
# HTML parser for the publications listing page
# ---------------------------------------------------------------------------


class _ListingParser(HTMLParser):
    """Extract publication slugs and surface-level metadata from an IGC listing page.

    The IGC Drupal site renders publications as anchor tags whose href matches
    /publications/<slug>.  Each anchor contains an <h3> (title) and <p> tags
    (authors, pub type, and sometimes a date snippet).

    This parser is intentionally permissive: it records whatever it can find
    and never raises.  The caller validates the result.

    Attributes:
        items: List of dicts, one per discovered publication link.
            Each dict has: slug, title, authors_raw, type_raw, date_raw.
    """

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []

        # Mutable state while traversing a publication anchor
        self._in_pub_anchor: bool = False
        self._current: dict | None = None
        self._capture_h3: bool = False
        self._h3_depth: int = 0
        self._capture_p: bool = False
        self._p_depth: int = 0
        self._p_texts: list[str] = []   # collects <p> texts within a card
        self._depth: int = 0            # nesting depth inside current anchor
        self._buf: str = ""             # text buffer for the active capture

    # ------------------------------------------------------------------
    # HTMLParser callbacks
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        tag = tag.lower()

        if tag == "a":
            href = attr_map.get("href") or ""
            m = _RE_PUB_HREF.match(href)
            if m:
                self._in_pub_anchor = True
                self._current = {
                    "slug": m.group(1),
                    "title": "",
                    "authors_raw": "",
                    "type_raw": "",
                    "date_raw": "",
                }
                self._depth = 0
                self._p_texts = []
                return

        if self._in_pub_anchor:
            self._depth += 1
            if tag == "h3":
                self._capture_h3 = True
                self._h3_depth = self._depth
                self._buf = ""
            elif tag == "p":
                self._capture_p = True
                self._p_depth = self._depth
                self._buf = ""

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if self._capture_h3 and tag == "h3":
            if self._current is not None:
                self._current["title"] = _strip_html(self._buf).strip()
            self._capture_h3 = False
            self._buf = ""

        elif self._capture_p and tag == "p":
            text = _strip_html(self._buf).strip()
            if text:
                self._p_texts.append(text)
            self._capture_p = False
            self._buf = ""

        elif self._in_pub_anchor and tag == "a":
            # Closing the publication anchor -- assign collected <p> texts
            if self._current is not None and self._current.get("title"):
                # Heuristic assignment: first <p> = authors, second = type
                if len(self._p_texts) >= 1:
                    self._current["authors_raw"] = self._p_texts[0]
                if len(self._p_texts) >= 2:
                    self._current["type_raw"] = self._p_texts[1]
                if len(self._p_texts) >= 3:
                    # Sometimes a date snippet appears as a third <p>
                    self._current["date_raw"] = self._p_texts[2]
                self.items.append(self._current)

            self._in_pub_anchor = False
            self._current = None
            self._capture_h3 = False
            self._capture_p = False
            self._p_texts = []
            self._depth = 0

        if self._in_pub_anchor:
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture_h3 or self._capture_p:
            self._buf += data

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        if self._capture_h3 or self._capture_p:
            self._buf += f"&{name};"

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        if self._capture_h3 or self._capture_p:
            self._buf += f"&#{name};"


# ---------------------------------------------------------------------------
# Individual paper page parsing
# ---------------------------------------------------------------------------


def _extract_from_paper_page(html: str) -> dict:
    """Extract abstract, authors, date, and JEL codes from an IGC paper page.

    Applied only when listing-level data is insufficient (e.g. abstract missing).
    Uses a series of defensive regex patterns; returns an empty dict on failure.

    Args:
        html: Raw HTML of the individual paper page.

    Returns:
        Partial dict with any of: abstract, authors (list[str]),
        published_at (str), jel_codes (list[str]), keywords (list[str]).
    """
    result: dict = {}

    # --- Abstract ---
    for pattern in (
        r'class="[^"]*(?:abstract|summary)[^"]*"[^>]*>(.*?)</(?:div|section|p)>',
        r'<meta\s+(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\'](.*?)["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            candidate = _strip_html(m.group(1)).strip()
            if len(candidate) > 60:
                result["abstract"] = candidate[:4000]
                break

    # --- Authors ---
    authors: list[str] = []
    for pattern in (
        r'class="[^"]*author[^"]*"[^>]*>(.*?)</(?:span|div|a|p|li)>',
        r'itemprop="author"[^>]*>(.*?)</(?:span|div|a|p)>',
        r'<meta\s+name=["\']author["\']\s+content=["\'](.*?)["\']',
    ):
        for m in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
            name = _strip_html(m.group(1)).strip()
            if name and len(name) < 120 and name not in authors:
                authors.append(name)
        if authors:
            break
    if authors:
        result["authors"] = authors

    # --- Date ---
    for pattern in (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<meta\s+(?:name|property)=["\'](?:pubdate|article:published_time)["\'][^>]*content=["\'](.*?)["\']',
        r'(?:Published|Date)[:\s]*<[^>]*>([^<]{6,30})<',
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            dt = _parse_date(m.group(1).strip())
            if dt:
                result["published_at"] = dt
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

    # --- Tags / themes ---
    tags: list[str] = []
    for pattern in (
        r'class="[^"]*(?:tag|topic|theme)[^"]*"[^>]*>(.*?)</(?:a|span|li)>',
        r'<a[^>]+rel=["\']tag["\'][^>]*>(.*?)</a>',
    ):
        for m in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
            tag = _strip_html(m.group(1)).strip()
            if tag and len(tag) < 60 and tag not in tags:
                tags.append(tag)
    if tags:
        result["keywords"] = tags

    return result


# ---------------------------------------------------------------------------
# Author string parsing
# ---------------------------------------------------------------------------


def _parse_authors(raw: str) -> list[str]:
    """Split a comma/semicolon/ampersand-delimited author string into a list.

    Args:
        raw: Raw author text, e.g. "Robin Burgess, Stefano Caria and Tim Dobermann".

    Returns:
        List of individual author name strings.
    """
    if not raw:
        return []
    # Normalise common delimiters
    normalised = re.sub(r'\band\b', ',', raw, flags=re.IGNORECASE)
    normalised = normalised.replace(';', ',').replace('&', ',')
    parts = [p.strip() for p in normalised.split(',') if p.strip()]
    # Filter out obvious non-names (very long strings, all-caps abbreviations)
    return [p for p in parts if 2 < len(p) < 100]


# ---------------------------------------------------------------------------
# Paper type mapping
# ---------------------------------------------------------------------------


def _normalise_type(raw: str) -> str:
    """Map an IGC publication type label to the standard paper_type string.

    Args:
        raw: Raw type label from the listing, e.g. "Working paper".

    Returns:
        One of: 'working_paper', 'policy_brief', or 'working_paper' as default.
    """
    lower = raw.lower()
    if "policy" in lower or "brief" in lower:
        return "policy_brief"
    return "working_paper"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/india/state.json, returning empty dict on failure.

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
    """Atomically persist state dict to watches/india/state.json.

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


class IGCSensor(BaseSensor):
    """Scrape IGC publications (theigc.org) for working papers and policy briefs.

    The IGC website has no public API.  This sensor fetches HTML listing pages,
    parses publication cards with html.parser and regex, and optionally retrieves
    individual paper pages to fill in missing abstracts.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category (india).
        rate_limit: 0.5 requests/second -- very polite, 2 s between calls.
        BASE_URL: Root URL for theigc.org.
        PUBLICATIONS_URL: Base publications listing URL.
    """

    name = "igc"
    watch = "india"
    rate_limit = 0.5   # 2 seconds between requests

    BASE_URL = _BASE_URL
    PUBLICATIONS_URL = _PUBLICATIONS_URL

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return the ISO date lower-bound for publication filtering.

        Reads igc.last_fetched from watches/india/state.json.  Falls back to
        _DEFAULT_LOOKBACK_DAYS days ago on first run.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = (state.get("igc") or {}).get("last_fetched")
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
        """Record today's date as the last successful IGC fetch in state."""
        state = _load_state()
        igc_state = state.get("igc") or {}
        igc_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state["igc"] = igc_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[igc] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Listing-page fetch and parse
    # ------------------------------------------------------------------

    def _listing_url(self, pub_type_slug: str, page: int) -> str:
        """Build a paginated IGC publications listing URL.

        Args:
            pub_type_slug: URL filter value, e.g. 'working-paper'.
            page: Zero-based page number.

        Returns:
            Fully-qualified URL string.
        """
        return f"{_PUBLICATIONS_URL}?type={pub_type_slug}&page={page}"

    def _fetch_listing_page(self, url: str) -> list[dict]:
        """Fetch a single listing page and parse publication cards from the HTML.

        Args:
            url: Full listing URL to fetch.

        Returns:
            List of raw card dicts from _ListingParser, possibly empty on error.
        """
        try:
            raw = self.fetch_url(url)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[igc] failed to fetch listing {url}: {exc}", file=sys.stderr)
            return []

        parser = _ListingParser()
        try:
            parser.feed(html)
        except Exception as exc:
            print(f"[igc] HTML parse error on {url}: {exc}", file=sys.stderr)
        return parser.items

    def _has_next_page(self, url: str, current_page: int) -> bool:
        """Check whether the listing page contains a next-page link.

        IGC pagination uses hrefs like ?type=working-paper&page=N.
        We infer a next page exists if the number of cards equals a full page
        (the site typically shows 12 per page).  This avoids a second fetch.

        Args:
            url: The URL that was just fetched (unused; kept for signature clarity).
            current_page: The zero-based page number just fetched.

        Returns:
            True if it is worth fetching page current_page+1.
        """
        # Handled by the caller checking card count vs threshold
        return True  # caller decides based on card count

    # ------------------------------------------------------------------
    # Paper-page detail fetch
    # ------------------------------------------------------------------

    def _fetch_paper_detail(self, slug: str) -> dict:
        """Fetch and parse an individual IGC paper page for extra metadata.

        Only called when the listing card lacks an abstract.  Returns an empty
        dict on any error so the caller can proceed with what it already has.

        Args:
            slug: IGC URL slug, e.g. 'impact-of-land-reform-in-india'.

        Returns:
            Partial paper dict with any of: abstract, authors, published_at,
            jel_codes, keywords.
        """
        url = f"{_BASE_URL}/publications/{slug}"
        try:
            raw = self.fetch_url(url)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[igc] failed to fetch paper page {url}: {exc}", file=sys.stderr)
            return {}

        try:
            return _extract_from_paper_page(html)
        except Exception as exc:
            print(f"[igc] parse error on paper page {url}: {exc}", file=sys.stderr)
            return {}

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Scrape IGC publications and return standard paper dicts.

        Steps:
        1. Determine from_date via watch state (default: 30 days ago).
        2. For each publication type (working paper, policy brief):
           a. Paginate the listing up to _MAX_PAGES pages.
           b. Parse cards from each listing page.
           c. Stop early when all cards on a page predate from_date (site lists
              newest-first), or when fewer than the expected cards-per-page are
              returned (indicating the last page).
        3. For cards missing an abstract, optionally fetch the individual paper
           page (controlled by a request budget to avoid hammering the server).
        4. Build the standard paper dict for each item.
        5. Update watch state on success.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
            Returns an empty list on total failure (defensive).
        """
        from_date = self._from_date()
        print(
            f"[igc] collecting publications on or after {from_date}",
            file=sys.stderr,
        )

        papers: list[dict] = []
        seen_slugs: set[str] = set()

        # Typical IGC listing page shows ~12 cards; we use 8 as the "full page"
        # lower bound -- if fewer come back, we're on the last page.
        _CARDS_PER_PAGE_MIN = 8

        # Per-run budget for individual paper page fetches (abstracts).
        # Kept low to respect the rate limit and avoid hammering the server.
        detail_budget = 20

        for type_slug, paper_type in _PUB_TYPES:
            print(f"[igc] scanning type={type_slug!r}", file=sys.stderr)
            stop_early = False

            for page in range(_MAX_PAGES):
                if stop_early:
                    break

                url = self._listing_url(type_slug, page)
                cards = self._fetch_listing_page(url)

                print(
                    f"[igc]   page={page} type={type_slug!r}: {len(cards)} cards",
                    file=sys.stderr,
                )

                if not cards:
                    # No cards -- either a parse failure or the last page
                    break

                page_new = 0
                all_old = True

                for card in cards:
                    slug = card.get("slug", "").strip()
                    if not slug or slug in seen_slugs:
                        continue

                    title = card.get("title", "").strip()
                    if not title:
                        continue

                    # Parse what we have from the listing
                    authors_raw = card.get("authors_raw", "")
                    date_raw = card.get("date_raw", "")
                    type_raw = card.get("type_raw", "")

                    authors = _parse_authors(authors_raw)
                    published_at = _parse_date(date_raw)
                    resolved_type = _normalise_type(type_raw or type_slug)
                    keywords = list(_BASE_KEYWORDS)

                    # If we have a date and it predates our window, the listing
                    # is sorted newest-first so we can stop for this type.
                    if published_at and published_at < from_date:
                        stop_early = True
                        break
                    all_old = False

                    seen_slugs.add(slug)

                    source_url = f"{_BASE_URL}/publications/{slug}"

                    # Attempt to get extra metadata from the paper page if we
                    # lack authors or abstract and have budget remaining.
                    detail: dict = {}
                    if (not authors or not card.get("abstract")) and detail_budget > 0:
                        detail = self._fetch_paper_detail(slug)
                        detail_budget -= 1

                    abstract: str | None = detail.get("abstract")
                    if not authors and detail.get("authors"):
                        authors = detail["authors"]
                    if not published_at and detail.get("published_at"):
                        published_at = detail["published_at"]
                    jel_codes: list[str] = detail.get("jel_codes", [])
                    extra_keywords: list[str] = detail.get("keywords", [])
                    for kw in extra_keywords:
                        if kw not in keywords:
                            keywords.append(kw)

                    papers.append({
                        "title": title,
                        "authors": authors,
                        "abstract": abstract,
                        "doi": None,
                        "url": source_url,
                        "published_at": published_at,
                        "paper_type": resolved_type,
                        "jel_codes": jel_codes,
                        "keywords": keywords,
                        "source_id": slug,
                        "source_url": source_url,
                        "raw_metadata": {
                            "slug": slug,
                            "authors_raw": authors_raw,
                            "type_raw": type_raw,
                            "date_raw": date_raw,
                            "source": "igc_html_scrape",
                        },
                    })
                    page_new += 1

                # If we got fewer cards than the expected minimum, this is the
                # last page -- no point fetching the next one.
                if len(cards) < _CARDS_PER_PAGE_MIN:
                    break

                # If every card on this page was newer than from_date but we
                # flagged stop_early (a card was old), honour it next iteration.
                if all_old and not stop_early:
                    break

        self._update_state()
        print(f"[igc] collected {len(papers)} publications", file=sys.stderr)
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = IGCSensor()
    sensor.run()
