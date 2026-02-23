"""India think tank publications sensor for EconSignals.

Monitors publications from major Indian economics research institutions by
scraping their public publications pages. No APIs are available for these
sites, so all collection is done via HTML parsing with regex.

Institutions monitored:
    NCAER, CPR, ICRIER, IGIDR, APU, IIHS

Each institution has a dedicated parser method. A generic fallback parser
handles sites whose structure doesn't match the dedicated parser. All
parsers are defensive: any failure yields an empty list for that tank,
allowing collection to continue for the remaining institutions.

Usage:
    python sensors/india_think_tanks.py

State:
    No persistent state is maintained. Every run re-scrapes all pages.
    Deduplication is handled downstream by lib/dedup.py via source_id.
"""

from __future__ import annotations

import hashlib
import re
import sys
import warnings
from datetime import datetime
from html import unescape
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py convention)
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Institution registry
# ---------------------------------------------------------------------------

THINK_TANKS: dict[str, dict[str, str | None]] = {
    "ncaer": {
        "name": "National Council of Applied Economic Research",
        "publications_url": "https://www.ncaer.org/publications",
        "rss_url": None,
    },
    "cpr": {
        "name": "Centre for Policy Research",
        "publications_url": "https://cprindia.org/research/papers",
        "rss_url": None,
    },
    "icrier": {
        "name": "Indian Council for Research on International Economic Relations",
        "publications_url": "https://icrier.org/publications/working-papers/",
        "rss_url": None,
    },
    "igidr": {
        "name": "Indira Gandhi Institute of Development Research",
        "publications_url": "http://www.igidr.ac.in/publications/working-papers/",
        "rss_url": None,
    },
    "apu": {
        "name": "Azim Premji University",
        "publications_url": "https://azimpremjiuniversity.edu.in/research/publications",
        "rss_url": None,
    },
    "iihs": {
        "name": "Indian Institute for Human Settlements",
        "publications_url": "https://iihs.co.in/knowledge-gateway/publications/",
        "rss_url": None,
    },
}

# ---------------------------------------------------------------------------
# Shared regex patterns
# ---------------------------------------------------------------------------

# Matches ISO dates, "January 2024", "Jan 2024", "01 Jan 2024", "01/2024", etc.
_RE_DATE = re.compile(
    r"""
    (?:
        \b(\d{4}-\d{2}-\d{2})\b                           # YYYY-MM-DD
      | \b(\d{1,2}[\s\-/]\w{3,9}[\s\-/]\d{4})\b          # DD Mon YYYY
      | \b(\w{3,9}\s+\d{1,2},?\s+\d{4})\b                # Month DD, YYYY
      | \b(\w{3,9}\s+\d{4})\b                             # Month YYYY
      | \b(\d{4})\b                                        # bare year
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_DATE_FMTS = (
    "%Y-%m-%d",
    "%d %B %Y",
    "%d %b %Y",
    "%d-%B-%Y",
    "%d-%b-%Y",
    "%d/%B/%Y",
    "%d/%b/%Y",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%B %Y",
    "%b %Y",
)

_RE_JEL_BLOCK = re.compile(
    r"JEL[^:]*:\s*((?:[A-Z][0-9]{1,2}[,;\s]*)+)", re.IGNORECASE
)
_RE_JEL_CODE = re.compile(r"\b[A-Z][0-9]{1,2}\b")

# Detect paper type keywords in title / surrounding text
_RE_POLICY_BRIEF = re.compile(
    r"\b(?:policy\s*brief|policy\s*note|briefing\s*note)\b", re.IGNORECASE
)

# Generic link pattern used by the fallback parser
_RE_ANCHOR = re.compile(
    r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _strip_html(fragment: str) -> str:
    """Remove HTML tags and collapse whitespace, returning plain text.

    Args:
        fragment: Raw HTML string or plain text.

    Returns:
        Plain-text string with normalised whitespace.

    Examples:
        >>> _strip_html("<b>Hello</b> <i>world</i>")
        'Hello world'
    """
    if not fragment:
        return ""
    # Drop script/style blocks wholesale
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
    """Parse a raw date string into ISO YYYY-MM-DD format.

    Tries a series of common date formats found on Indian think tank pages.
    Returns None when parsing fails rather than raising.

    Args:
        raw: Raw date string, e.g. "March 2024" or "12 Jan 2024".

    Returns:
        ISO date string "YYYY-MM-DD", or None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()

    # Bare four-digit year -> Jan 1st of that year
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw}-01-01"

    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Try stripping a leading YYYY-MM-DD prefix
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)

    return None


