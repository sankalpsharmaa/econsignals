"""Ministry of Statistics and Programme Implementation (MoSPI) sensor for EconSignals.

Monitors MoSPI press releases and statistical publications including:
- PLFS (Periodic Labour Force Survey) bulletins and annual reports
- GDP / National Accounts estimates (advance, provisional, final)
- CPI (Consumer Price Index) releases
- NSSO / NSO statistical reports
- Other major statistical publications

Primary URLs scraped:
    Press releases:  https://mospi.gov.in/web/mospi/press-release
    Reports:         https://mospi.gov.in/web/mospi/reports-publications
    PLFS:            https://mospi.gov.in/web/mospi/plfs
    Main:            https://www.mospi.gov.in/

State is persisted under the "mospi" key in watches/india/state.json.
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
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py / rbi.py convention)
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_LOOKBACK_DAYS = 60  # MoSPI publishes infrequently; wider window
_MAX_ITEMS_PER_PAGE = 40

_STATE_PATH = PROJ_ROOT / "watches" / "india" / "state.json"

# MoSPI Liferay portal base
_BASE_URL = "https://mospi.gov.in"

_SCRAPE_URLS: list[tuple[str, str]] = [
    # (label, url)
    ("press_release", "https://mospi.gov.in/web/mospi/press-release"),
    ("reports", "https://mospi.gov.in/web/mospi/reports-publications"),
    ("plfs", "https://mospi.gov.in/web/mospi/plfs"),
]

# Keywords that indicate high-value statistical publications
_RESEARCH_KEYWORDS = re.compile(
    r"\b(plfs|periodic labour force|labour force|unemployment|employment"
    r"|gdp|gross domestic product|national accounts|gva"
    r"|cpi|consumer price|inflation|price index|wpi|wholesale price"
    r"|nsso|nso|national sample|household survey"
    r"|census|economic survey|statistical|estimate|advance estimate"
    r"|provisional|quarterly|annual|bulletin|report|release)\b",
    re.IGNORECASE,
)

# Patterns to skip (administrative / non-statistical noise)
_NOISE_PATTERNS = re.compile(
    r"\b(tender|recruitment|vacancy|circular|office order|notification"
    r"|advertisement|result|admit card|interview|e-auction|corrigendum)\b",
    re.IGNORECASE,
)

# Map title keywords to paper types
_REPORT_KEYWORDS = re.compile(
    r"\b(annual report|survey|census|handbook|compendium|yearbook"
    r"|volume|series|study|monograph|bulletin)\b",
    re.IGNORECASE,
)

# JEL regex (MoSPI documents occasionally reference JEL codes)
_RE_JEL = re.compile(
    r"\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)",
    re.IGNORECASE,
)
_RE_JEL_CODE = re.compile(r"[A-Z][0-9]{1,2}")

# Date patterns appearing in press release titles / link text
_RE_DATE_IN_TEXT = re.compile(
    r"\b(\d{1,2})[- ](Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May"
    r"|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"[- ](\d{4})\b",
    re.IGNORECASE,
)
_RE_MONTH_YEAR = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May"
    r"|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"[,\s]+(\d{4})\b",
    re.IGNORECASE,
)
_RE_YEAR_RANGE = re.compile(r"\b(20\d{2})[- ](20\d{2}|\d{2})\b")

_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------------------
# State management (shared with rbi.py via india/state.json)
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/india/state.json, returning empty dict on failure."""
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist state dict to watches/india/state.json."""
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
# HTML parser (stdlib only)
# ---------------------------------------------------------------------------


class _LinkExtractor(HTMLParser):
    """Extract anchor tags with their text content from MoSPI listing pages.

    MoSPI runs on Liferay; content is in standard HTML <a> tags inside
    portlet divs. We collect all anchors and post-filter by relevance.

    Attributes:
        links: List of (href, text, context_text) tuples discovered so far.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[tuple[str, str, str]] = []  # (href, anchor_text, surrounding_text)
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []
        self._depth = 0
        self._in_anchor = False
        # Track surrounding paragraph text for context
        self._para_text_parts: list[str] = []
        self._in_para = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "a":
            href = attr_dict.get("href", "") or ""
            href = href.strip()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                if not href.startswith("http"):
                    href = urljoin(self.base_url, href)
                self._current_href = href
                self._current_text_parts = []
                self._in_anchor = True
        elif tag in ("p", "div", "li", "td"):
            self._para_text_parts = []
            self._in_para = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_anchor:
            if self._current_href:
                text = " ".join(self._current_text_parts).strip()
                text = re.sub(r"\s+", " ", text)
                if text:
                    self.links.append((self._current_href, text, ""))
            self._current_href = None
            self._current_text_parts = []
            self._in_anchor = False

    def handle_data(self, data: str) -> None:
        if self._in_anchor and self._current_href:
            self._current_text_parts.append(data)

    def error(self, message: str) -> None:  # pragma: no cover
        pass  # HTMLParser calls this on malformed HTML; we swallow it


