"""SSRN sensor for EconSignals.

Monitors SSRN economics research networks for new working papers.

SSRN does not expose a stable public API. This sensor tries three approaches
in order, stopping at the first that yields results:

1. SSRN Atom/RSS feeds for each configured economics research network
   (journal_id-based URLs).  These are machine-readable and fast.
2. SSRN search page scraping via their internal CFML search endpoint.
3. Network listing page scraping (HTML), extracting abstract_id links.

All approaches are defensive: any failure falls back to the next strategy,
or returns an empty list with a stderr warning. The sensor never raises out of
collect() so the wider scan is not interrupted.

State (last-seen abstract_ids and last-fetch timestamps) is stored under the
"ssrn" key in PROJ_ROOT/watches/papers/state.json, matching the convention
used by rss_feeds.py.
"""

from __future__ import annotations

import json
import re
import sys
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py convention)
# ---------------------------------------------------------------------------
SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
PROJ_ROOT = SKILL_ROOT.parent.parent.parent  # econsignals project root

from sensors._base import BaseSensor  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SSRN economics research networks mapped to their journal_id values.
# Each network has an Atom feed and an HTML listing page at the same base URL.
SSRN_NETWORKS: dict[str, int] = {
    "development_econ": 2613076,
    "urban_econ": 2613099,
    "labor_econ": 2613078,
    "public_econ": 2613091,
    "micro_econ": 2613081,
    "macro_econ": 2613080,
    "international_econ": 2613077,
    "health_econ": 2613075,
    "environmental_econ": 2613073,
    "political_economy": 2613090,
}

_SSRN_FEED_BASE = (
    "https://papers.ssrn.com/sol3/JELJOUR_Results.cfm"
    "?form_name=journalBrowse&journal_id={journal_id}&Network=no&stype=rss"
)
_SSRN_LISTING_BASE = (
    "https://papers.ssrn.com/sol3/JELJOUR_Results.cfm"
    "?form_name=journalBrowse&journal_id={journal_id}&Network=no"
)
_SSRN_ABSTRACT_URL = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id={abstract_id}"

# Paper link pattern on SSRN HTML pages
_ABSTRACT_ID_RE = re.compile(r"/sol3/papers\.cfm\?abstract_id=(\d+)", re.IGNORECASE)

# DOI pattern (SSRN sometimes embeds these)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)")

# XML namespaces used by SSRN Atom feeds
_NS_ATOM = "http://www.w3.org/2005/Atom"
_NS_DC = "http://purl.org/dc/elements/1.1/"

# Default lookback when no prior state exists
_DEFAULT_LOOKBACK_DAYS = 7

# State file shared with other paper sensors
_STATE_FILE = PROJ_ROOT / "watches" / "papers" / "state.json"

# Browser-like headers to reduce likelihood of 403 responses
_SSRN_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load the shared sensor state JSON file.

    Returns:
        Parsed state dict, or empty dict on missing or corrupt file.
    """
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    """Persist state to the JSON file, creating parent directories as needed.

    Args:
        state: Full state dict to serialize.
    """
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


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
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_date(raw: str) -> str:
    """Parse an RFC 822, ISO 8601, or SSRN-style date to YYYY-MM-DD.

    Falls back to today's UTC date when the string cannot be parsed.

    Args:
        raw: Raw date string.

    Returns:
        ISO date string (YYYY-MM-DD).
    """
    if not raw:
        return datetime.now(timezone.utc).date().isoformat()

    raw = raw.strip()

    # RFC 822 (RSS pubDate)
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        pass

    # ISO 8601 variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw[: len(fmt) + 5], fmt).date().isoformat()
        except (ValueError, TypeError):
            pass

    # Strip trailing timezone offset and retry
    clean = re.sub(r"[+-]\d{2}:\d{2}$", "", raw).strip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean[: len(fmt)], fmt).date().isoformat()
        except (ValueError, TypeError):
            pass

    # SSRN sometimes uses "Month DD, YYYY"
    try:
        return datetime.strptime(raw, "%B %d, %Y").date().isoformat()
    except (ValueError, TypeError):
        pass

    warnings.warn(f"ssrn: could not parse date {raw!r}, defaulting to today")
    return datetime.now(timezone.utc).date().isoformat()


def _extract_doi(text: str) -> str | None:
    """Extract the first DOI found in a text or URL string.

    Args:
        text: Any string potentially containing a DOI.

    Returns:
        Bare DOI string or None.
    """
    if not text:
        return None
    match = _DOI_RE.search(text)
    if match:
        return match.group(1).rstrip(".,;)")
    return None


def _text_of(el: ET.Element | None) -> str:
    """Safely extract stripped text from an XML element."""
    if el is None:
        return ""
    return (el.text or "").strip()


# ---------------------------------------------------------------------------
# Strategy 1: Atom/RSS feeds
# ---------------------------------------------------------------------------


def _parse_atom_feed(xml_bytes: bytes, network_name: str) -> list[dict]:
    """Parse an SSRN Atom/RSS feed into raw item dicts.

    SSRN feeds can be either Atom (``<feed>``) or RSS 2.0 (``<rss>``).

    Args:
        xml_bytes: Raw bytes from the feed URL.
        network_name: Human-readable network key (for warnings).

    Returns:
        List of raw dicts with keys: abstract_id, title, authors, abstract,
        pub_date_raw, doi, url.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        warnings.warn(f"ssrn: XML parse error for {network_name!r}: {exc}")
        return []

    tag = root.tag
    local = tag.split("}")[1].lower() if "}" in tag else tag.lower()

    if local == "feed":
        return _parse_ssrn_atom(root, network_name)
    else:
        # RSS 2.0
        channel = root.find("channel") or root
        return _parse_ssrn_rss2(channel, network_name)


