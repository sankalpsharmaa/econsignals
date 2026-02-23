"""Reserve Bank of India (RBI) publications sensor for EconSignals.

Monitors RBI working papers, staff studies, and research publications.

Primary approach: RSS feeds for publications and press releases.
Fallback: HTML scraping of the Working Paper Series listing page.

Working papers are at:
    https://rbi.org.in/scripts/OccasionalPublications.aspx?head=Working+Paper+Series

RSS feeds:
    Publications: https://rbi.org.in/rss/rbi_publications.xml
    Press releases: https://www.rbi.org.in/rss/rbi_press.xml

RBI Bulletin articles:
    https://bulletin.rbi.org.in/

State is persisted under the "rbi" key in watches/india/state.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
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

_NS_ATOM = "http://www.w3.org/2005/Atom"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"

_DEFAULT_LOOKBACK_DAYS = 30  # RBI publishes infrequently; wider window
_MAX_SCRAPE_PAPERS = 20

_STATE_PATH = PROJ_ROOT / "watches" / "india" / "state.json"

# Regex patterns for HTML scraping the working papers listing
_RE_PDF_LINK = re.compile(r'href=["\']([^"\']+\.pdf)["\']', re.IGNORECASE)
_RE_PAPER_LINK = re.compile(
    r'href=["\']([^"\']*(?:PublicationReport|OccasionalPublications|Scripts/BS_View)[^"\']*)["\']',
    re.IGNORECASE,
)
_RE_JEL = re.compile(
    r"\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)",
    re.IGNORECASE,
)
_RE_JEL_CODE = re.compile(r"[A-Z][0-9]{1,2}")

# Patterns that identify research publications vs. press releases
# (We want to keep these; the filter below rejects noise)
_RESEARCH_KEYWORDS = re.compile(
    r"\b(working paper|staff study|occasional paper|research|bulletin article"
    r"|monetary economics|development|financial stability|inflation|growth"
    r"|banking|credit|fiscal|macr|micro|econom)\b",
    re.IGNORECASE,
)

# Patterns that clearly indicate non-research press releases to skip
_NOISE_PATTERNS = re.compile(
    r"\b(repo rate|crr|slr|laf|rbi appoints|rbi launches|payment system"
    r"|directive|circular|master direction|press release|appointment|result of auction"
    r"|liquidity|ombudsman|penalty|fine|cancellation|surrender of licence"
    r"|directions to|cease and desist)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/india/state.json, returning empty dict on failure.

    Returns:
        Full state dict. May be empty if file is absent or corrupt.
    """
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist state dict to watches/india/state.json.

    Uses a temp-file + rename to avoid truncating state on write errors.

    Args:
        state: Dict to serialize as JSON.
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
# XML / RSS helpers
# ---------------------------------------------------------------------------


def _text(element: ET.Element | None) -> str:
    """Safely extract and strip text content from an XML element."""
    if element is None:
        return ""
    return (element.text or "").strip()


def _find(parent: ET.Element, *tags: str) -> ET.Element | None:
    """Try a sequence of tag names and return the first match."""
    for tag in tags:
        el = parent.find(tag)
        if el is not None:
            return el
    return None


