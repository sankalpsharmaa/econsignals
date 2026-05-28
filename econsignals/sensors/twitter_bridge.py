"""Twitter/X sensor for EconSignals.

Tries two methods in order:
  1. Exa API tweet search (best quality, needs EXA_API_KEY)
  2. Public economics RSS/Atom feeds that aggregate Twitter discourse

Falls back gracefully — always returns whatever it can find.

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
# Exa search queries for economics tweets
# ---------------------------------------------------------------------------

# --- Research paper / social post queries (consolidated for fewer API calls) ---
EXA_QUERIES: list[str] = [
    "India housing land urban zoning property rights urbanization new paper working paper",
    "development economics RCT causal inference difference-in-differences new results paper",
    "labor economics migration informal sector women gender developing countries South Asia",
    "urban economics housing supply regulation real estate land use spatial new paper",
    "Gyourko Duranton Brueckner Henderson Olken Deininger Heckman economics new paper",
    "#EconTwitter #DevEcon new working paper NBER",
]

# --- Funding / grants / CFP queries (consolidated) ---
EXA_FUNDING_QUERIES: list[str] = [
    "PEDL STEG IGC CEPR call for proposals funding deadline development economics",
    "J-PAL economics research grant funding call for proposals poverty inequality",
    "development economics urban housing India research grant fellowship deadline 2026",
    "economics conference call for papers submission deadline CFP NBER IZA BREAD 2026",
    "NSF economics grant SES social sciences research funding deadline",
]

_FUNDING_RE = re.compile(
    r"\b(deadline|call for (proposals?|papers?|applications?)|"
    r"funding|grant|fellowship|apply by|submit by|CFP|"
    r"applications? (open|due|close)|proposals? due)\b",
    re.IGNORECASE,
)

# Exa fills the `highlights` field of a tweet with an AI description of the
# tweet's attached image or linked page (e.g. "The image prominently displays
# the Artha journal cover..."), never the actual post. Drop any content that
# is clearly such a caption rather than a real post.
_IMAGE_CAPTION_RE = re.compile(
    r"^\s*(the|this)\s+(image|photo|photograph|graphic|picture|illustration|"
    r"screenshot|infographic|chart|figure|visual)\b",
    re.IGNORECASE,
)


def _is_image_caption(text: str) -> bool:
    """True if `text` reads like an Exa AI image/page caption, not a real post.

    Heuristics: starts with an image-description phrase, or carries neither a
    URL nor an @handle (real posts almost always include one or the other).
    """
    if not text:
        return True
    if _IMAGE_CAPTION_RE.match(text):
        return True
    # A genuine post usually contains a link or an @mention; a bare prose
    # description of an image typically has neither.
    has_url = "http" in text
    has_handle = bool(re.search(r"@\w", text))
    return not (has_url or has_handle)

_MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_DATE_PATTERNS = [
    re.compile(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})[,\s]+(\d{4})", re.I),
    re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.?\s+(\d{1,2})[,\s]+(\d{4})", re.I),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
]


def _extract_deadline_date(text: str) -> str | None:
    """Try to find a deadline date in tweet text. Returns YYYY-MM-DD or None."""
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if g[0].isdigit() and len(g[0]) == 4:
                return f"{int(g[0]):04d}-{int(g[1]):02d}-{int(g[2]):02d}"
            month = _MONTH_MAP.get(g[0].lower().rstrip("."), 0)
            if month:
                day, year = int(g[1]), int(g[2])
                if 1 <= day <= 31 and 2024 <= year <= 2030:
                    return f"{year:04d}-{month:02d}-{day:02d}"
        except (ValueError, IndexError):
            continue
    return None

# ---------------------------------------------------------------------------
# Fallback: public feeds that surface Twitter-adjacent econ discourse
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
    """Collect economics social content via Exa or RSS fallback."""

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

    def _collect_exa(self) -> list[dict]:
        items: list[dict] = []
        seen: set[str] = set()

        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

        for query in EXA_QUERIES:
            results = exa_search(
                query,
                category="tweet",
                num_results=25,
                max_characters=600,
                start_published_date=cutoff,
                log_prefix="[twitter_bridge]",
            )
            if not results:
                continue

            print(f"[twitter_bridge] exa query={query!r}: {len(results)} results", file=sys.stderr)

            for r in results:
                url = (r.get("url") or "").strip()
                if not url:
                    continue
                source_id = f"twitter:{url}"
                if source_id in seen:
                    continue
                seen.add(source_id)

                # Prefer the real post body; the AI-generated highlight is only
                # a fallback summary, never the stored content.
                text = (r.get("text") or "").strip()
                highlights = r.get("highlights") or []
                hl_text = " ".join(highlights) if isinstance(highlights, list) else ""
                content = (text or hl_text).strip()
                if not content:
                    continue

                # Drop items whose only content is an Exa image/page caption
                # rather than an actual tweet.
                if not text and _is_image_caption(hl_text):
                    continue
                if text and _is_image_caption(text) and not hl_text.strip():
                    continue

                # Resolve the handle from Exa's author field or the URL, then
                # normalize to exactly one leading '@' (avoids '@@handle').
                author = (r.get("author") or "").strip()
                if not author:
                    m = re.search(r"(?:twitter\.com|x\.com)/([^/]+)", url)
                    author = m.group(1) if m else "unknown"
                author = "@" + author.lstrip("@")

                items.append({
                    "source": "twitter",
                    "source_id": source_id,
                    "author_handle": author,
                    "content": content[:2000],
                    "url": url,
                    "paper_id": self._match_paper(self._extract_doi(content)),
                    "engagement_score": float(r.get("score") or 0),
                    "published_at": r.get("publishedDate") or r.get("published_date"),
                })

        return items

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

    _PAPER_SIGNAL_RE = re.compile(
        r"\b(new (working )?paper|paper alert|new research|working paper|"
        r"wp\s*\d|nber|ssrn|published in|forthcoming|accepted at|"
        r"our (new |latest )?paper|just released|new study)\b",
        re.IGNORECASE,
    )

    def _ingest_papers_from_tweets(self, items: list[dict], max_lookups: int = 12) -> int:
        """Find papers mentioned in tweets via Exa paper search, then ingest them."""
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
            f"[twitter_bridge] {len(candidates)} tweets look like paper announcements, "
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

                tweet_url = item.get("url") or ""
                sid = f"twitter:{hashlib.md5((url or title).encode()).hexdigest()[:12]}"
                insert_paper_source(paper_id, "twitter", sid, tweet_url)

                for pos, name in enumerate(authors[:6]):
                    if name:
                        aid = upsert_author(name, normalize_author_name(name))
                        link_paper_author(paper_id, aid, pos)

                ingested += 1
            except Exception as exc:
                print(f"[twitter_bridge] paper ingest error: {exc}", file=sys.stderr)

        return ingested

    def _collect_funding_tweets(self) -> list[dict]:
        """Search for funding/grant/CFP tweets and extract deadlines."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        deadlines: list[dict] = []

        for query in EXA_FUNDING_QUERIES:
            results = exa_search(
                query,
                category="tweet",
                num_results=10,
                max_characters=800,
                start_published_date=cutoff,
                log_prefix="[twitter_bridge]",
            )
            if not results:
                continue

            print(f"[twitter_bridge] funding query={query!r}: {len(results)} results", file=sys.stderr)

            for r in results:
                # Prefer the real post body; fall back to the highlight only.
                text = (r.get("text") or "").strip()
                highlights = r.get("highlights") or []
                hl_text = " ".join(highlights) if isinstance(highlights, list) and highlights else ""
                content = (text or hl_text).strip()
                if not content or not _FUNDING_RE.search(content):
                    continue

                # Skip Exa image/page captions masquerading as posts.
                if not text and _is_image_caption(hl_text):
                    continue

                # A curated deadline must carry a real date; tweets with no
                # parseable deadline are not written to the deadlines table.
                url = (r.get("url") or "").strip()
                deadline_date = _extract_deadline_date(content)
                if not deadline_date:
                    continue
                name = content[:120].replace("\n", " ").strip()
                if len(name) > 100:
                    name = name[:100] + "..."

                relevance = 0.5
                content_lower = content.lower()
                for kw in ("pedl", "steg", "igc", "j-pal", "jpal", "nber",
                           "development economics", "urban economics",
                           "housing", "land", "india", "south asia",
                           "labor", "migration", "poverty", "inequality"):
                    if kw in content_lower:
                        relevance = min(relevance + 0.1, 0.95)

                deadlines.append({
                    "type": "funding",
                    "name": name,
                    "organization": "Twitter/X",
                    "deadline_date": deadline_date,
                    "event_date": None,
                    "url": url,
                    "description": content[:300],
                    "relevance_score": relevance,
                })

        print(f"[twitter_bridge] extracted {len(deadlines)} funding deadlines from tweets", file=sys.stderr)
        return deadlines

    def collect(self) -> list[dict]:
        exa_items = self._collect_exa()
        if exa_items:
            print(f"[twitter_bridge] exa returned {len(exa_items)} items", file=sys.stderr)
            return exa_items

        print("[twitter_bridge] exa unavailable, falling back to RSS feeds", file=sys.stderr)
        rss_items = self._collect_rss()
        print(f"[twitter_bridge] rss collected {len(rss_items)} items", file=sys.stderr)
        return rss_items

    def run(self) -> dict:
        from econsignals.lib.db import log_sensor_start, log_sensor_end, insert_social_item, upsert_deadline

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
                print(f"[twitter_bridge] ingested {paper_count} new papers from tweet DOIs", file=sys.stderr)

            funding = self._collect_funding_tweets()
            for dl in funding:
                try:
                    upsert_deadline(dl)
                except Exception as exc:
                    print(f"[twitter_bridge] deadline upsert error: {exc}", file=sys.stderr)

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
