"""Twitter/X bridge sensor for EconSignals via Nitter RSS.

Best-effort access to economics Twitter using public Nitter instances.
Nitter instances are unreliable and frequently go down; this sensor is
explicitly designed to fail gracefully and never block the scan pipeline.

Approach:
  1. Probe configured Nitter instances and use the first that responds.
  2. Fetch RSS feeds for tracked authors with twitter_handle set.
  3. Search for #EconTwitter and #DevEcon via the Nitter search RSS endpoint.
  4. Parse RSS items into social_item dicts and insert into the DB.

All URLs stored in the DB are canonical twitter.com URLs, not Nitter URLs.

Usage:
    python sensors/twitter_bridge.py
"""

from __future__ import annotations

import json
import re
import sys
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT, _SSL_CTX  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NITTER_INSTANCES: list[str] = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
]

# Hashtag searches to run via Nitter's search RSS endpoint
SEARCH_HASHTAGS: list[str] = [
    "#EconTwitter",
    "#DevEcon",
]

# Nitter probe timeout: short because instances frequently hang
_PROBE_TIMEOUT = 10

# RSS fetch timeout: slightly more generous than probe
_FETCH_TIMEOUT = 15

# XML namespace constants (same as rss_feeds.py)
_NS_ATOM = "http://www.w3.org/2005/Atom"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"

# DOI patterns
_RE_DOI = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s\"'<>]+)",
    re.IGNORECASE,
)
_RE_DOI_BARE = re.compile(r"(?<![/\w])(10\.\d{4,9}/[^\s\"'<>]{4,})")

# Nitter item link pattern: {instance}/{handle}/status/{id}
# Used to extract handle and tweet ID from the RSS <link> element.
_RE_NITTER_STATUS = re.compile(
    r"https?://[^/]+/([^/]+)/status/(\d+)",
    re.IGNORECASE,
)

# Twitter.com canonical URL template
_TWITTER_URL = "https://twitter.com/{handle}/status/{tweet_id}"


# ---------------------------------------------------------------------------
# RSS helpers (adapted from rss_feeds.py)
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace.

    Args:
        html: Raw HTML string.

    Returns:
        Plain text with collapsed whitespace.
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


def _text(element: ET.Element | None) -> str:
    """Safely extract stripped text from an XML element."""
    if element is None:
        return ""
    return (element.text or "").strip()


def _parse_rss_date(date_str: str) -> str | None:
    """Parse RFC 822 or ISO 8601 date string to ISO 8601 datetime string.

    Returns ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SSZ) or None if unparseable.

    Args:
        date_str: Raw date string from an RSS element.

    Returns:
        ISO datetime string or None.
    """
    if not date_str:
        return None
    date_str = date_str.strip()

    # RFC 822 (standard for RSS 2.0)
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass

    # ISO 8601 variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str[: len(fmt) + 5], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            pass

    warnings.warn(f"twitter_bridge: could not parse date {date_str!r}")
    return None


def _extract_doi(text: str) -> str | None:
    """Extract the first DOI from a text string.

    Checks doi.org URLs, doi: prefixes, and bare DOIs starting with 10.

    Args:
        text: Raw text that may contain a DOI.

    Returns:
        Bare DOI string or None.
    """
    if not text:
        return None
    m = _RE_DOI.search(text)
    if m:
        return m.group(1).rstrip(".,;)")
    m = _RE_DOI_BARE.search(text)
    if m:
        return m.group(1).rstrip(".,;)")
    return None


def _nitter_link_to_twitter(nitter_link: str) -> tuple[str | None, str | None, str | None]:
    """Convert a Nitter status URL to a canonical twitter.com URL.

    Also extracts the handle and tweet ID.

    Args:
        nitter_link: URL from a Nitter RSS <link> element, e.g.
                     https://nitter.privacydev.net/EconTwit/status/12345

    Returns:
        Tuple of (twitter_url, handle, tweet_id). All three are None if the
        URL does not match the expected pattern.

    Examples:
        >>> _nitter_link_to_twitter("https://nitter.privacydev.net/JohnDoe/status/123")
        ('https://twitter.com/JohnDoe/status/123', 'JohnDoe', '123')
    """
    m = _RE_NITTER_STATUS.match(nitter_link)
    if not m:
        return None, None, None
    handle = m.group(1)
    tweet_id = m.group(2)
    twitter_url = _TWITTER_URL.format(handle=handle, tweet_id=tweet_id)
    return twitter_url, handle, tweet_id