def _parse_rss_date(date_str: str) -> str:
    """Parse a date string in RFC 822 or ISO 8601 to ISO date (YYYY-MM-DD).

    Falls back to today's UTC date if the string cannot be parsed.

    Args:
        date_str: Raw date string from a feed element.

    Returns:
        ISO date string in YYYY-MM-DD format.

    Examples:
        >>> _parse_rss_date("Mon, 20 Jan 2025 12:00:00 +0000")
        '2025-01-20'
        >>> _parse_rss_date("2025-01-20T12:00:00Z")
        '2025-01-20'
    """
    if not date_str:
        return datetime.now(timezone.utc).date().isoformat()
    date_str = date_str.strip()
    try:
        return parsedate_to_datetime(date_str).date().isoformat()
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d %B %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(date_str[: len(fmt) + 5], fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    # Strip timezone suffix and retry
    clean = re.sub(r"[+-]\d{2}:\d{2}$", "", date_str).strip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean[: len(fmt)], fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    warnings.warn(f"rbi: could not parse date {date_str!r}, defaulting to today")
    return datetime.now(timezone.utc).date().isoformat()


def _strip_html(html: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace.

    Args:
        html: Raw HTML string.

    Returns:
        Plain text with collapsed whitespace.

    Examples:
        >>> _strip_html("<p>Hello &amp; <b>world</b></p>")
        'Hello & world'
    """
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
    """Return the first 12 hex chars of the SHA-256 of a URL string.

    Args:
        url: Any URL string.

    Returns:
        12-character hex digest.
    """
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL classification codes from a block of text.

    Args:
        text: Plain text or HTML string to search.

    Returns:
        Deduplicated list of JEL code strings (e.g. ['C1', 'D83']).

    Examples:
        >>> _extract_jel_codes("JEL: C1, D8, F21")
        ['C1', 'D8', 'F21']
    """
    codes: list[str] = []
    seen: set[str] = set()
    for match in _RE_JEL.finditer(text):
        segment = match.group(1)
        for code in _RE_JEL_CODE.findall(segment):
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _extract_authors(text: str) -> list[str]:
    """Attempt to extract author names from description text.

    RBI working papers typically list authors in the description as
    "by FirstName LastName" or at the start of the abstract as
    "FirstName LastName and FirstName LastName".

    Args:
        text: Plain-text description or abstract snippet.

    Returns:
        List of author name strings, or empty list if none found.

    Examples:
        >>> _extract_authors("This paper by John Smith examines...")
        ['John Smith']
    """
    authors: list[str] = []

    # Pattern 1: "by Author Name and Author Name"
    m = re.search(
        r"\bby\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}"
        r"(?:\s+and\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})*)",
        text,
    )
    if m:
        raw = m.group(1)
        parts = re.split(r"\s+and\s+", raw, flags=re.IGNORECASE)
        for part in parts:
            name = part.strip()
            if name and len(name) < 60:
                authors.append(name)
        if authors:
            return authors

    # Pattern 2: Leading "Firstname Lastname*" lines (footnote markers common in RBI)
    lines = text.split("\n")
    for line in lines[:4]:
        line = line.strip().rstrip("*†‡1234567890")
        if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$", line):
            authors.append(line)
    return authors


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------


def _parse_rss2_items(channel: ET.Element) -> list[dict]:
    """Extract items from an RSS 2.0 <channel> element.

    Args:
        channel: The <channel> XML element.

    Returns:
        List of raw item dicts with keys: title, link, description,
        pub_date, authors, keywords, guid.
    """
    raw_items = []
    for item in channel.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = _find(item, "description", f"{{{_NS_CONTENT}}}encoded")
        date_el = item.find("pubDate")
        guid_el = item.find("guid")
        author_el = _find(item, "author", f"{{{_NS_DC}}}creator")
        categories = [_text(c) for c in item.findall("category") if _text(c)]
        link = _text(link_el)
        guid = _text(guid_el)
        raw_items.append({
            "title": _text(title_el),
            "link": link,
            "description": _text(desc_el),
            "pub_date": _text(date_el),
            "authors": [_text(author_el)] if author_el is not None and _text(author_el) else [],
            "keywords": categories,
            "guid": guid or link,
        })
    return raw_items


def _parse_atom_entries(feed_el: ET.Element) -> list[dict]:
    """Extract entries from an Atom <feed> element.

    Args:
        feed_el: The root <feed> XML element.

    Returns:
        List of raw item dicts.
    """
    ns = _NS_ATOM
    raw_items = []
    for entry in feed_el.iter(f"{{{ns}}}entry"):
        title_el = entry.find(f"{{{ns}}}title")
        link = ""
        for link_el in entry.findall(f"{{{ns}}}link"):
            rel = link_el.get("rel", "alternate")
            href = link_el.get("href", "")
            if rel == "alternate" and href:
                link = href
                break
            if not link and href:
                link = href
        summary_el = _find(entry, f"{{{ns}}}summary", f"{{{ns}}}content")
        date_el = _find(entry, f"{{{ns}}}published", f"{{{ns}}}updated")
        authors = []
        for author_el in entry.findall(f"{{{ns}}}author"):
            name_el = author_el.find(f"{{{ns}}}name")
            if name_el is not None and _text(name_el):
                authors.append(_text(name_el))
        if not authors:
            dc_creator = entry.find(f"{{{_NS_DC}}}creator")
            if dc_creator is not None and _text(dc_creator):
                authors.append(_text(dc_creator))
        categories = []
        for cat in entry.findall(f"{{{ns}}}category"):
            term = cat.get("term", "").strip()
            if term:
                categories.append(term)
        id_el = entry.find(f"{{{ns}}}id")
        guid = _text(id_el) or link
        raw_items.append({
            "title": _text(title_el),
            "link": link,
            "description": _text(summary_el),
            "pub_date": _text(date_el),
            "authors": authors,
            "keywords": categories,
            "guid": guid,
        })
    return raw_items


def _parse_feed_bytes(xml_bytes: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom XML bytes into a list of raw item dicts.

    Args:
        xml_bytes: Raw XML response bytes.

    Returns:
        List of raw item dicts. Returns empty list on parse failure.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        warnings.warn(f"rbi: XML parse error: {exc}")
        return []

    root_tag = root.tag if "}" not in root.tag else root.tag.split("}")[1]
    root_tag = root_tag.lower()

    if root_tag == "feed":
        return _parse_atom_entries(root)

    channel = root.find("channel") or root
    return _parse_rss2_items(channel)


# ---------------------------------------------------------------------------
# Research relevance filtering
# ---------------------------------------------------------------------------


def _is_research_relevant(title: str, description: str) -> bool:
    """Determine whether an RSS item represents a research publication.

    Applies two-stage logic:
    1. Reject items whose title matches known non-research noise patterns.
    2. Accept items whose title or description contain research keywords.

    Args:
        title: Item title string.
        description: Plain-text item description.

    Returns:
        True if the item should be included in the output.

    Examples:
        >>> _is_research_relevant("Working Paper: Inflation Dynamics", "")
        True
        >>> _is_research_relevant("RBI increases repo rate by 25 bps", "")
        False
    """
    combined = f"{title} {description}"
    if _NOISE_PATTERNS.search(title):
        return False
    return bool(_RESEARCH_KEYWORDS.search(combined))


# ---------------------------------------------------------------------------
# HTML scraping of working papers listing
# ---------------------------------------------------------------------------


def _parse_working_papers_html(html: str, base_url: str) -> list[dict]:
    """Extract paper metadata from the RBI Working Paper Series listing page.

    The RBI listing page at OccasionalPublications.aspx contains table rows
    with paper titles, links, and sometimes author names. This parser applies
    regex patterns to extract those items defensively.

    Args:
        html: Raw HTML of the listing page.
        base_url: Base URL used to resolve relative href values.

    Returns:
        List of raw paper dicts with keys: title, url, description.
    """
    papers: list[dict] = []
    seen_urls: set[str] = set()

    # Look for table rows containing paper title links
    # RBI listing uses <td> cells with anchors
    row_pattern = re.compile(
        r"<tr[^>]*>(.*?)</tr>",
        re.DOTALL | re.IGNORECASE,
    )
    link_pattern = re.compile(
        r'<a\s[^>]*href=["\'](.*?)["\'][^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )

    for row_match in row_pattern.finditer(html):
        row_html = row_match.group(1)
        # Skip navigation / header rows (short or no meaningful link text)
        links_in_row = link_pattern.findall(row_html)
        if not links_in_row:
            continue

        for href, link_text in links_in_row:
            title = _strip_html(link_text).strip()
            if len(title) < 10:
                continue  # Skip short anchor texts (images, nav links)

            href = href.strip()
            # Resolve relative URLs
            if href.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = urljoin(base_url, href)
            else:
                url = urljoin(base_url, href)

            if url in seen_urls:
                continue

            # Only include links that look like paper pages or PDFs
            if not re.search(
                r"\.(pdf|aspx|htm|html)($|\?)",
                url,
                re.IGNORECASE,
            ):
                continue

            seen_urls.add(url)
            # Extract surrounding description text from the row
            desc = _strip_html(row_html).strip()
            # Remove the title itself from description to avoid duplication
            desc = desc.replace(title, "").strip()

            papers.append({
                "title": title,
                "url": url,
                "description": desc[:500],
            })

    return papers[:_MAX_SCRAPE_PAPERS]


# ---------------------------------------------------------------------------
# Item-to-paper-dict conversion
# ---------------------------------------------------------------------------


def _rss_item_to_paper(raw: dict, feed_key: str, cutoff: str) -> dict | None:
    """Convert a raw RSS/Atom item dict to the standard paper dict format.

    Skips items older than the cutoff date, noise press releases, and
    items without a meaningful title.

    Args:
        raw: Raw item dict from _parse_rss2_items or _parse_atom_entries.
        feed_key: Feed identifier string (e.g. "publications").
        cutoff: ISO date string; items on or before this date are skipped.

    Returns:
        Paper dict conforming to BaseSensor.collect() contract, or None.
    """
    title = raw.get("title", "").strip()
    if not title or len(title) < 5:
        return None

    description_raw = raw.get("description", "")
    description = _strip_html(description_raw)

    if not _is_research_relevant(title, description):
        return None

    pub_date = _parse_rss_date(raw.get("pub_date", ""))
    if pub_date <= cutoff:
        return None

    link = raw.get("link", "").strip()
    guid = raw.get("guid", "") or link
    source_id = f"rbi:{_url_hash(guid or title)}"

    # Authors: prefer explicit feed authors, then try to extract from description
    authors = [a for a in raw.get("authors", []) if a.strip()]
    if not authors and description:
        authors = _extract_authors(description)

    keywords_base = ["RBI", "India", "monetary policy", "central bank"]
    keywords = keywords_base + [k for k in raw.get("keywords", []) if k.strip()]

    jel_codes = _extract_jel_codes(description)

    # Determine paper type: publications feed items are more likely working papers
    paper_type = "working_paper" if "working" in title.lower() else "report"

    abstract = description[:2000] if description else None

    return {
        "title": title,
        "authors": authors if authors else [],
        "abstract": abstract,
        "doi": None,
        "url": link or None,
        "published_at": pub_date,
        "paper_type": paper_type,
        "jel_codes": jel_codes,
        "keywords": keywords,
        "source_id": source_id,
        "source_url": link or None,
        "raw_metadata": {
            "feed": feed_key,
            "guid": guid,
            "pub_date_raw": raw.get("pub_date"),
        },
    }


def _scraped_item_to_paper(raw: dict, pub_date: str) -> dict | None:
    """Convert a scraped working-paper dict to the standard paper dict format.

    Args:
        raw: Raw dict from _parse_working_papers_html with keys:
             title, url, description.
        pub_date: ISO date string to assign (approximation).

    Returns:
        Paper dict conforming to BaseSensor.collect() contract, or None.
    """
    title = raw.get("title", "").strip()
    if not title or len(title) < 10:
        return None

    url = raw.get("url", "").strip()
    description = raw.get("description", "").strip()

    source_id = f"rbi:{_url_hash(url or title)}"
    keywords = ["RBI", "India", "monetary policy", "central bank", "working paper"]
    jel_codes = _extract_jel_codes(description)
    authors = _extract_authors(description)

    abstract = description[:2000] if description else None

    return {
        "title": title,
        "authors": authors if authors else [],
        "abstract": abstract,
        "doi": None,
        "url": url or None,
        "published_at": pub_date,
        "paper_type": "working_paper",
        "jel_codes": jel_codes,
        "keywords": keywords,
        "source_id": source_id,
        "source_url": url or None,
        "raw_metadata": {
            "source": "html_scrape",
            "url": url,
        },
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class RBISensor(BaseSensor):
    """Sensor that collects RBI working papers and research publications.

    Primary approach: two RBI RSS feeds (publications, press releases).
    Fallback: HTML scraping of the Working Paper Series listing page.

    Only research-relevant items pass the relevance filter; routine press
    releases about rate decisions, circulars, and appointments are excluded.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category (india).
        rate_limit: Conservative rate for the RBI site (0.5 req/s).
    """

    name = "rbi"
    watch = "india"
    rate_limit = 0.5  # RBI site is slow; be gentle

    RSS_FEEDS: dict[str, str] = {
        "publications": "https://rbi.org.in/rss/rbi_publications.xml",
        "press": "https://www.rbi.org.in/rss/rbi_press.xml",
    }

    WORKING_PAPERS_URL = (
        "https://rbi.org.in/scripts/OccasionalPublications.aspx"
        "?head=Working+Paper+Series"
    )

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _cutoff_date(self) -> str:
        """Return the ISO date string to use as the lower-bound publication filter.

        Reads ``rbi.last_fetched`` from watch state. Defaults to 30 days ago.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = (state.get("rbi") or {}).get("last_fetched")
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
        """Record today's date as the last successful RBI fetch in state."""
        state = _load_state()
        rbi_state = state.get("rbi") or {}
        rbi_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state["rbi"] = rbi_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[rbi] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # RSS collection
    # ------------------------------------------------------------------

    def _collect_from_feeds(self, cutoff: str) -> list[dict]:
        """Fetch and parse all configured RSS feeds.

        Skips feeds that fail to download; never raises.

        Args:
            cutoff: ISO date string; items on or before this date are skipped.

        Returns:
            List of paper dicts that pass the relevance and date filters.
        """
        papers: list[dict] = []
        seen_ids: set[str] = set()

        for feed_key, feed_url in self.RSS_FEEDS.items():
            try:
                xml_bytes = self.fetch_url(feed_url, timeout=45)
            except Exception as exc:
                warnings.warn(f"[rbi] failed to fetch RSS feed {feed_key!r} ({feed_url}): {exc}")
                continue

            raw_items = _parse_feed_bytes(xml_bytes)
            print(
                f"[rbi] {feed_key}: {len(raw_items)} raw items from RSS",
                file=sys.stderr,
            )

            for raw in raw_items:
                paper = _rss_item_to_paper(raw, feed_key, cutoff)
                if paper is None:
                    continue
                sid = paper["source_id"]
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                papers.append(paper)

            print(
                f"[rbi] {feed_key}: {sum(1 for p in papers if p['raw_metadata'].get('feed') == feed_key)}"
                f" papers after filtering",
                file=sys.stderr,
            )

        return papers

    # ------------------------------------------------------------------
    # HTML scraping fallback
    # ------------------------------------------------------------------

    def _collect_from_scrape(self, cutoff: str) -> list[dict]:
        """Scrape the RBI Working Paper Series listing page for papers.

        Used when the RSS feeds yield no research-relevant items. The
        listing page is not paginated; it shows all working papers.
        Assigns an approximate publication date of today when none is
        available from the HTML.

        Args:
            cutoff: ISO date string (used as approximate lower bound
                    - individual paper dates are unavailable from the listing).

        Returns:
            List of paper dicts. Returns empty list on any error.
        """
        print(f"[rbi] scraping working papers listing: {self.WORKING_PAPERS_URL}", file=sys.stderr)
        try:
            html_bytes = self.fetch_url(self.WORKING_PAPERS_URL, timeout=60)
            html = html_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            warnings.warn(f"[rbi] failed to fetch working papers listing: {exc}")
            return []

        raw_papers = _parse_working_papers_html(html, self.WORKING_PAPERS_URL)
        print(f"[rbi] scraped {len(raw_papers)} raw items from listing page", file=sys.stderr)

        papers: list[dict] = []
        seen_ids: set[str] = set()
        # Use today as the publication date since the listing doesn't show dates
        approx_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for raw in raw_papers:
            paper = _scraped_item_to_paper(raw, approx_date)
            if paper is None:
                continue
            sid = paper["source_id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            papers.append(paper)

        return papers

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Collect RBI research publications.

        Steps:
        1. Determine the cutoff date from watch state (default: 30 days ago).
        2. Fetch and parse both RSS feeds; filter for research-relevant items.
        3. If no relevant items found in RSS, fall back to HTML scraping of
           the Working Paper Series listing page.
        4. Update watch state with today's date.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        cutoff = self._cutoff_date()
        print(
            f"[rbi] collecting publications on or after {cutoff}",
            file=sys.stderr,
        )

        # Primary: RSS feeds
        papers = self._collect_from_feeds(cutoff)
        print(f"[rbi] RSS feeds yielded {len(papers)} research-relevant items", file=sys.stderr)

        # Fallback: scrape working papers page if feeds gave nothing
        if not papers:
            warnings.warn(
                "[rbi] no research items from RSS feeds; falling back to HTML scraping"
            )
            try:
                papers = self._collect_from_scrape(cutoff)
            except Exception as exc:
                warnings.warn(f"[rbi] HTML scraping failed: {exc}")
                papers = []

        print(f"[rbi] collected {len(papers)} total papers", file=sys.stderr)
        self._update_state()
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = RBISensor()
    sensor.run()
