"""Mastodon sensor for EconSignals.

Monitors econtwitter.net and other Mastodon instances for economics discussion
using public timeline and hashtag endpoints (no authentication required).

Instance API: https://econtwitter.net/api/v1/timelines/public?limit=40
Hashtag API:  https://econtwitter.net/api/v1/timelines/tag/{hashtag}?limit=40
Rate limit:   300 req/5min per instance (~1 req/s sustained).

Usage:
    python sensors/mastodon.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMELINE_LIMIT = 40

# DOI patterns mirroring the bluesky sensor
_RE_DOI = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s\"'<>]+)",
    re.IGNORECASE,
)
_RE_DOI_BARE = re.compile(r"(?<![/\w])(10\.\d{4,9}/[^\s\"'<>]{4,})")


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace.

    Mastodon content fields contain HTML (e.g. ``<p>``, ``<a>``, ``<br />``).
    This converts them to plain text without introducing third-party dependencies.

    Args:
        html: Raw HTML string from a Mastodon status ``content`` field.

    Returns:
        Plain text with collapsed whitespace.

    Examples:
        >>> _strip_html("<p>Hello &amp; <b>world</b></p>")
        'Hello & world'
    """
    if not html:
        return ""
    # Remove script/style blocks first
    html = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Replace <br> variants with a space
    html = re.sub(r"<br\s*/?>", " ", html, flags=re.IGNORECASE)
    # Replace block-level close tags with a space
    html = re.sub(r"</(p|div|li|blockquote)>", " ", html, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class MastodonSensor(BaseSensor):
    """Collect economics-related posts from Mastodon instances.

    Uses the public Mastodon API v1 — no authentication needed.
    Fetches the public timeline and per-hashtag timelines for each
    configured instance, then deduplicates by status URL.

    Posts are inserted as social_items via the overridden run() method,
    bypassing the paper dedup pipeline.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 1 request per second (well within the 300/5min cap).
        INSTANCES: Mastodon instances to query.
        HASHTAGS: Hashtags to fetch timelines for on each instance.
    """

    name = "mastodon"
    watch = "social"
    rate_limit = 1.0

    INSTANCES: list[str] = ["econtwitter.net"]
    HASHTAGS: list[str] = ["econtwitter", "economics", "econsky", "devecon"]

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def _fetch_timeline(self, instance: str) -> list[dict]:
        """Fetch the public timeline for a Mastodon instance.

        Args:
            instance: Hostname of the Mastodon instance (e.g. "econtwitter.net").

        Returns:
            List of raw status dicts, or empty list on any error.
        """
        url = f"https://{instance}/api/v1/timelines/public?limit={_TIMELINE_LIMIT}"
        try:
            data = self.fetch_json(url)
        except Exception as exc:
            print(
                f"[mastodon] failed to fetch public timeline from {instance}: {exc}",
                file=sys.stderr,
            )
            return []
        if not isinstance(data, list):
            return []
        return data

    def _fetch_hashtag(self, instance: str, hashtag: str) -> list[dict]:
        """Fetch the hashtag timeline for a single hashtag on an instance.

        Args:
            instance: Hostname of the Mastodon instance.
            hashtag: Hashtag string without the leading ``#``.

        Returns:
            List of raw status dicts, or empty list on any error.
        """
        url = (
            f"https://{instance}/api/v1/timelines/tag/{hashtag}"
            f"?limit={_TIMELINE_LIMIT}"
        )
        try:
            data = self.fetch_json(url)
        except Exception as exc:
            print(
                f"[mastodon] failed to fetch #{hashtag} from {instance}: {exc}",
                file=sys.stderr,
            )
            return []
        if not isinstance(data, list):
            return []
        return data

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _extract_doi(self, text: str) -> str | None:
        """Find the first DOI in post text.

        Checks for doi.org URLs, doi: prefixes, and bare 10.XXXX/ patterns.

        Args:
            text: Plain text of a Mastodon post.

        Returns:
            DOI string without URL prefix, or None if not found.
        """
        m = _RE_DOI.search(text)
        if m:
            return m.group(1).rstrip(".,;)")
        m = _RE_DOI_BARE.search(text)
        if m:
            return m.group(1).rstrip(".,;)")
        return None

    def _match_paper(self, doi: str | None) -> int | None:
        """Return the DB paper id for a DOI, or None if not found.

        Args:
            doi: DOI string, or None to skip lookup.

        Returns:
            Paper id integer if found, otherwise None.
        """
        if not doi:
            return None
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
        try:
            from lib.db import find_paper_by_doi

            paper = find_paper_by_doi(doi)
            if paper:
                return paper["id"]
        except Exception as exc:
            print(
                f"[mastodon] paper lookup error for doi={doi}: {exc}",
                file=sys.stderr,
            )
        return None

    def _parse_status(self, status: dict) -> dict | None:
        """Convert a raw Mastodon status dict to a social_item dict.

        Mastodon status structure:
          status.id             -> numeric string ID
          status.url            -> canonical URL (may be None for local)
          status.uri            -> ActivityPub URI (always present)
          status.content        -> HTML content
          status.account.acct   -> "user" or "user@instance"
          status.created_at     -> ISO 8601 timestamp
          status.favourites_count, reblogs_count, replies_count

        Args:
            status: Raw status dict from the Mastodon API.

        Returns:
            social_item dict ready for insert_social_item(), or None if the
            status cannot be meaningfully parsed (missing content or URL).
        """
        # Prefer url over uri; fall back to constructing one from id
        url: str = (status.get("url") or status.get("uri") or "").strip()
        if not url:
            return None

        status_id: str = str(status.get("id") or url)

        account = status.get("account") or {}
        acct: str = account.get("acct") or ""
        # Mastodon acct is "user" for local accounts; make it fully qualified
        # if we can determine the instance from the URL
        if acct and "@" not in acct and url:
            try:
                from urllib.parse import urlparse

                host = urlparse(url).hostname or ""
                if host:
                    acct = f"{acct}@{host}"
            except Exception:
                pass
        author_handle = f"@{acct}" if acct else None

        content_html: str = status.get("content") or ""
        content_text = _strip_html(content_html)
        if not content_text:
            return None

        # Parse timestamp
        created_at_raw: str = status.get("created_at") or ""
        published_at: str | None = None
        if created_at_raw:
            try:
                dt = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                published_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, AttributeError):
                published_at = created_at_raw[:19] + "Z" if created_at_raw else None

        # Engagement: favourites + reblogs (replies excluded — often noise)
        favourites = int(status.get("favourites_count") or 0)
        reblogs = int(status.get("reblogs_count") or 0)
        engagement_score = round(math.log1p(favourites + reblogs), 4)

        doi = self._extract_doi(content_text)
        paper_id = self._match_paper(doi)

        return {
            "source": "mastodon",
            "source_id": url,
            "author_handle": author_handle,
            "content": content_text[:2000],
            "url": url,
            "paper_id": paper_id,
            "engagement_score": engagement_score,
            "published_at": published_at,
        }

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Collect economics-related Mastodon posts from configured instances.

        For each instance:
          1. Fetch the public timeline.
          2. Fetch per-hashtag timelines for all configured hashtags.
          3. Parse and deduplicate by status URL.

        Returns:
            List of social_item dicts ready for insert_social_item(). Always
            returns a list; returns empty list on total failure.
        """
        seen_urls: set[str] = set()
        items: list[dict] = []

        for instance in self.INSTANCES:
            # Public timeline
            raw_statuses = self._fetch_timeline(instance)
            print(
                f"[mastodon] {instance} public timeline: {len(raw_statuses)} statuses",
                file=sys.stderr,
            )
            for status in raw_statuses:
                url = (status.get("url") or status.get("uri") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                item = self._parse_status(status)
                if item:
                    items.append(item)

            # Hashtag timelines
            for hashtag in self.HASHTAGS:
                raw_statuses = self._fetch_hashtag(instance, hashtag)
                print(
                    f"[mastodon] {instance} #{hashtag}: {len(raw_statuses)} statuses",
                    file=sys.stderr,
                )
                for status in raw_statuses:
                    url = (status.get("url") or status.get("uri") or "").strip()
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    item = self._parse_status(status)
                    if item:
                        items.append(item)

        print(f"[mastodon] collected {len(items)} unique posts", file=sys.stderr)
        return items

    # ------------------------------------------------------------------
    # Run override
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute the sensor: collect posts, insert as social_items, log run.

        Overrides BaseSensor.run() because social items bypass the paper
        deduplication pipeline and are inserted directly via insert_social_item().

        Returns:
            Stats dict with keys: sensor, watch, status, found, new, errors,
            and optionally error_message on failure.
        """
        from lib.db import insert_social_item, log_sensor_end, log_sensor_start

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
                    print(f"[mastodon] insert error: {exc}", file=sys.stderr)

            log_sensor_end(
                run_id,
                "success",
                int(self.stats["found"]),
                int(self.stats["new"]),
            )
        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"[mastodon] sensor failed: {exc}", file=sys.stderr)

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
    sensor = MastodonSensor()
    sensor.run()