class _DateExtractor(HTMLParser):
    """Extract visible text from HTML to help find publication dates.

    Strips script/style tags and collects all other text nodes.
    """

    def __init__(self) -> None:
        super().__init__()
        self._skip = False
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)

    def error(self, message: str) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace."""
    if not html:
        return ""
    html = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _url_hash(url: str) -> str:
    """Return first 12 hex chars of SHA-256 of url."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL codes from text."""
    codes: list[str] = []
    seen: set[str] = set()
    for match in _RE_JEL.finditer(text):
        for code in _RE_JEL_CODE.findall(match.group(1)):
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _infer_date_from_text(text: str) -> str | None:
    """Try to extract a publication date from title or surrounding text.

    Tries patterns in order of specificity:
    1. "15 Jan 2025", "15 January 2025"
    2. "January 2025", "Jan 2025" -> first of that month
    3. "2024-25" year range -> April of first year (Indian fiscal year start)

    Args:
        text: Title string or nearby text blob.

    Returns:
        ISO date string (YYYY-MM-DD) or None if no date found.
    """
    m = _RE_DATE_IN_TEXT.search(text)
    if m:
        day = int(m.group(1))
        month_str = m.group(2)[:3].lower()
        year = int(m.group(3))
        month = _MONTH_ABBR.get(month_str, 1)
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    m = _RE_MONTH_YEAR.search(text)
    if m:
        month_str = m.group(1)[:3].lower()
        year = int(m.group(2))
        month = _MONTH_ABBR.get(month_str, 1)
        try:
            return datetime(year, month, 1).strftime("%Y-%m-%d")
        except ValueError:
            pass

    m = _RE_YEAR_RANGE.search(text)
    if m:
        year = int(m.group(1))
        return f"{year}-04-01"  # Indian fiscal year start

    return None


def _is_relevant(title: str, description: str = "") -> bool:
    """Return True if the item is a statistical publication worth tracking.

    Args:
        title: Item title string.
        description: Optional surrounding text for additional context.

    Returns:
        True if the item should be included.
    """
    if _NOISE_PATTERNS.search(title):
        return False
    combined = f"{title} {description}"
    return bool(_RESEARCH_KEYWORDS.search(combined))


def _classify_paper_type(title: str) -> str:
    """Return 'report' or 'policy_brief' based on title keywords.

    Press releases and advance/quick estimates are policy_brief.
    Survey reports, bulletins, annual reports are report.

    Args:
        title: Item title string.

    Returns:
        'report' or 'policy_brief'
    """
    title_lower = title.lower()
    # Press releases and flash estimates -> policy_brief
    if re.search(
        r"\b(press release|advance estimate|quick estimate|flash|first advance"
        r"|second advance|provisional estimate|statement)\b",
        title_lower,
    ):
        return "policy_brief"
    # Surveys, bulletins, handbooks -> report
    if _REPORT_KEYWORDS.search(title):
        return "report"
    # CPI / WPI monthly releases are policy_brief
    if re.search(r"\b(cpi|wpi|index number)\b", title_lower):
        return "policy_brief"
    # GDP / GVA press releases -> policy_brief
    if re.search(r"\b(gdp|gva|national accounts)\b", title_lower):
        return "policy_brief"
    return "report"


def _infer_keywords(title: str) -> list[str]:
    """Build a keyword list from the title content.

    Args:
        title: Item title string.

    Returns:
        List of keyword strings always including base MoSPI tags.
    """
    base = ["MoSPI", "India", "statistics", "government"]
    extras: list[str] = []

    checks = [
        (r"\b(plfs|labour force|employment|unemployment)\b", "labour market"),
        (r"\b(gdp|gross domestic product|national accounts|gva)\b", "GDP"),
        (r"\b(cpi|consumer price|inflation)\b", "CPI"),
        (r"\b(wpi|wholesale price)\b", "WPI"),
        (r"\b(nsso|nso|national sample|household survey)\b", "NSSO"),
        (r"\bplfs\b", "PLFS"),
        (r"\bcensus\b", "census"),
        (r"\bquarterly\b", "quarterly estimates"),
        (r"\bannual\b", "annual"),
    ]
    for pattern, kw in checks:
        if re.search(pattern, title, re.IGNORECASE):
            extras.append(kw)

    return base + extras


