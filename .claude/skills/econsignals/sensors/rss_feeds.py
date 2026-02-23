"""RSS/Atom feed sensor for EconSignals.

Monitors economics blogs and policy digests: VoxDev, VoxEU, NBER Digest,
Mostly Economics, Marginal Revolution, Dev Impact, Chris Blattman, and others.

Parses RSS 2.0 and Atom feeds using stdlib xml.etree.ElementTree only.
State (last-fetch timestamps per feed) is persisted in
PROJ_ROOT/watches/papers/state.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py convention)
# ---------------------------------------------------------------------------
SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
PROJ_ROOT = SKILL_ROOT.parent.parent.parent  # econsignals project root

from sensors._base import BaseSensor  # noqa: E402  (after sys.path tweak)

# ---------------------------------------------------------------------------
# Feed registry
# ---------------------------------------------------------------------------

FEEDS: dict[str, str] = {
    "voxdev": "https://voxdev.org/topic/feed",
    "voxeu": "https://cepr.org/rss/voxeu",
    "nber_digest": "https://www.nber.org/rss/digest",
    "nber_new": "https://www.nber.org/rss/new",
    "mostly_economics": "https://mostlyeconomics.wordpress.com/feed/",
    "marginal_revolution": "https://marginalrevolution.com/feed",
    "dev_impact": "https://blogs.worldbank.org/en/impactevaluations/feed",
    "chris_blattman": "https://chrisblattman.com/feed/",
}

# paper_type assigned per feed name prefix
_FEED_PAPER_TYPES: dict[str, str] = {
    "voxdev": "policy_brief",
    "voxeu": "policy_brief",
    "nber_digest": "working_paper",
    "nber_new": "working_paper",
    "mostly_economics": "report",
    "marginal_revolution": "report",
    "dev_impact": "report",
    "chris_blattman": "report",
}

# XML namespace constants
_NS_ATOM = "http://www.w3.org/2005/Atom"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"

# Default lookback when no prior state exists
_DEFAULT_LOOKBACK_DAYS = 7

# State file location
_STATE_FILE = PROJ_ROOT / "watches" / "papers" / "state.json"


# ---------------------------------------------------------------------------
# Helper functions
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
    # Remove script/style blocks entirely (content is noise)
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_rss_date(date_str: str) -> str:
    """Parse a date string in RFC 822, ISO 8601, or common blog formats to ISO date.

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

    # Attempt RFC 822 (most RSS 2.0 feeds)
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.date().isoformat()
    except Exception:
        pass

    # Attempt ISO 8601 variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str[:len(fmt) + 5], fmt)
            return dt.date().isoformat()
        except (ValueError, TypeError):
            pass

    # Attempt partial ISO - strip trailing timezone info and retry
    clean = re.sub(r"[+-]\d{2}:\d{2}$", "", date_str).strip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(clean[:len(fmt)], fmt)
            return dt.date().isoformat()
        except (ValueError, TypeError):
            pass

    warnings.warn(f"rss_feeds: could not parse date {date_str!r}, defaulting to today")
    return datetime.now(timezone.utc).date().isoformat()


def _extract_doi(text: str) -> str | None:
    """Try to extract a DOI from a URL or text snippet.

    Args:
        text: Any string that may contain a DOI.

    Returns:
        Bare DOI string (e.g. "10.1257/aer.20190123") or None.
    """
    if not text:
        return None
    match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", text)
    if match:
        doi = match.group(1).rstrip(".,;)")
        return doi
    return None


def _guid_to_source_id(feed_name: str, guid: str | None, url: str) -> str:
    """Produce a stable, collision-resistant source_id for a feed item.

    Format: ``{feed_name}:{hash}`` where hash is the first 12 hex chars of
    the SHA-256 of the guid (preferred) or the item URL.

    Args:
        feed_name: Key from the FEEDS dict.
        guid: Optional guid element text from the feed item.
        url: Item link URL (fallback if guid is absent).

    Returns:
        Namespaced source_id string.
    """
    raw = (guid or url or "").strip()
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{feed_name}:{digest}"