def _parse_nitter_rss(xml_bytes: bytes) -> list[dict]:
    """Parse a Nitter RSS feed into a list of raw item dicts.

    Nitter emits RSS 2.0 with a <channel><item> structure. Each item has:
      <title>   - "{handle}: {tweet text}" or just tweet text
      <link>    - Nitter status URL
      <pubDate> - RFC 822 timestamp
      <description> - HTML-encoded tweet content (with media links etc.)
      <guid>    - same as <link>

    Args:
        xml_bytes: Raw RSS response bytes from a Nitter instance.

    Returns:
        List of raw item dicts with keys: link, content, pub_date.
        Returns empty list on parse error.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        warnings.warn(f"twitter_bridge: XML parse error: {exc}")
        return []

    channel = root.find("channel")
    if channel is None:
        channel = root

    items: list[dict] = []
    for item in channel.iter("item"):
        link = _text(item.find("link"))

        # Nitter description contains the tweet text as HTML
        desc_el = item.find("description")
        raw_html = _text(desc_el)
        content = _strip_html(raw_html)

        # Fall back to title if description is empty
        if not content:
            title_el = item.find("title")
            raw_title = _text(title_el)
            # Nitter prefixes with "Handle: " — strip it if present
            content = raw_title.split(": ", 1)[-1] if ": " in raw_title else raw_title

        pub_date = _text(item.find("pubDate"))

        if link and content:
            items.append({"link": link, "content": content, "pub_date": pub_date})

    return items


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class TwitterBridgeSensor(BaseSensor):
    """Collect economics-related tweets via Nitter RSS bridges.

    Uses public Nitter instances (alternative Twitter frontends that expose
    RSS feeds). Instances are frequently unreliable; the sensor probes each
    in order and uses the first that responds within a short timeout.

    Fetches:
      - RSS feeds for tracked authors with twitter_handle set in the DB.
      - Hashtag search feeds for #EconTwitter and #DevEcon.

    Overrides run() to insert social_items directly (same pattern as bluesky.py).

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 0.5 requests per second; Nitter instances are shared
                    public resources and should be treated politely.
    """

    name = "twitter_bridge"
    watch = "social"
    rate_limit = 0.5

    # ------------------------------------------------------------------
    # Instance discovery
    # ------------------------------------------------------------------

    def _probe_instance(self, base_url: str) -> bool:
        """Check whether a Nitter instance is reachable.

        Fetches the instance root with a short timeout. Does not use
        BaseSensor.fetch_url() because we want a custom timeout and we
        must not trigger the rate limiter or circuit breaker here.

        Args:
            base_url: Base URL of the Nitter instance (no trailing slash).

        Returns:
            True if the instance responded with HTTP 200, False otherwise.
        """
        from urllib.request import Request, urlopen

        url = f"{base_url}/"
        headers = {
            "User-Agent": (
                "EconSignals/1.0 (Nitter RSS bridge; "
                "https://github.com/zedeus/nitter)"
            )
        }
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=_PROBE_TIMEOUT, context=_SSL_CTX) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _find_working_instance(self) -> str | None:
        """Return the first Nitter instance that responds, or None.

        Probes NITTER_INSTANCES in order. Logs each attempt to stderr.

        Returns:
            Base URL string of the first working instance, or None if all fail.
        """
        for instance in NITTER_INSTANCES:
            print(
                f"[twitter_bridge] probing {instance} ...",
                file=sys.stderr,
            )
            if self._probe_instance(instance):
                print(
                    f"[twitter_bridge] using instance: {instance}",
                    file=sys.stderr,
                )
                return instance
            print(
                f"[twitter_bridge] instance unavailable: {instance}",
                file=sys.stderr,
            )
        return None

    # ------------------------------------------------------------------
    # Feed fetching
    # ------------------------------------------------------------------

    def _fetch_rss(self, url: str) -> list[dict]:
        """Fetch a Nitter RSS URL and parse it into raw item dicts.

        Uses a short timeout suitable for Nitter's often-slow responses.
        Returns an empty list on any error rather than raising.

        Args:
            url: Full RSS feed URL.

        Returns:
            List of raw item dicts from _parse_nitter_rss(), or empty list.
        """
        try:
            xml_bytes = self.fetch_url(url, timeout=_FETCH_TIMEOUT)
            return _parse_nitter_rss(xml_bytes)
        except HTTPError as exc:
            print(
                f"[twitter_bridge] HTTP {exc.code} fetching {url}",
                file=sys.stderr,
            )
            return []
        except (URLError, TimeoutError, OSError) as exc:
            print(
                f"[twitter_bridge] network error fetching {url}: {exc}",
                file=sys.stderr,
            )
            return []
        except Exception as exc:
            print(
                f"[twitter_bridge] unexpected error fetching {url}: {exc}",
                file=sys.stderr,
            )
            return []

    # ------------------------------------------------------------------
    # Paper matching
    # ------------------------------------------------------------------

    def _match_paper(self, doi: str | None) -> int | None:
        """Look up a paper in the DB by DOI and return its id.

        Args:
            doi: Bare DOI string, or None to skip lookup.

        Returns:
            Paper id integer if found, otherwise None.
        """
        if not doi:
            return None
        try:
            from lib.db import find_paper_by_doi

            doi = re.sub(
                r"^https?://(?:dx\.)?doi\.org/",
                "",
                doi,
                flags=re.IGNORECASE,
            )
            paper = find_paper_by_doi(doi)
            if paper:
                return paper["id"]
        except Exception as exc:
            print(
                f"[twitter_bridge] paper lookup error (doi={doi}): {exc}",
                file=sys.stderr,
            )
        return None

    # ------------------------------------------------------------------
    # Item conversion
    # ------------------------------------------------------------------

    def _raw_to_social_item(self, raw: dict) -> dict | None:
        """Convert a raw Nitter RSS item dict to a social_item dict.

        Converts the Nitter status URL to a canonical twitter.com URL,
        extracts the author handle and tweet ID for the source_id, and
        attempts to match any DOI found in the tweet text to a paper.

        Args:
            raw: Dict with keys: link (Nitter URL), content (tweet text),
                 pub_date (RFC 822 string).

        Returns:
            social_item dict, or None if the item cannot be parsed (missing
            link or content, or URL does not match expected pattern).
        """
        nitter_link: str = raw.get("link", "").strip()
        content: str = raw.get("content", "").strip()

        if not nitter_link or not content:
            return None

        twitter_url, handle, tweet_id = _nitter_link_to_twitter(nitter_link)
        if not twitter_url or not handle or not tweet_id:
            print(
                f"[twitter_bridge] skipping unrecognised link: {nitter_link}",
                file=sys.stderr,
            )
            return None

        published_at = _parse_rss_date(raw.get("pub_date", ""))

        doi = _extract_doi(content)
        paper_id = self._match_paper(doi)

        return {
            "source": "twitter",
            # Stable identifier: source + tweet ID (handles are mutable)
            "source_id": f"twitter:{tweet_id}",
            "author_handle": f"@{handle}",
            "content": content[:2000],
            "url": twitter_url,
            "paper_id": paper_id,
            "engagement_score": 0,  # Not available via Nitter RSS
            "published_at": published_at,
        }

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def _collect_author_feeds(self, instance: str) -> list[dict]:
        """Fetch RSS feeds for tracked authors with twitter_handle set.

        Queries the authors table for rows where is_tracked=1 and
        twitter_handle is not null.

        Args:
            instance: Base URL of the working Nitter instance.

        Returns:
            List of social_item dicts.
        """
        try:
            from lib.db import get_tracked_authors

            tracked = get_tracked_authors()
        except Exception as exc:
            print(
                f"[twitter_bridge] could not load tracked authors: {exc}",
                file=sys.stderr,
            )
            return []

        authors_with_handles = [
            a
            for a in tracked
            if a.get("twitter_handle") and a["twitter_handle"].strip()
        ]

        if not authors_with_handles:
            print(
                "[twitter_bridge] no tracked authors with Twitter handles",
                file=sys.stderr,
            )
            return []

        seen: set[str] = set()
        items: list[dict] = []

        for author in authors_with_handles:
            handle = author["twitter_handle"].strip().lstrip("@")
            rss_url = f"{instance}/{handle}/rss"

            raw_items = self._fetch_rss(rss_url)
            print(
                f"[twitter_bridge] author @{handle}: {len(raw_items)} items",
                file=sys.stderr,
            )

            for raw in raw_items:
                item = self._raw_to_social_item(raw)
                if item and item["source_id"] not in seen:
                    seen.add(item["source_id"])
                    items.append(item)

        return items

    def _collect_hashtag_feeds(self, instance: str) -> list[dict]:
        """Fetch RSS feeds for configured hashtag searches.

        Uses Nitter's search RSS endpoint:
        ``{instance}/search/rss?f=tweets&q={encoded_hashtag}``

        Args:
            instance: Base URL of the working Nitter instance.

        Returns:
            List of social_item dicts.
        """
        seen: set[str] = set()
        items: list[dict] = []

        for hashtag in SEARCH_HASHTAGS:
            encoded = quote_plus(hashtag)
            rss_url = f"{instance}/search/rss?f=tweets&q={encoded}"

            raw_items = self._fetch_rss(rss_url)
            print(
                f"[twitter_bridge] hashtag {hashtag!r}: {len(raw_items)} items",
                file=sys.stderr,
            )

            for raw in raw_items:
                item = self._raw_to_social_item(raw)
                if item and item["source_id"] not in seen:
                    seen.add(item["source_id"])
                    items.append(item)

        return items

    def collect(self) -> list[dict]:
        """Collect economics tweets via Nitter RSS.

        Steps:
        1. Find a working Nitter instance (probe in order, first wins).
        2. Fetch RSS feeds for tracked authors with twitter_handle set.
        3. Fetch RSS search feeds for configured hashtags.
        4. Merge and deduplicate by source_id.

        If no Nitter instance is reachable, returns an empty list with a
        warning logged to stderr. This never raises.

        Returns:
            List of social_item dicts ready for insert_social_item().
        """
        instance = self._find_working_instance()
        if instance is None:
            print(
                "[twitter_bridge] all Nitter instances unreachable; skipping",
                file=sys.stderr,
            )
            return []

        author_items = self._collect_author_feeds(instance)
        hashtag_items = self._collect_hashtag_feeds(instance)

        # Merge, deduplicating by source_id
        seen: set[str] = {item["source_id"] for item in author_items}
        merged: list[dict] = list(author_items)

        for item in hashtag_items:
            if item["source_id"] not in seen:
                seen.add(item["source_id"])
                merged.append(item)

        print(
            f"[twitter_bridge] collected {len(merged)} unique items",
            file=sys.stderr,
        )
        return merged

    # ------------------------------------------------------------------
    # Run override (social items bypass paper dedup pipeline)
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute the sensor: collect tweets, insert as social_items, log run.

        Overrides BaseSensor.run() because social items bypass the paper
        deduplication pipeline and are inserted directly via insert_social_item().

        Returns:
            Stats dict with keys: sensor, watch, status, found, new, errors,
            and optionally error_message on failure.
        """
        from lib.db import log_sensor_start, log_sensor_end, insert_social_item

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                try:
                    result = insert_social_item(item)
                    if result > 0:
                        self.stats["new"] = int(self.stats["new"]) + 1
                except Exception as exc:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(
                        f"[twitter_bridge] insert error: {exc}",
                        file=sys.stderr,
                    )

            log_sensor_end(
                run_id,
                "success",
                int(self.stats["found"]),
                int(self.stats["new"]),
            )
        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"[twitter_bridge] sensor failed: {exc}", file=sys.stderr)

        result = {
            "sensor": self.name,
            "watch": self.watch,
            "status": "error" if "error_message" in self.stats else "success",
            **self.stats,
        }
        print(json.dumps(result))
        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = TwitterBridgeSensor()
    sensor.run()