def _is_mospi_content_url(url: str) -> bool:
    """Return True if the URL looks like a MoSPI content page or document.

    Filters out navigation, login, and external links.

    Args:
        url: URL string to check.

    Returns:
        True if the URL should be considered a publication link.
    """
    parsed = urlparse(url)
    # Must be on mospi.gov.in or related domain
    if "mospi.gov.in" not in parsed.netloc and "mospi.nic.in" not in parsed.netloc:
        return False
    path = parsed.path.lower()
    # Skip pure navigation anchors and known non-content paths
    skip_paths = (
        "/web/mospi/home", "/web/mospi/login", "/web/mospi/about",
        "/web/mospi/contact", "/web/mospi/sitemap",
    )
    if any(path.startswith(s) for s in skip_paths):
        return False
    # Accept document downloads and content pages
    ok_patterns = (
        r"\.(pdf|xlsx?|docx?|pptx?|zip)$",
        r"/documents/",
        r"/web/mospi/(press-release|plfs|reports|publication|statistics|data|release)",
        r"/-/document",
        r"/portlet_file_entry/",
        r"/documents/\d+",
    )
    for pat in ok_patterns:
        if re.search(pat, path, re.IGNORECASE):
            return True
    # Also accept anything with a query parameter pointing at a document
    if "fileId" in parsed.query or "groupId" in parsed.query:
        return True
    return False


# ---------------------------------------------------------------------------
# HTML scraping
# ---------------------------------------------------------------------------


def _parse_listing_page(html_bytes: bytes, page_url: str) -> list[dict]:
    """Extract publication entries from a MoSPI Liferay listing page.

    Runs the _LinkExtractor parser over the page HTML, then post-filters
    links for relevance, deduplicates by URL hash, and returns raw item
    dicts.

    Args:
        html_bytes: Raw HTTP response bytes.
        page_url: The URL of the page (used for relative URL resolution
                  and as base_url for the parser).

    Returns:
        List of raw dicts with keys: title, url, description, page_label.
    """
    try:
        html = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return []

    parser = _LinkExtractor(base_url=page_url)
    try:
        parser.feed(html)
    except Exception as exc:
        warnings.warn(f"[mospi] HTML parse error on {page_url}: {exc}")

    # Also extract raw text for date-guessing later
    text_parser = _DateExtractor()
    try:
        text_parser.feed(html)
    except Exception:
        pass
    page_text = " ".join(text_parser.text_parts)

    items: list[dict] = []
    seen_hashes: set[str] = set()

    for href, anchor_text, _ in parser.links:
        title = re.sub(r"\s+", " ", anchor_text).strip()

        if len(title) < 8:
            continue
        if not _is_relevant(title):
            continue
        if not _is_mospi_content_url(href):
            # Also allow links that at minimum stay on mospi.gov.in
            parsed = urlparse(href)
            if "mospi.gov.in" not in parsed.netloc:
                continue

        url_hash = _url_hash(href)
        if url_hash in seen_hashes:
            continue
        seen_hashes.add(url_hash)

        # Try to infer date from the title, then from nearby page text
        pub_date = _infer_date_from_text(title) or _infer_date_from_text(page_text)

        items.append({
            "title": title,
            "url": href,
            "description": "",
            "pub_date": pub_date,
        })

        if len(items) >= _MAX_ITEMS_PER_PAGE:
            break

    return items


# ---------------------------------------------------------------------------
# Item to paper dict
# ---------------------------------------------------------------------------


