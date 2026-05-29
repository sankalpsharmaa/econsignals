"""Twitter/X-adjacent social sensor for EconSignals.

Exa deprecated its tweet category (it no longer has X/Twitter coverage), so
this sensor surfaces economics social discourse from public RSS/Atom feeds and
uses Exa only to resolve the papers those posts announce. Native social posts
come from the dedicated `bluesky` sensor.

Usage:
    python sensors/twitter_bridge.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

from econsignals.sensors._base import BaseSensor
from econsignals.sensors._exa import exa_search

# ---------------------------------------------------------------------------
# Public feeds that surface Twitter-adjacent econ discourse
# ---------------------------------------------------------------------------

FALLBACK_FEEDS: dict[str, str] = {
    "econtwitter_rss": "https://www.econtwitter.net/rss",
    "aeaweb_highlights": "https://www.aeaweb.org/research/rss-feed",
    "bankunderground": "https://bankunderground.co.uk/feed/",
    "libertystreet": "https://libertystreeteconomics.newyorkfed.org/feed/",
    "brookings_econ": "https://www.brookings.edu/topic/economic-studies/feed/",
    "imf_blog": "https://www.imf.org/en/Blogs/rss",
    "worldbank_blogs": "https://blogs.worldbank.org/en/developmenttalk/feed",
}

_RE_DOI = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s\"'<>]+)",
    re.IGNORECASE,
)


# Trailing site-name chrome that Exa returns from a page's HTML <title>,
# e.g. "Minimum Wages and Rise of the Robots | NBER" or "... - HAL-SHS".
_TITLE_CHROME_RE = re.compile(r"\s*[|–—-]\s*[^|–—-]{1,60}$")


def _strip_title_chrome(title: str) -> str:
    """Remove a trailing ' | Site' or ' - Site' chrome segment from a title."""
    if not title:
        return ""
    stripped = _TITLE_CHROME_RE.sub("", title).strip()
    # Only accept the strip if it leaves a substantive title behind; otherwise
    # the separator was part of the real title, so keep the original.
    return stripped if len(stripped) >= 15 else title


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_rss_date(date_str: str) -> str | None:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str.strip())
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str.strip()[:len(fmt) + 5], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            pass
    return None


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


class TwitterBridgeSensor(BaseSensor):
    """Collect economics social discourse from public RSS feeds.

    Exa is used only to resolve papers announced in those posts; the Exa
    tweet-search path was removed when Exa dropped its tweet category.
    """

    name = "twitter_bridge"
    watch = "social"
    rate_limit = 0.5

    def _extract_doi(self, text: str) -> str | None:
        m = _RE_DOI.search(text or "")
        return m.group(1).rstrip(".,;)") if m else None

    def _match_paper(self, doi: str | None) -> int | None:
        if not doi:
            return None
        try:
            from econsignals.lib.db import find_paper_by_doi
            paper = find_paper_by_doi(doi)
            return paper["id"] if paper else None
        except Exception:
            return None

    def _fetch_feed(self, url: str) -> bytes:
        return self.fetch_url(url, timeout=20)

    def _collect_rss(self) -> list[dict]:
        items: list[dict] = []
        seen: set[str] = set()

        for feed_name, feed_url in FALLBACK_FEEDS.items():
            try:
                xml_bytes = self._fetch_feed(feed_url)
            except Exception as exc:
                print(f"[twitter_bridge] rss {feed_name} failed: {exc}", file=sys.stderr)
                continue

            try:
                root = ET.fromstring(xml_bytes)
            except ET.ParseError:
                continue

            ns_atom = "http://www.w3.org/2005/Atom"
            entries = list(root.iter("item")) + list(root.iter(f"{{{ns_atom}}}entry"))
            count = 0

            for entry in entries:
                link_el = entry.find("link")
                atom_link = entry.find(f"{{{ns_atom}}}link")
                url = ""
                if link_el is not None and link_el.text:
                    url = link_el.text.strip()
                elif atom_link is not None:
                    url = (atom_link.get("href") or "").strip()

                if not url:
                    continue

                source_id = f"rss:{hashlib.md5(url.encode()).hexdigest()[:12]}"
                if source_id in seen:
                    continue
                seen.add(source_id)

                title = _text(entry.find("title")) or _text(entry.find(f"{{{ns_atom}}}title"))
                desc = _text(entry.find("description")) or _text(entry.find(f"{{{ns_atom}}}summary"))
                content = _strip_html(f"{title}. {desc}")[:2000] if desc else title

                pub = (
                    _text(entry.find("pubDate"))
                    or _text(entry.find(f"{{{ns_atom}}}published"))
                    or _text(entry.find(f"{{{ns_atom}}}updated"))
                )

                items.append({
                    "source": "rss",
                    "source_id": source_id,
                    "author_handle": feed_name,
                    "content": content,
                    "url": url,
                    "paper_id": self._match_paper(self._extract_doi(content)),
                    "engagement_score": 0,
                    "published_at": _parse_rss_date(pub),
                })
                count += 1

            if count:
                print(f"[twitter_bridge] rss {feed_name}: {count} items", file=sys.stderr)

        return items

    _PAPER_SIGNAL_RE = re.compile(
        r"\b(new (working )?paper|paper alert|new research|working paper|"
        r"wp\s*\d|nber|ssrn|published in|forthcoming|accepted at|"
        r"our (new |latest )?paper|just released|new study)\b",
        re.IGNORECASE,
    )

    def _ingest_papers_from_tweets(self, items: list[dict], max_lookups: int = 12) -> int:
        """Find papers announced in feed posts via Exa paper search, then ingest them."""
        from econsignals.lib.db import (
            insert_paper, insert_paper_source,
            upsert_author, link_paper_author,
        )
        from econsignals.lib.normalize import normalize_title, canonical_paper_id, normalize_author_name

        candidates = [
            item for item in items
            if self._PAPER_SIGNAL_RE.search(item.get("content") or "")
        ]
        if not candidates:
            return 0

        print(
            f"[twitter_bridge] {len(candidates)} posts look like paper announcements, "
            f"searching top {min(len(candidates), max_lookups)}",
            file=sys.stderr,
        )

        ingested = 0
        seen_titles: set[str] = set()

        for item in candidates[:max_lookups]:
            content = item.get("content") or ""
            query = content[:200].replace("\n", " ").strip()
            if not query:
                continue

            results = exa_search(
                query,
                category="research paper",
                num_results=1,
                max_characters=400,
                log_prefix="[twitter_bridge:paper]",
            )
            if not results:
                continue

            r = results[0]
            url = (r.get("url") or "").strip()
            highlights = r.get("highlights") or []
            text = (r.get("text") or "").strip() or (
                " ".join(highlights) if isinstance(highlights, list) and highlights else ""
            ).strip()

            # Strip web-page chrome (e.g. " | NBER", " - HAL-SHS") that Exa
            # returns verbatim from the HTML <title>, then require either a real
            # DOI or a substantive de-chromed title before ingesting.
            title = _strip_title_chrome((r.get("title") or "").strip())
            doi = self._extract_doi(url) or self._extract_doi(text)
            if not doi and (not title or len(title) < 15):
                continue

            title_norm = normalize_title(title) if title else ""
            dedup_key = doi or title_norm
            if dedup_key in seen_titles:
                continue
            seen_titles.add(dedup_key)

            author_raw = (r.get("author") or "").strip()
            pub_date = (r.get("publishedDate") or "")[:10] or None

            authors = [a.strip() for a in re.split(r",\s*|\band\b", author_raw) if a.strip()] if author_raw else []

            paper_dict = {
                "canonical_id": canonical_paper_id(title, authors),
                "title": title,
                "title_normalized": title_norm,
                "abstract": text[:1000] if text else None,
                "doi": doi,
                "url": url,
                "published_at": pub_date,
                "paper_type": "working_paper",
                "jel_codes": None,
                "keywords": None,
            }

            try:
                paper_id = insert_paper(paper_dict)
                if not paper_id:
                    continue

                post_url = item.get("url") or ""
                sid = f"twitter:{hashlib.md5((url or title).encode()).hexdigest()[:12]}"
                insert_paper_source(paper_id, "twitter", sid, post_url)

                for pos, name in enumerate(authors[:6]):
                    if name:
                        aid = upsert_author(name, normalize_author_name(name))
                        link_paper_author(paper_id, aid, pos)

                ingested += 1
            except Exception as exc:
                print(f"[twitter_bridge] paper ingest error: {exc}", file=sys.stderr)

        return ingested

    def collect(self) -> list[dict]:
        # Exa deprecated its tweet category, so social discourse now comes from
        # the public econ RSS/Atom feeds; the bluesky sensor covers native posts.
        rss_items = self._collect_rss()
        print(f"[twitter_bridge] rss collected {len(rss_items)} items", file=sys.stderr)
        return rss_items

    def run(self) -> dict:
        from econsignals.lib.db import log_sensor_start, log_sensor_end, insert_social_item

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                try:
                    if insert_social_item(item) > 0:
                        self.stats["new"] = int(self.stats["new"]) + 1
                except Exception as exc:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(f"[twitter_bridge] insert error: {exc}", file=sys.stderr)

            paper_count = self._ingest_papers_from_tweets(items)
            if paper_count:
                print(f"[twitter_bridge] ingested {paper_count} new papers from feed posts", file=sys.stderr)

            log_sensor_end(
                run_id, "success",
                int(self.stats["found"]), int(self.stats["new"]),
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


if __name__ == "__main__":
    from econsignals.lib.db import init_db
    init_db()
    TwitterBridgeSensor().run()