def _parse_ssrn_atom(root: ET.Element, network_name: str) -> list[dict]:
    """Extract entries from an Atom ``<feed>`` element.

    Args:
        root: Root ``<feed>`` XML element.
        network_name: Network key for logging.

    Returns:
        List of raw item dicts.
    """
    ns = _NS_ATOM
    results: list[dict] = []

    for entry in root.iter(f"{{{ns}}}entry"):
        # Title
        title = _text_of(entry.find(f"{{{ns}}}title"))

        # Link: prefer rel=alternate
        link = ""
        for link_el in entry.findall(f"{{{ns}}}link"):
            rel = link_el.get("rel", "alternate")
            href = link_el.get("href", "")
            if rel == "alternate" and href:
                link = href
                break
            if not link and href:
                link = href

        # Abstract from summary or content
        summary_el = entry.find(f"{{{ns}}}summary") or entry.find(f"{{{ns}}}content")
        abstract = _strip_html(_text_of(summary_el))

        # Date: prefer published, fall back to updated
        date_el = entry.find(f"{{{ns}}}published") or entry.find(f"{{{ns}}}updated")
        pub_date_raw = _text_of(date_el)

        # Authors
        authors: list[str] = []
        for author_el in entry.findall(f"{{{ns}}}author"):
            name_el = author_el.find(f"{{{ns}}}name")
            name = _text_of(name_el)
            if name:
                authors.append(name)
        if not authors:
            dc = entry.find(f"{{{_NS_DC}}}creator")
            name = _text_of(dc)
            if name:
                authors.append(name)

        abstract_id = _extract_abstract_id(link)
        doi = _extract_doi(_text_of(entry.find(f"{{{ns}}}id")) or link)

        if abstract_id:
            results.append({
                "abstract_id": abstract_id,
                "title": title,
                "authors": authors,
                "abstract": abstract[:2000] if abstract else "",
                "pub_date_raw": pub_date_raw,
                "doi": doi,
                "url": _SSRN_ABSTRACT_URL.format(abstract_id=abstract_id),
            })

    return results


def _parse_ssrn_rss2(channel: ET.Element, network_name: str) -> list[dict]:
    """Extract items from an RSS 2.0 ``<channel>`` element.

    Args:
        channel: The ``<channel>`` XML element (or root if items are at top level).
        network_name: Network key for logging.

    Returns:
        List of raw item dicts.
    """
    results: list[dict] = []

    for item in channel.iter("item"):
        title = _text_of(item.find("title"))
        link = _text_of(item.find("link"))
        desc_el = item.find("description")
        abstract = _strip_html(_text_of(desc_el))
        pub_date_raw = _text_of(item.find("pubDate"))

        # Authors: dc:creator is the most reliable source in SSRN RSS
        author_els = item.findall(f"{{{_NS_DC}}}creator")
        authors = [_text_of(a) for a in author_els if _text_of(a)]
        if not authors:
            author_el = item.find("author")
            name = _text_of(author_el)
            if name:
                authors = [name]

        abstract_id = _extract_abstract_id(link)
        doi = _extract_doi(link)

        if abstract_id:
            results.append({
                "abstract_id": abstract_id,
                "title": title,
                "authors": authors,
                "abstract": abstract[:2000] if abstract else "",
                "pub_date_raw": pub_date_raw,
                "doi": doi,
                "url": _SSRN_ABSTRACT_URL.format(abstract_id=abstract_id),
            })

    return results


