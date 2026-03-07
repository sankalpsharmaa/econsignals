"""Bluesky AT Protocol sensor for EconSignals.

Monitors economics discussion on Bluesky, linking posts to papers where possible.

Primary approach: Authenticated API using BLUESKY_HANDLE + BLUESKY_APP_PASSWORD env vars.
Fallback: Public API at public.api.bsky.app (no auth required, slightly limited).

Searches economics hashtags/keywords and fetches recent posts from tracked authors
who have a bluesky_handle set in the authors table. Extracts DOIs from post text
to link social items to papers already in the database.

Usage:
    python sensors/bluesky.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

from econsignals.sensors._base import BaseSensor, _SSL_CTX

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PUBLIC_BASE = "https://public.api.bsky.app/xrpc"
_AUTH_BASE = "https://bsky.social/xrpc"
_SESSION_URL = f"{_AUTH_BASE}/com.atproto.server.createSession"

_SEARCH_LIMIT = 25
_FEED_LIMIT = 25

# Bluesky AT URI pattern: at://did:plc:.../app.bsky.feed.post/rkey
_RE_AT_URI = re.compile(r"at://([^/]+)/app\.bsky\.feed\.post/([a-zA-Z0-9]+)")

# DOI patterns: bare DOI, doi.org URL, or dx.doi.org URL
_RE_DOI = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s\"'<>]+)",
    re.IGNORECASE,
)
# Fallback: bare DOI starting with 10. not preceded by /
_RE_DOI_BARE = re.compile(r"(?<![/\w])(10\.\d{4,9}/[^\s\"'<>]{4,})")

# NBER working paper URLs
_RE_NBER = re.compile(r"nber\.org/papers/(w\d+)", re.IGNORECASE)

# arXiv IDs
_RE_ARXIV = re.compile(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class BlueskySensor(BaseSensor):
    """Collect economics-related Bluesky posts and link them to papers.

    Authenticates via BLUESKY_HANDLE and BLUESKY_APP_PASSWORD environment
    variables if available; otherwise uses the unauthenticated public API.

    Overrides run() to insert social_items directly rather than routing
    through the paper dedup pipeline.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 0.5 requests per second (2-second interval) to respect Bluesky.
    """

    name = "bluesky"
    watch = "social"
    rate_limit = 0.5  # 1 request per 2 seconds

    SEARCH_QUERIES: list[str] = [
        "#EconTwitter",
        "#EconSky",
        "#DevEcon",
        "economics research",
        "working paper",
        "NBER",
    ]

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate(self) -> str | None:
        """Create a Bluesky session and return the access JWT.

        Reads BLUESKY_HANDLE and BLUESKY_APP_PASSWORD from the environment.
        If either is absent, returns None and the sensor falls back to the
        public unauthenticated API.

        Returns:
            Access JWT string, or None if credentials are missing or auth fails.
        """
        handle = os.environ.get("BLUESKY_HANDLE", "").strip()
        password = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()

        if not handle or not password:
            print(
                "[bluesky] no credentials found, using public API",
                file=sys.stderr,
            )
            return None

        payload = json.dumps({"identifier": handle, "password": password}).encode()
        req = Request(
            _SESSION_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        self.limiter.wait()
        try:
            with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                data = json.loads(resp.read())
            token: str = data["accessJwt"]
            print(f"[bluesky] authenticated as {handle}", file=sys.stderr)
            return token
        except HTTPError as exc:
            print(
                f"[bluesky] authentication failed (HTTP {exc.code}): {exc.reason}",
                file=sys.stderr,
            )
            return None
        except Exception as exc:
            print(f"[bluesky] authentication error: {exc}", file=sys.stderr)
            return None

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def _auth_headers(self, auth_token: str | None) -> dict | None:
        """Build Authorization header dict, or None if no token.

        Args:
            auth_token: Bearer JWT, or None for unauthenticated requests.

        Returns:
            Header dict with Authorization key, or None.
        """
        if auth_token:
            return {"Authorization": f"Bearer {auth_token}"}
        return None

    def _search_posts(self, query: str, auth_token: str | None = None) -> list[dict]:
        """Search Bluesky for posts matching a query string.

        Uses the authenticated API base when a token is provided, otherwise
        falls back to the public API.

        Args:
            query: Search query (hashtag or keyword string).
            auth_token: Optional Bearer JWT for authenticated requests.

        Returns:
            List of raw post dicts from the API response, or empty list on error.
        """
        base = _AUTH_BASE if auth_token else _PUBLIC_BASE
        params = urlencode({"q": query, "limit": _SEARCH_LIMIT})
        url = f"{base}/app.bsky.feed.searchPosts?{params}"

        try:
            data = self.fetch_json(url, headers=self._auth_headers(auth_token))
        except Exception as exc:
            print(
                f"[bluesky] search failed for '{query}': {exc}",
                file=sys.stderr,
            )
            return []

        posts = data.get("posts") or []
        if not isinstance(posts, list):
            return []

        return posts

    def _get_author_feed(self, handle: str, auth_token: str | None = None) -> list[dict]:
        """Fetch recent posts from a specific Bluesky author.

        Args:
            handle: Bluesky handle (e.g. "economist.bsky.social").
            auth_token: Optional Bearer JWT for authenticated requests.

        Returns:
            List of raw feed view post dicts, or empty list on error.
        """
        base = _AUTH_BASE if auth_token else _PUBLIC_BASE
        params = urlencode({"actor": handle, "limit": _FEED_LIMIT})
        url = f"{base}/app.bsky.feed.getAuthorFeed?{params}"

        try:
            data = self.fetch_json(url, headers=self._auth_headers(auth_token))
        except Exception as exc:
            print(
                f"[bluesky] author feed failed for '{handle}': {exc}",
                file=sys.stderr,
            )
            return []

        feed = data.get("feed") or []
        if not isinstance(feed, list):
            return []

        # Each feed entry is {"post": {...}, "reply": {...}, ...}
        return [entry.get("post", {}) for entry in feed if entry.get("post")]

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _extract_doi(self, text: str) -> str | None:
        """Find the first DOI in a post's text.

        Checks for doi.org URLs, doi: prefix, and bare DOIs starting with 10.

        Args:
            text: Raw post text.

        Returns:
            DOI string (without URL prefix), or None if not found.
        """
        m = _RE_DOI.search(text)
        if m:
            # Strip trailing punctuation that might have been captured
            return m.group(1).rstrip(".,;)")

        m = _RE_DOI_BARE.search(text)
        if m:
            return m.group(1).rstrip(".,;)")

        return None

    def _match_paper(self, doi: str | None = None) -> int | None:
        """Look up an existing paper in the DB and return its id.

        Only matches by DOI for now. NBER handles are converted to their
        canonical DOI (10.3386/wNNNNN) before lookup.

        Args:
            doi: DOI string to search for, or None to skip lookup.

        Returns:
            Paper id integer if a match is found, otherwise None.
        """
        if not doi:
            return None

            from econsignals.lib.db import find_paper_by_doi

        # Normalise: strip leading doi.org prefix if present
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)

        try:
            paper = find_paper_by_doi(doi)
            if paper:
                return paper["id"]
        except Exception as exc:
            print(f"[bluesky] paper lookup error for doi={doi}: {exc}", file=sys.stderr)

        return None

    def _parse_post(self, post: dict) -> dict | None:
        """Convert a raw Bluesky post dict to a social_item dict.

        Handles the AT Protocol post structure:
          post.uri       -> at://did/app.bsky.feed.post/rkey
          post.author    -> {handle, displayName, did}
          post.record    -> {text, createdAt, ...}
          post.likeCount, post.repostCount, post.replyCount

        Args:
            post: Raw post dict from the Bluesky API.

        Returns:
            social_item dict, or None if the post cannot be parsed
            (missing URI or text).
        """
        uri: str = post.get("uri") or ""
        if not uri:
            return None

        m = _RE_AT_URI.match(uri)
        if not m:
            return None

        did = m.group(1)
        rkey = m.group(2)

        author_info = post.get("author") or {}
        handle: str = author_info.get("handle") or did

        record = post.get("record") or {}
        text: str = record.get("text") or ""
        if not text:
            return None

        # Published timestamp
        created_at_raw: str = record.get("createdAt") or post.get("indexedAt") or ""
        published_at: str | None = None
        if created_at_raw:
            try:
                # Bluesky timestamps are ISO 8601 with Z suffix
                dt = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                published_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, AttributeError):
                published_at = created_at_raw[:19] + "Z" if created_at_raw else None

        # Engagement: likes + reposts + replies, normalized to [0, 1] via log scale
        likes = int(post.get("likeCount") or 0)
        reposts = int(post.get("repostCount") or 0)
        replies = int(post.get("replyCount") or 0)
        raw_engagement = likes + reposts + replies
        # Simple log-scale normalization: log1p keeps 0 at 0
        import math
        engagement_score = round(math.log1p(raw_engagement), 4)

        # Web URL: https://bsky.app/profile/{handle}/post/{rkey}
        url = f"https://bsky.app/profile/{handle}/post/{rkey}"

        # Try to link to a paper
        doi = self._extract_doi(text)

        # Also check for NBER links and convert to DOI
        if not doi:
            nber_m = _RE_NBER.search(text)
            if nber_m:
                doi = f"10.3386/{nber_m.group(1).lower()}"

        paper_id = self._match_paper(doi=doi)

        return {
            "source": "bluesky",
            "source_id": uri,
            "author_handle": handle,
            "content": text[:2000],  # Guard against pathologically long posts
            "url": url,
            "paper_id": paper_id,
            "engagement_score": engagement_score,
            "published_at": published_at,
        }

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def _collect_search_posts(self, auth_token: str | None) -> list[dict]:
        """Search all configured queries and return deduplicated social_item dicts.

        Args:
            auth_token: Optional JWT for authenticated requests.

        Returns:
            List of social_item dicts.
        """
        seen_uris: set[str] = set()
        items: list[dict] = []

        for query in self.SEARCH_QUERIES:
            raw_posts = self._search_posts(query, auth_token)
            print(
                f"[bluesky] search '{query}': {len(raw_posts)} posts",
                file=sys.stderr,
            )

            for post in raw_posts:
                uri = post.get("uri") or ""
                if uri in seen_uris:
                    continue
                seen_uris.add(uri)

                item = self._parse_post(post)
                if item:
                    items.append(item)

        return items

    def _collect_author_feeds(self, auth_token: str | None) -> list[dict]:
        """Fetch recent posts from tracked authors with Bluesky handles.

        Queries the authors table for rows where is_tracked=1 and
        bluesky_handle is not null.

        Args:
            auth_token: Optional JWT for authenticated requests.

        Returns:
            List of social_item dicts.
        """
        from econsignals.lib.db import get_tracked_authors

        try:
            tracked = get_tracked_authors()
        except Exception as exc:
            print(f"[bluesky] could not load tracked authors: {exc}", file=sys.stderr)
            return []

        authors_with_handles = [
            a for a in tracked
            if a.get("bluesky_handle") and a["bluesky_handle"].strip()
        ]

        if not authors_with_handles:
            print("[bluesky] no tracked authors with Bluesky handles", file=sys.stderr)
            return []

        seen_uris: set[str] = set()
        items: list[dict] = []

        for author in authors_with_handles:
            bsky_handle: str = author["bluesky_handle"].strip()
            raw_posts = self._get_author_feed(bsky_handle, auth_token)
            print(
                f"[bluesky] author feed '{bsky_handle}': {len(raw_posts)} posts",
                file=sys.stderr,
            )

            for post in raw_posts:
                uri = post.get("uri") or ""
                if uri in seen_uris:
                    continue
                seen_uris.add(uri)

                item = self._parse_post(post)
                if item:
                    items.append(item)

        return items

    def collect(self) -> list[dict]:
        """Collect economics-related Bluesky posts.

        Steps:
        1. Attempt authentication via env credentials.
        2. Search for posts matching SEARCH_QUERIES.
        3. Fetch recent posts from tracked authors with bluesky_handle set.
        4. Deduplicate by source_id (URI).

        Returns:
            List of social_item dicts ready for insert_social_item().
        """
        auth_token = self._authenticate()

        search_items = self._collect_search_posts(auth_token)
        author_items = self._collect_author_feeds(auth_token)

        # Merge, deduplicating by source_id
        seen_uris: set[str] = {item["source_id"] for item in search_items}
        merged: list[dict] = list(search_items)

        for item in author_items:
            if item["source_id"] not in seen_uris:
                seen_uris.add(item["source_id"])
                merged.append(item)

        print(f"[bluesky] collected {len(merged)} unique posts", file=sys.stderr)
        return merged

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
        from econsignals.lib.db import log_sensor_start, log_sensor_end, insert_social_item

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
                    print(f"[bluesky] insert error: {exc}", file=sys.stderr)

            log_sensor_end(
                run_id,
                "success",
                int(self.stats["found"]),
                int(self.stats["new"]),
            )
        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"[bluesky] sensor failed: {exc}", file=sys.stderr)

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
    from econsignals.lib.db import init_db

    init_db()
    sensor = BlueskySensor()
    sensor.run()