def _text(element: ET.Element | None) -> str:
    """Safely extract and strip text content from an XML element."""
    if element is None:
        return ""
    return (element.text or "").strip()


def _find(parent: ET.Element, *tags: str) -> ET.Element | None:
    """Try a sequence of tag names (with optional namespace) and return the first match."""
    for tag in tags:
        el = parent.find(tag)
        if el is not None:
            return el
    return None


def _parse_feed(xml_bytes: bytes, feed_name: str) -> list[dict]:
    """Parse RSS 2.0 or Atom XML into a list of raw item dicts.

    Handles:
    - RSS 2.0: ``<channel><item>`` structure
    - Atom: ``<feed><entry>`` structure with ``{http://www.w3.org/2005/Atom}`` namespace
    - dc:creator for author names
    - category/tag elements for keywords

    Args:
        xml_bytes: Raw XML response bytes.
        feed_name: Feed key from FEEDS (used for paper_type and source_id prefix).

    Returns:
        List of raw item dicts with keys: title, link, description, pub_date,
        authors, keywords, guid.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        warnings.warn(f"rss_feeds: XML parse error for {feed_name!r}: {exc}")
        return []

    items: list[dict] = []

    # Determine format by root tag
    root_tag = root.tag.lower() if "}" not in root.tag else root.tag.split("}")[1].lower()

    if root_tag == "feed":
        # Atom format
        items = _parse_atom(root, feed_name)
    else:
        # RSS 2.0 (root is <rss> or <rdf:RDF>); channel is a child
        channel = root.find("channel")
        if channel is None:
            # Some feeds put items directly under root (RDF)
            channel = root
        items = _parse_rss2(channel, feed_name)

    return items


def _parse_rss2(channel: ET.Element, feed_name: str) -> list[dict]:
    """Extract items from an RSS 2.0 <channel> element.

    Args:
        channel: The <channel> XML element.
        feed_name: Feed key, used for source_id generation.

    Returns:
        List of raw item dicts.
    """
    raw_items = []
    for item in channel.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = _find(item, "description", f"{{{_NS_CONTENT}}}encoded")
        date_el = item.find("pubDate")
        guid_el = item.find("guid")

        # Author: try <author>, then dc:creator
        author_el = _find(item, "author", f"{{{_NS_DC}}}creator")

        # Keywords from <category> elements
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


def _parse_atom(feed_el: ET.Element, feed_name: str) -> list[dict]:
    """Extract entries from an Atom <feed> element.

    Args:
        feed_el: The root <feed> XML element.
        feed_name: Feed key, used for source_id generation.

    Returns:
        List of raw item dicts.
    """
    ns = _NS_ATOM
    raw_items = []

    for entry in feed_el.iter(f"{{{ns}}}entry"):
        title_el = entry.find(f"{{{ns}}}title")
        # Atom link: <link href="..." rel="alternate"/> - prefer rel=alternate
        link = ""
        for link_el in entry.findall(f"{{{ns}}}link"):
            rel = link_el.get("rel", "alternate")
            href = link_el.get("href", "")
            if rel == "alternate" and href:
                link = href
                break
            if not link and href:
                link = href

        # Summary / content
        summary_el = _find(entry, f"{{{ns}}}summary", f"{{{ns}}}content")
        # Published date: prefer <published>, fall back to <updated>
        date_el = _find(entry, f"{{{ns}}}published", f"{{{ns}}}updated")

        # Author names
        authors = []
        for author_el in entry.findall(f"{{{ns}}}author"):
            name_el = author_el.find(f"{{{ns}}}name")
            if name_el is not None and _text(name_el):
                authors.append(_text(name_el))

        # dc:creator fallback
        if not authors:
            dc_creator = entry.find(f"{{{_NS_DC}}}creator")
            if dc_creator is not None and _text(dc_creator):
                authors.append(_text(dc_creator))

        # Keywords from <category term="...">
        categories = []
        for cat in entry.findall(f"{{{ns}}}category"):
            term = cat.get("term", "").strip()
            if term:
                categories.append(term)

        # id element as guid
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


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load the sensor state JSON file, returning an empty dict on missing/corrupt file.

    Returns:
        Dict with at least an "rss_last_fetch" sub-dict mapping feed names to
        ISO date strings.
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
# Sensor
# ---------------------------------------------------------------------------


class RSSFeedsSensor(BaseSensor):
    """Sensor that collects items from RSS 2.0 and Atom economics feeds.

    Supported feeds: VoxDev, VoxEU, NBER Digest, NBER New Papers,
    Mostly Economics, Marginal Revolution, Dev Impact (World Bank),
    and Chris Blattman's blog.

    Attributes:
        name: Sensor identifier used in DB logs.
        watch: Watch category this sensor belongs to.
        rate_limit: Maximum requests per second across all feed fetches.
    """

    name = "rss_feeds"
    watch = "papers"
    rate_limit = 2.0  # polite; these are blogs, not APIs

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Fetch all configured RSS/Atom feeds and return new items as paper dicts.

        Skips items older than the last successful fetch for each feed (defaults
        to 7 days on first run). Updates state after processing.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        state = _load_state()
        last_fetch_map: dict[str, str] = state.get("rss_last_fetch", {})
        cutoff_default = (
            datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        ).date().isoformat()

        results: list[dict] = []
        new_last_fetch: dict[str, str] = dict(last_fetch_map)
        today_iso = datetime.now(timezone.utc).date().isoformat()

        for feed_name, feed_url in FEEDS.items():
            cutoff = last_fetch_map.get(feed_name, cutoff_default)
            try:
                xml_bytes = self.fetch_url(feed_url)
            except Exception as exc:
                warnings.warn(f"rss_feeds: failed to fetch {feed_name!r} ({feed_url}): {exc}")
                continue

            raw_items = _parse_feed(xml_bytes, feed_name)
            for raw in raw_items:
                parsed = self._to_paper_dict(raw, feed_name, cutoff)
                if parsed is not None:
                    results.append(parsed)

            # Advance the stored timestamp to today regardless of item count
            new_last_fetch[feed_name] = today_iso

        # Persist updated state
        state["rss_last_fetch"] = new_last_fetch
        _save_state(state)

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _to_paper_dict(
        self,
        raw: dict,
        feed_name: str,
        cutoff: str,
    ) -> dict | None:
        """Convert a raw feed item dict to the standard paper dict format.

        Skips the item if its publication date is on or before the cutoff date,
        or if the title is empty.

        Args:
            raw: Raw item dict from _parse_rss2 or _parse_atom.
            feed_name: Feed key from FEEDS.
            cutoff: ISO date string (YYYY-MM-DD); items on this date or older
                    are skipped.

        Returns:
            Paper dict or None if the item should be skipped.
        """
        title = raw.get("title", "").strip()
        if not title:
            return None

        pub_date = _parse_rss_date(raw.get("pub_date", ""))

        # Skip items that predate our last-fetch cutoff
        if pub_date <= cutoff:
            return None

        link = raw.get("link", "").strip()
        description = raw.get("description", "")
        abstract = _strip_html(description)[:2000]

        authors = [a for a in raw.get("authors", []) if a.strip()]
        if not authors:
            authors = ["Unknown"]

        keywords = [k for k in raw.get("keywords", []) if k.strip()]
        guid = raw.get("guid", "") or link
        source_id = _guid_to_source_id(feed_name, guid, link)

        paper_type = _FEED_PAPER_TYPES.get(feed_name, "report")

        # Attempt DOI extraction from link and description
        doi = _extract_doi(link) or _extract_doi(description)

        return {
            "title": title,
            "authors": authors,
            "abstract": abstract if abstract else None,
            "doi": doi,
            "url": link,
            "published_at": pub_date,
            "paper_type": paper_type,
            "jel_codes": [],
            "keywords": keywords,
            "source_id": source_id,
            "source_url": link,
            "raw_metadata": {
                "feed": feed_name,
                "guid": guid,
                "pub_date_raw": raw.get("pub_date"),
            },
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from lib.db import init_db

    init_db()
    sensor = RSSFeedsSensor()
    sensor.run()