# ---------------------------------------------------------------------------
# Strategy 2: HTML listing page scraping
# ---------------------------------------------------------------------------


def _scrape_listing_page(html: str) -> list[dict]:
    """Scrape an SSRN network listing HTML page for paper links.

    This is a last-resort strategy. SSRN listing pages are heavily
    JavaScript-driven; only paper links present in the initial HTML are
    captured. Authors and abstracts are not reliably available here, so
    they are left empty for the caller to fill.

    The scraper looks for anchor tags whose href contains
    ``/sol3/papers.cfm?abstract_id=``, then tries to grab the surrounding
    text for title and author information.

    Args:
        html: Raw HTML string from the listing page.

    Returns:
        List of partial raw item dicts with at least abstract_id and title.
        Authors and abstract may be empty.
    """
    results: list[dict] = []
    seen: set[str] = set()

    # Find all anchor tags with an abstract_id
    anchor_re = re.compile(
        r'<a[^>]+href=["\']([^"\']*abstract_id=(\d+)[^"\']*)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in anchor_re.finditer(html):
        href, abstract_id, link_text = match.group(1), match.group(2), match.group(3)
        if abstract_id in seen:
            continue
        seen.add(abstract_id)

        title = _strip_html(link_text).strip()
        if not title or len(title) < 5:
            # Skip navigation links and other short anchors
            continue

        # Try to extract author names from the ~500 chars after the anchor
        end = match.end()
        surrounding = html[end : end + 500]
        authors = _extract_authors_from_context(surrounding)

        # Try to find a date in the surrounding text
        pub_date_raw = _extract_date_from_context(surrounding)

        results.append({
            "abstract_id": abstract_id,
            "title": title,
            "authors": authors,
            "abstract": "",
            "pub_date_raw": pub_date_raw,
            "doi": None,
            "url": _SSRN_ABSTRACT_URL.format(abstract_id=abstract_id),
        })

    return results


def _extract_abstract_id(url: str) -> str | None:
    """Pull the SSRN abstract_id integer string from a URL.

    Args:
        url: Any URL that may contain ``abstract_id=DIGITS``.

    Returns:
        The abstract_id string or None.
    """
    if not url:
        return None
    match = _ABSTRACT_ID_RE.search(url)
    return match.group(1) if match else None


def _extract_authors_from_context(html_snippet: str) -> list[str]:
    """Heuristically extract author names from HTML near a paper link.

    SSRN listing pages do not follow a strict template. This function
    strips HTML tags and splits on common author separators (, and ;) to
    produce a best-effort list.

    Args:
        html_snippet: Raw HTML text immediately following a paper title anchor.

    Returns:
        List of author name strings, possibly empty.
    """
    text = _strip_html(html_snippet)
    # SSRN typically renders "by Author A, Author B" near each paper
    by_match = re.search(r"\bby\s+(.+?)(?:\n|Posted|Date|Number|Pages|$)", text, re.IGNORECASE)
    if by_match:
        raw_authors = by_match.group(1)
        authors = [a.strip() for a in re.split(r"[,;]| and ", raw_authors) if a.strip()]
        # Discard obviously wrong tokens (e.g. very long strings)
        return [a for a in authors if len(a) < 80]

    return []


def _extract_date_from_context(html_snippet: str) -> str:
    """Heuristically extract a publication date from HTML near a paper link.

    Looks for patterns like "Date Written: Month DD, YYYY" or bare ISO dates.

    Args:
        html_snippet: Raw HTML text immediately following a paper title anchor.

    Returns:
        Raw date string (to be normalised by ``_parse_date``), or empty string.
    """
    text = _strip_html(html_snippet)

    # "Date Written: January 1, 2025"
    dw_match = re.search(r"Date Written:\s*([A-Za-z]+ \d{1,2},\s*\d{4})", text)
    if dw_match:
        return dw_match.group(1)

    # "Posted: 15 Jan 2025"
    posted_match = re.search(r"Posted:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text)
    if posted_match:
        return posted_match.group(1)

    # ISO date
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso_match:
        return iso_match.group(1)

    return ""


# ---------------------------------------------------------------------------
# Paper dict assembly
# ---------------------------------------------------------------------------


def _to_paper_dict(
    raw: dict,
    network_name: str,
    seen_ids: set[str],
    cutoff: str,
) -> dict | None:
    """Convert a raw SSRN item dict to the standard paper dict format.

    Skips items whose abstract_id has already been seen this session,
    whose publication date is on or before the cutoff, or whose title
    is empty.

    Args:
        raw: Dict with keys abstract_id, title, authors, abstract,
             pub_date_raw, doi, url.
        network_name: Source network key (used in raw_metadata).
        seen_ids: Set of abstract_ids already returned in this collect call;
                  updated in-place.
        cutoff: ISO date string; items on or before this date are skipped.

    Returns:
        Paper dict or None.
    """
    abstract_id = raw.get("abstract_id", "").strip()
    if not abstract_id:
        return None
    if abstract_id in seen_ids:
        return None

    title = raw.get("title", "").strip()
    if not title:
        return None

    pub_date = _parse_date(raw.get("pub_date_raw", ""))
    if pub_date <= cutoff:
        return None

    seen_ids.add(abstract_id)

    authors = [a for a in raw.get("authors", []) if a.strip()]

    return {
        "title": title,
        "authors": authors if authors else ["Unknown"],
        "abstract": raw.get("abstract") or None,
        "doi": raw.get("doi") or None,
        "url": raw.get("url") or _SSRN_ABSTRACT_URL.format(abstract_id=abstract_id),
        "published_at": pub_date,
        "paper_type": "working_paper",
        "jel_codes": [],
        "keywords": [],
        "source_id": abstract_id,
        "source_url": raw.get("url") or _SSRN_ABSTRACT_URL.format(abstract_id=abstract_id),
        "raw_metadata": {
            "network": network_name,
            "abstract_id": abstract_id,
            "pub_date_raw": raw.get("pub_date_raw"),
        },
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class SSRNSensor(BaseSensor):
    """Sensor that monitors SSRN economics research networks for new working papers.

    Tries three progressively more brittle access strategies for each network:

    1. Atom/RSS feed (reliable when available).
    2. HTML listing page scraping (fragile, JS-heavy pages may yield nothing).

    Any network that raises an HTTP 403 or CAPTCHA-like response is skipped
    gracefully. The sensor never raises out of ``collect()``.

    Attributes:
        name: Sensor identifier used in DB logs.
        watch: Watch category this sensor belongs to.
        rate_limit: Maximum requests per second (0.5 = one request every 2 s).
    """

    name = "ssrn"
    watch = "papers"
    rate_limit = 0.5  # Be polite: one request every 2 seconds

    def collect(self) -> list[dict]:
        """Fetch papers from all configured SSRN economics research networks.

        Skips networks that fail (403, CAPTCHA, timeout). Updates the shared
        state file with the latest fetch timestamps.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        state = _load_state()
        ssrn_state: dict = state.get("ssrn", {})
        last_fetch_map: dict[str, str] = ssrn_state.get("last_fetch", {})

        cutoff_default = (
            datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        ).date().isoformat()
        today_iso = datetime.now(timezone.utc).date().isoformat()

        results: list[dict] = []
        seen_ids: set[str] = set()
        new_last_fetch: dict[str, str] = dict(last_fetch_map)

        for network_name, journal_id in SSRN_NETWORKS.items():
            cutoff = last_fetch_map.get(network_name, cutoff_default)

            papers = self._collect_network(network_name, journal_id, cutoff, seen_ids)
            results.extend(papers)

            # Always advance timestamp to avoid re-scanning after partial success
            new_last_fetch[network_name] = today_iso

        # Persist updated state under the "ssrn" key
        ssrn_state["last_fetch"] = new_last_fetch
        state["ssrn"] = ssrn_state
        _save_state(state)

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _collect_network(
        self,
        network_name: str,
        journal_id: int,
        cutoff: str,
        seen_ids: set[str],
    ) -> list[dict]:
        """Collect papers for a single SSRN research network.

        Attempts the Atom/RSS feed first, then falls back to the HTML listing
        page. Returns an empty list if both approaches fail.

        Args:
            network_name: Human-readable network key.
            journal_id: SSRN journal_id integer.
            cutoff: ISO date string; older papers are skipped.
            seen_ids: Shared set of already-seen abstract_ids (mutated in-place).

        Returns:
            List of paper dicts for this network.
        """
        # Strategy 1: Atom/RSS feed
        raw_items = self._try_feed(network_name, journal_id)

        # Strategy 2: HTML listing page (fallback if feed returned nothing)
        if not raw_items:
            raw_items = self._try_listing(network_name, journal_id)

        papers: list[dict] = []
        for raw in raw_items:
            paper = _to_paper_dict(raw, network_name, seen_ids, cutoff)
            if paper is not None:
                papers.append(paper)

        return papers

    def _try_feed(self, network_name: str, journal_id: int) -> list[dict]:
        """Attempt to fetch and parse the SSRN Atom/RSS feed for a network.

        Returns an empty list on any error, with a stderr warning.

        Args:
            network_name: Human-readable network key.
            journal_id: SSRN journal_id integer.

        Returns:
            List of raw item dicts, possibly empty.
        """
        url = _SSRN_FEED_BASE.format(journal_id=journal_id)
        try:
            xml_bytes = self.fetch_url(url, headers=_SSRN_HEADERS)
        except HTTPError as exc:
            if exc.code in (403, 429, 503):
                warnings.warn(
                    f"ssrn: feed blocked (HTTP {exc.code}) for {network_name!r}, skipping"
                )
            else:
                warnings.warn(f"ssrn: feed HTTP {exc.code} for {network_name!r}: {exc}")
            return []
        except (URLError, TimeoutError, RuntimeError, OSError) as exc:
            warnings.warn(f"ssrn: feed fetch failed for {network_name!r}: {exc}")
            return []
        except Exception as exc:
            warnings.warn(f"ssrn: unexpected error fetching feed for {network_name!r}: {exc}")
            return []

        return _parse_atom_feed(xml_bytes, network_name)

    def _try_listing(self, network_name: str, journal_id: int) -> list[dict]:
        """Attempt to scrape the SSRN HTML listing page for a network.

        This is a best-effort fallback. SSRN pages are JavaScript-driven and
        may return minimal content in the initial HTML response.

        Returns an empty list on any error, with a stderr warning.

        Args:
            network_name: Human-readable network key.
            journal_id: SSRN journal_id integer.

        Returns:
            List of partial raw item dicts, possibly empty.
        """
        url = _SSRN_LISTING_BASE.format(journal_id=journal_id)
        try:
            html_bytes = self.fetch_url(url, headers=_SSRN_HEADERS, timeout=45)
        except HTTPError as exc:
            if exc.code in (403, 429, 503):
                warnings.warn(
                    f"ssrn: listing page blocked (HTTP {exc.code}) for {network_name!r}, skipping"
                )
            else:
                warnings.warn(
                    f"ssrn: listing page HTTP {exc.code} for {network_name!r}: {exc}"
                )
            return []
        except (URLError, TimeoutError, RuntimeError, OSError) as exc:
            warnings.warn(f"ssrn: listing page fetch failed for {network_name!r}: {exc}")
            return []
        except Exception as exc:
            warnings.warn(
                f"ssrn: unexpected error fetching listing for {network_name!r}: {exc}"
            )
            return []

        # SSRN may return a gzip-compressed body; fetch_url reads raw bytes.
        # Attempt UTF-8 decode with fallback to latin-1.
        try:
            html = html_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html = html_bytes.decode("latin-1", errors="replace")

        # Quick CAPTCHA / access-denied check
        if _is_blocked_response(html):
            warnings.warn(
                f"ssrn: access denied / CAPTCHA detected for {network_name!r} listing, skipping"
            )
            return []

        items = _scrape_listing_page(html)
        if not items:
            warnings.warn(
                f"ssrn: no papers found in listing page for {network_name!r} "
                f"(page may be JS-rendered)"
            )
        return items


def _is_blocked_response(html: str) -> bool:
    """Detect common SSRN CAPTCHA and access-denied page signatures.

    Args:
        html: Decoded HTML response body.

    Returns:
        True if the response looks like a block/CAPTCHA page.
    """
    lower = html.lower()
    indicators = (
        "captcha",
        "access denied",
        "you have been blocked",
        "cloudflare",
        "please verify",
        "robot",
        "403 forbidden",
    )
    return any(ind in lower for ind in indicators)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = SSRNSensor()
    sensor.run()