def _first_date(text: str) -> str | None:
    """Extract and parse the first date-like string found in text.

    Args:
        text: Plain text or HTML fragment to search.

    Returns:
        ISO date string or None.
    """
    for m in _RE_DATE.finditer(text):
        candidate = next((g for g in m.groups() if g), None)
        if candidate:
            parsed = _parse_date(candidate.strip())
            if parsed:
                return parsed
    return None


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL classification codes from a text block.

    Looks for explicit "JEL: X1, Y2" patterns.

    Args:
        text: Plain text to search.

    Returns:
        Deduplicated list of JEL code strings.
    """
    codes: list[str] = []
    seen: set[str] = set()
    for m in _RE_JEL_BLOCK.finditer(text):
        for code in _RE_JEL_CODE.findall(m.group(1)):
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _url_hash(url: str) -> str:
    """Return a short hex digest of the URL for use in source_id.

    Args:
        url: The canonical URL string.

    Returns:
        8-character lowercase hex string.
    """
    return hashlib.sha1(url.encode()).hexdigest()[:8]


def _paper_type(title: str, context: str = "") -> str:
    """Infer paper_type from title and surrounding context text.

    Args:
        title: Paper title.
        context: Additional text (abstract, surrounding HTML) to check.

    Returns:
        "policy_brief" or "working_paper".
    """
    combined = f"{title} {context}"
    if _RE_POLICY_BRIEF.search(combined):
        return "policy_brief"
    return "working_paper"


def _authors_from_text(text: str) -> list[str]:
    """Heuristically extract author names from a plain-text blob.

    Looks for comma- or semicolon-separated sequences of title-case words
    that resemble personal names. False positives are likely; callers should
    trim to reasonable length.

    Args:
        text: Plain-text blob, e.g. "by Abhijit Banerjee and Esther Duflo".

    Returns:
        List of extracted name strings, possibly empty.
    """
    # Strip "by", "authors:", etc.
    text = re.sub(r"^\s*(?:by|authors?)\s*[:\-]?\s*", "", text, flags=re.IGNORECASE)
    # Split on " and ", "," or ";"
    parts = re.split(r"\s*(?:,\s*|\s+and\s+|;\s*)\s*", text)
    names = []
    for part in parts:
        part = part.strip()
        # Require at least two words, each starting with a capital letter
        words = part.split()
        if 2 <= len(words) <= 5 and all(w[0].isupper() for w in words if w):
            names.append(part)
    return names


# ---------------------------------------------------------------------------
# Per-tank parsers
# ---------------------------------------------------------------------------


def _parse_ncaer(html: bytes) -> list[dict]:
    """Parse NCAER publications listing page.

    NCAER renders publication listings with article elements containing
    an <h3> or <h4> title link, optional author line, and a date.

    Args:
        html: Raw HTML bytes from the publications page.

    Returns:
        List of partial paper dicts (title, url, authors, published_at,
        abstract, paper_type).
    """
    text = html.decode("utf-8", errors="replace")
    papers: list[dict] = []

    # NCAER typically wraps each listing in <div class="...pub..."> or <article>
    # Capture blocks between publication entry anchors
    # Strategy: find all <h3>/<h4>/<h2> with an <a href> inside
    block_re = re.compile(
        r'<(?:h[2-4]|div)[^>]*class="[^"]*(?:pub|paper|title|entry-title)[^"]*"[^>]*>'
        r'(.*?)</(?:h[2-4]|div)>',
        re.DOTALL | re.IGNORECASE,
    )

    seen_urls: set[str] = set()

    for block_m in block_re.finditer(text):
        block = block_m.group(0)
        link_m = re.search(
            r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            block,
            re.DOTALL | re.IGNORECASE,
        )
        if not link_m:
            continue

        href = link_m.group(1).strip()
        title = _strip_html(link_m.group(2)).strip()
        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Grab surrounding context (200 chars before and after the block)
        start = max(0, block_m.start() - 200)
        end = min(len(text), block_m.end() + 400)
        context = text[start:end]

        # Resolve relative URL
        if href.startswith("/"):
            href = "https://www.ncaer.org" + href
        elif not href.startswith("http"):
            href = "https://www.ncaer.org/" + href.lstrip("/")

        published_at = _first_date(_strip_html(context))

        # Authors: look for a line mentioning "by" or author names near the title
        author_text = ""
        author_m = re.search(
            r'(?:by|author)[^<]{0,5}[:\-]?\s*([A-Z][a-zA-Z\s,\.]{5,100})',
            context, re.IGNORECASE,
        )
        if author_m:
            author_text = author_m.group(1)

        papers.append({
            "title": title,
            "url": href,
            "authors": _authors_from_text(author_text),
            "published_at": published_at,
            "abstract": None,
            "paper_type": _paper_type(title),
        })

    # Fallback to generic if nothing found
    if not papers:
        papers = _generic_parse(text, "https://www.ncaer.org")

    return papers


def _parse_cpr(html: bytes) -> list[dict]:
    """Parse Centre for Policy Research publications page.

    CPR lists papers in article/div blocks with <h3> titles and author
    bylines, often with a "Published:" date nearby.

    Args:
        html: Raw HTML bytes from the publications page.

    Returns:
        List of partial paper dicts.
    """
    text = html.decode("utf-8", errors="replace")
    papers: list[dict] = []
    seen_urls: set[str] = set()

    # CPR tends to use <article> or <div class="...item..."> containers
    entry_re = re.compile(
        r'<(?:article|li)[^>]*>(.*?)</(?:article|li)>',
        re.DOTALL | re.IGNORECASE,
    )

    for entry_m in entry_re.finditer(text):
        entry = entry_m.group(1)

        link_m = re.search(
            r'<a\s[^>]*href=["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
            entry,
            re.DOTALL | re.IGNORECASE,
        )
        if not link_m:
            continue

        href = link_m.group(1).strip()
        title = _strip_html(link_m.group(2)).strip()
        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        if href.startswith("/"):
            href = "https://cprindia.org" + href
        elif not href.startswith("http"):
            href = "https://cprindia.org/" + href.lstrip("/")

        plain = _strip_html(entry)
        published_at = _first_date(plain)

        # CPR often has author names in a <span class="...author..."> element
        author_m = re.search(
            r'class="[^"]*(?:author|byline|contributor)[^"]*"[^>]*>(.*?)</(?:span|div|p)>',
            entry, re.DOTALL | re.IGNORECASE,
        )
        authors: list[str] = []
        if author_m:
            authors = _authors_from_text(_strip_html(author_m.group(1)))

        # Abstract: any <p> with decent text inside the entry
        abstract: str | None = None
        for p_m in re.finditer(r'<p[^>]*>(.*?)</p>', entry, re.DOTALL | re.IGNORECASE):
            candidate = _strip_html(p_m.group(1)).strip()
            if len(candidate) > 60 and candidate != title:
                abstract = candidate[:2000]
                break

        papers.append({
            "title": title,
            "url": href,
            "authors": authors,
            "published_at": published_at,
            "abstract": abstract,
            "paper_type": _paper_type(title),
        })

    if not papers:
        papers = _generic_parse(text, "https://cprindia.org")

    return papers


def _parse_icrier(html: bytes) -> list[dict]:
    """Parse ICRIER working papers listing page.

    ICRIER lists working papers in table rows or div blocks with a paper
    number, title link, author list, and year.

    Args:
        html: Raw HTML bytes from the publications page.

    Returns:
        List of partial paper dicts.
    """
    text = html.decode("utf-8", errors="replace")
    papers: list[dict] = []
    seen_urls: set[str] = set()

    # ICRIER working paper pages often have rows: WP-NNN | Title | Authors | Year
    # Try <tr> rows first
    row_re = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    for row_m in row_re.finditer(text):
        row = row_m.group(1)
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
        if len(cells) < 2:
            continue

        # Find the cell with a link
        title = ""
        href = ""
        authors_raw = ""
        date_raw = ""

        for i, cell in enumerate(cells):
            link_m = re.search(
                r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                cell, re.DOTALL | re.IGNORECASE,
            )
            if link_m and not title:
                href = link_m.group(1).strip()
                title = _strip_html(link_m.group(2)).strip()
            else:
                plain = _strip_html(cell).strip()
                # Heuristic: later cells with all-caps or name-like text are authors
                if i > 0 and not date_raw and re.search(r"\d{4}", plain):
                    date_raw = plain
                elif i > 0 and not authors_raw and len(plain) > 3:
                    authors_raw = plain

        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        if href.startswith("/"):
            href = "https://icrier.org" + href
        elif not href.startswith("http"):
            href = "https://icrier.org/" + href.lstrip("/")

        papers.append({
            "title": title,
            "url": href,
            "authors": _authors_from_text(authors_raw),
            "published_at": _parse_date(date_raw) if date_raw else _first_date(date_raw),
            "abstract": None,
            "paper_type": "working_paper",
        })

    # Fallback to div/link scanning
    if not papers:
        papers = _generic_parse(text, "https://icrier.org")

    return papers


def _parse_igidr(html: bytes) -> list[dict]:
    """Parse IGIDR working papers listing page.

    IGIDR typically lists papers in a table with columns for paper ID,
    title, authors, and year. Some pages use div-based layouts.

    Args:
        html: Raw HTML bytes from the publications page.

    Returns:
        List of partial paper dicts.
    """
    text = html.decode("utf-8", errors="replace")
    papers: list[dict] = []
    seen_urls: set[str] = set()

    base_url = "http://www.igidr.ac.in"

    # IGIDR often wraps each entry in <div class="views-row"> or similar
    entry_re = re.compile(
        r'<div[^>]*class="[^"]*(?:views-row|paper-item|publication-item)[^"]*"[^>]*>(.*?)</div>\s*</div>',
        re.DOTALL | re.IGNORECASE,
    )

    for entry_m in entry_re.finditer(text):
        entry = entry_m.group(1)
        link_m = re.search(
            r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            entry, re.DOTALL | re.IGNORECASE,
        )
        if not link_m:
            continue

        href = link_m.group(1).strip()
        title = _strip_html(link_m.group(2)).strip()
        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        if href.startswith("/"):
            href = base_url + href
        elif not href.startswith("http"):
            href = base_url + "/" + href.lstrip("/")

        plain = _strip_html(entry)
        published_at = _first_date(plain)

        authors: list[str] = []
        author_m = re.search(
            r'class="[^"]*(?:author|field-name-field-author)[^"]*"[^>]*>(.*?)</div>',
            entry, re.DOTALL | re.IGNORECASE,
        )
        if author_m:
            authors = _authors_from_text(_strip_html(author_m.group(1)))

        papers.append({
            "title": title,
            "url": href,
            "authors": authors,
            "published_at": published_at,
            "abstract": None,
            "paper_type": "working_paper",
        })

    # Fallback: scan for WP-number patterns alongside links
    if not papers:
        wp_block_re = re.compile(
            r'(?:WP-?\d{3,4}|Working\s+Paper\s+No\.?\s*\d+)[^\n<]{0,200}',
            re.IGNORECASE,
        )
        for m in wp_block_re.finditer(text):
            block_start = max(0, m.start() - 100)
            block_end = min(len(text), m.end() + 400)
            block = text[block_start:block_end]
            link_m = re.search(
                r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                block, re.DOTALL | re.IGNORECASE,
            )
            if not link_m:
                continue
            href = link_m.group(1).strip()
            title = _strip_html(link_m.group(2)).strip()
            if not title or len(title) < 8:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
            if href.startswith("/"):
                href = base_url + href
            plain = _strip_html(block)
            papers.append({
                "title": title,
                "url": href,
                "authors": [],
                "published_at": _first_date(plain),
                "abstract": None,
                "paper_type": "working_paper",
            })

    if not papers:
        papers = _generic_parse(text, base_url)

    return papers


def _parse_apu(html: bytes) -> list[dict]:
    """Parse Azim Premji University publications page.

    APU uses a card-based layout. Each publication card has a title link,
    category tag, and optional author/date metadata.

    Args:
        html: Raw HTML bytes from the publications page.

    Returns:
        List of partial paper dicts.
    """
    text = html.decode("utf-8", errors="replace")
    papers: list[dict] = []
    seen_urls: set[str] = set()

    base_url = "https://azimpremjiuniversity.edu.in"

    # APU uses card / item container divs
    card_re = re.compile(
        r'<(?:div|article)[^>]*class="[^"]*(?:card|item|publication|result)[^"]*"[^>]*>'
        r'(.*?)</(?:div|article)>',
        re.DOTALL | re.IGNORECASE,
    )

    for card_m in card_re.finditer(text):
        card = card_m.group(1)

        # Find first meaningful <a href>
        link_m = re.search(
            r'<a\s[^>]*href=["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
            card, re.DOTALL | re.IGNORECASE,
        )
        if not link_m:
            continue

        href = link_m.group(1).strip()
        title = _strip_html(link_m.group(2)).strip()
        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        if href.startswith("/"):
            href = base_url + href
        elif not href.startswith("http"):
            href = base_url + "/" + href.lstrip("/")

        plain = _strip_html(card)
        published_at = _first_date(plain)

        # APU often lists authors in a separate span/div
        author_m = re.search(
            r'class="[^"]*(?:author|contributor|faculty)[^"]*"[^>]*>(.*?)</(?:span|div|p)>',
            card, re.DOTALL | re.IGNORECASE,
        )
        authors: list[str] = []
        if author_m:
            authors = _authors_from_text(_strip_html(author_m.group(1)))

        # Paper type: look for category tag
        ptype = _paper_type(title, plain)
        if re.search(r"\bworking\s+paper\b", plain, re.IGNORECASE):
            ptype = "working_paper"

        papers.append({
            "title": title,
            "url": href,
            "authors": authors,
            "published_at": published_at,
            "abstract": None,
            "paper_type": ptype,
        })

    if not papers:
        papers = _generic_parse(text, base_url)

    return papers


def _parse_iihs(html: bytes) -> list[dict]:
    """Parse IIHS knowledge gateway publications page.

    IIHS uses a card or list layout with title links and metadata including
    type tags (Working Paper, Policy Brief, etc.) and dates.

    Args:
        html: Raw HTML bytes from the publications page.

    Returns:
        List of partial paper dicts.
    """
    text = html.decode("utf-8", errors="replace")
    papers: list[dict] = []
    seen_urls: set[str] = set()

    base_url = "https://iihs.co.in"

    # IIHS knowledge gateway wraps each entry in <div class="...resource..."> or <article>
    entry_re = re.compile(
        r'<(?:div|article)[^>]*class="[^"]*(?:resource|pub|card|post|entry)[^"]*"[^>]*>'
        r'(.*?)</(?:div|article)>',
        re.DOTALL | re.IGNORECASE,
    )

    for entry_m in entry_re.finditer(text):
        entry = entry_m.group(1)

        link_m = re.search(
            r'<a\s[^>]*href=["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
            entry, re.DOTALL | re.IGNORECASE,
        )
        if not link_m:
            continue

        href = link_m.group(1).strip()
        title = _strip_html(link_m.group(2)).strip()
        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        if href.startswith("/"):
            href = base_url + href
        elif not href.startswith("http"):
            href = base_url + "/" + href.lstrip("/")

        plain = _strip_html(entry)
        published_at = _first_date(plain)

        # IIHS often includes a type tag
        type_m = re.search(
            r'class="[^"]*(?:type|category|tag)[^"]*"[^>]*>(.*?)</(?:span|div|a)>',
            entry, re.DOTALL | re.IGNORECASE,
        )
        type_text = _strip_html(type_m.group(1)) if type_m else ""
        ptype = _paper_type(title, type_text)

        # Abstract snippet
        abstract: str | None = None
        for p_m in re.finditer(r'<p[^>]*>(.*?)</p>', entry, re.DOTALL | re.IGNORECASE):
            candidate = _strip_html(p_m.group(1)).strip()
            if len(candidate) > 60 and candidate != title:
                abstract = candidate[:2000]
                break

        papers.append({
            "title": title,
            "url": href,
            "authors": [],
            "published_at": published_at,
            "abstract": abstract,
            "paper_type": ptype,
        })

    if not papers:
        papers = _generic_parse(text, base_url)

    return papers


# ---------------------------------------------------------------------------
# Generic fallback parser
# ---------------------------------------------------------------------------


def _generic_parse(text: str, base_url: str) -> list[dict]:
    """Generic HTML parser for unknown site structures.

    Scans the page for <a href="...">title</a> patterns near date-like
    strings. Filters out navigation, footer, and other non-publication links
    by requiring a minimum title length and excluding common skip terms.

    Args:
        text: Decoded HTML string.
        base_url: Base URL for resolving relative hrefs.

    Returns:
        List of partial paper dicts. May be empty.
    """
    papers: list[dict] = []
    seen_urls: set[str] = set()

    # Terms that indicate navigation/footer links, not publications
    _SKIP_RE = re.compile(
        r"\b(?:home|about|contact|login|register|search|sitemap|privacy|terms|"
        r"copyright|subscribe|newsletter|menu|navigation|facebook|twitter|linkedin)\b",
        re.IGNORECASE,
    )

    for m in _RE_ANCHOR.finditer(text):
        href = m.group(1).strip()
        title = _strip_html(m.group(2)).strip()

        # Must look like a real publication title
        if len(title) < 20:
            continue
        if _SKIP_RE.search(title):
            continue
        if href in seen_urls:
            continue

        # Skip anchors, js, mail links
        if href.startswith(("#", "javascript:", "mailto:")):
            continue

        seen_urls.add(href)

        if href.startswith("/"):
            href = base_url.rstrip("/") + href
        elif not href.startswith("http"):
            href = base_url.rstrip("/") + "/" + href.lstrip("/")

        # Look for a date in the 300 chars surrounding this match
        start = max(0, m.start() - 150)
        end = min(len(text), m.end() + 150)
        context_plain = _strip_html(text[start:end])
        published_at = _first_date(context_plain)

        papers.append({
            "title": title,
            "url": href,
            "authors": [],
            "published_at": published_at,
            "abstract": None,
            "paper_type": _paper_type(title),
        })

    return papers


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

_PARSER_MAP = {
    "ncaer": _parse_ncaer,
    "cpr": _parse_cpr,
    "icrier": _parse_icrier,
    "igidr": _parse_igidr,
    "apu": _parse_apu,
    "iihs": _parse_iihs,
}


class IndiaThinkTanksSensor(BaseSensor):
    """Collect publications from major Indian economics research institutions.

    Scrapes each institution's publications listing page and extracts paper
    metadata via HTML parsing. No APIs are available; all parsing is
    regex-based and deliberately defensive.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 0.3 requests per second (very polite for small sites).
    """

    name = "india_think_tanks"
    watch = "india"
    rate_limit = 0.3

    def _collect_one_tank(self, tank_id: str, meta: dict[str, str | None]) -> list[dict]:
        """Fetch and parse one institution's publications page.

        Applies the per-tank parser, then normalises each result into the
        standard BaseSensor paper dict format.

        Args:
            tank_id: Short identifier key (e.g. "ncaer", "cpr").
            meta: Dict with "name" and "publications_url" keys.

        Returns:
            List of normalised paper dicts. Empty on any error.
        """
        pub_url = meta.get("publications_url")
        if not pub_url:
            return []

        print(f"[india_think_tanks] fetching {tank_id}: {pub_url}", file=sys.stderr)

        try:
            html = self.fetch_url(pub_url, timeout=45)
        except Exception as exc:
            warnings.warn(f"[india_think_tanks] {tank_id}: fetch failed: {exc}")
            return []

        parser = _PARSER_MAP.get(tank_id)
        try:
            raw_items = parser(html) if parser else _generic_parse(
                html.decode("utf-8", errors="replace"), pub_url
            )
        except Exception as exc:
            warnings.warn(f"[india_think_tanks] {tank_id}: parse failed: {exc}")
            return []

        institution_name = str(meta.get("name", tank_id))
        papers: list[dict] = []

        for item in raw_items:
            title = (item.get("title") or "").strip()
            if not title:
                continue

            item_url = item.get("url") or pub_url
            source_id = f"{tank_id}:{_url_hash(item_url)}"

            jel_codes = _extract_jel_codes(
                f"{title} {item.get('abstract') or ''}"
            )

            papers.append({
                "title": title,
                "authors": item.get("authors") or [],
                "abstract": item.get("abstract"),
                "doi": None,
                "url": item_url,
                "published_at": item.get("published_at"),
                "paper_type": item.get("paper_type", "working_paper"),
                "jel_codes": jel_codes,
                "keywords": ["India", institution_name],
                "source_id": source_id,
                "source_url": item_url,
                "raw_metadata": {
                    "tank_id": tank_id,
                    "institution": institution_name,
                    "publications_url": pub_url,
                    "scraped_url": item_url,
                },
            })

        print(
            f"[india_think_tanks] {tank_id}: {len(papers)} papers extracted",
            file=sys.stderr,
        )
        return papers

    def collect(self) -> list[dict]:
        """Collect publications from all monitored Indian think tanks.

        Iterates through THINK_TANKS, fetching and parsing each institution's
        publications page. Failures on individual tanks are logged as warnings
        and do not abort collection for the remaining institutions.

        Returns:
            Combined list of paper dicts conforming to BaseSensor.collect()
            contract, deduplicated by source_id within this run.
        """
        all_papers: list[dict] = []
        seen_source_ids: set[str] = set()

        for tank_id, meta in THINK_TANKS.items():
            try:
                papers = self._collect_one_tank(tank_id, meta)
            except Exception as exc:
                warnings.warn(
                    f"[india_think_tanks] {tank_id}: unexpected error: {exc}"
                )
                papers = []

            for paper in papers:
                sid = paper.get("source_id", "")
                if sid and sid in seen_source_ids:
                    continue
                if sid:
                    seen_source_ids.add(sid)
                all_papers.append(paper)

        print(
            f"[india_think_tanks] total papers collected: {len(all_papers)}",
            file=sys.stderr,
        )
        return all_papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = IndiaThinkTanksSensor()
    sensor.run()