def _item_to_paper(raw: dict, cutoff: str, source_label: str) -> dict | None:
    """Convert a raw scraped item to the standard paper dict.

    Skips items that pre-date the cutoff (when a date is known) and
    items that fail the relevance check.

    Args:
        raw: Dict with keys: title, url, description, pub_date.
        cutoff: ISO date string; known items on or before this are skipped.
        source_label: Label of the page this came from (e.g. 'press_release').

    Returns:
        Paper dict or None.
    """
    title = (raw.get("title") or "").strip()
    if not title or len(title) < 8:
        return None

    url = (raw.get("url") or "").strip()
    description = (raw.get("description") or "").strip()
    pub_date = raw.get("pub_date")

    # If we have a date and it's before the cutoff, skip
    if pub_date and pub_date <= cutoff:
        return None

    # Fall back to today if no date inferred
    if not pub_date:
        pub_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    source_id = f"mospi:{_url_hash(url or title)}"
    paper_type = _classify_paper_type(title)
    keywords = _infer_keywords(title)
    jel_codes = _extract_jel_codes(f"{title} {description}")

    abstract = description[:2000] if description else None

    return {
        "title": title,
        "authors": [],  # MoSPI releases are institutional; no named authors
        "abstract": abstract,
        "doi": None,
        "url": url or None,
        "published_at": pub_date,
        "paper_type": paper_type,
        "jel_codes": jel_codes,
        "keywords": keywords,
        "source_id": source_id,
        "source_url": url or None,
        "raw_metadata": {
            "source_page": source_label,
            "url": url,
            "pub_date_raw": raw.get("pub_date"),
        },
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class MoSPISensor(BaseSensor):
    """Sensor that collects MoSPI statistical publications and press releases.

    Scrapes three Liferay portal listing pages:
    - Press releases (CPI, GDP, PLFS bulletins, etc.)
    - Reports & publications
    - PLFS-specific page

    Each page is parsed with stdlib HTMLParser; no third-party HTML library
    is required.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category (india).
        rate_limit: Polite rate for government site (0.5 req/s).
    """

    name = "mospi"
    watch = "india"
    rate_limit = 0.5  # polite; government servers are often under-resourced

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _cutoff_date(self) -> str:
        """Return ISO date string used as the lower-bound filter.

        Reads mospi.last_fetched from watch state; defaults to
        _DEFAULT_LOOKBACK_DAYS days ago.
        """
        state = _load_state()
        last_fetched = (state.get("mospi") or {}).get("last_fetched")
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
        """Record today as last successful MoSPI fetch in state."""
        state = _load_state()
        mospi_state = state.get("mospi") or {}
        mospi_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state["mospi"] = mospi_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[mospi] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    def _scrape_page(self, label: str, url: str, cutoff: str) -> list[dict]:
        """Fetch one listing page and return filtered paper dicts.

        Errors are caught and logged to stderr; never raises.

        Args:
            label: Human-readable label for this page (e.g. 'press_release').
            url: URL to fetch.
            cutoff: ISO date string for the recency filter.

        Returns:
            List of paper dicts from this page.
        """
        print(f"[mospi] fetching {label}: {url}", file=sys.stderr)
        try:
            html_bytes = self.fetch_url(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": _BASE_URL,
                },
                timeout=60,
            )
        except Exception as exc:
            warnings.warn(f"[mospi] failed to fetch {label} ({url}): {exc}")
            return []

        raw_items = _parse_listing_page(html_bytes, url)
        print(f"[mospi] {label}: {len(raw_items)} raw links extracted", file=sys.stderr)

        papers: list[dict] = []
        seen_ids: set[str] = set()

        for raw in raw_items:
            paper = _item_to_paper(raw, cutoff, label)
            if paper is None:
                continue
            sid = paper["source_id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            papers.append(paper)

        print(f"[mospi] {label}: {len(papers)} papers after filtering", file=sys.stderr)
        return papers

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Collect MoSPI statistical publications.

        Steps:
        1. Determine cutoff date from watch state (default: 60 days ago).
        2. Scrape press-release, reports, and PLFS listing pages.
        3. Deduplicate across pages by source_id.
        4. Update watch state.

        Returns:
            Deduplicated list of paper dicts conforming to BaseSensor.collect().
        """
        cutoff = self._cutoff_date()
        print(
            f"[mospi] collecting publications on or after {cutoff}",
            file=sys.stderr,
        )

        all_papers: list[dict] = []
        seen_ids: set[str] = set()

        for label, url in _SCRAPE_URLS:
            try:
                page_papers = self._scrape_page(label, url, cutoff)
            except Exception as exc:
                warnings.warn(f"[mospi] unexpected error scraping {label}: {exc}")
                page_papers = []

            for paper in page_papers:
                sid = paper["source_id"]
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    all_papers.append(paper)

        print(f"[mospi] collected {len(all_papers)} total papers", file=sys.stderr)
        self._update_state()
        return all_papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = MoSPISensor()
    sensor.run()
