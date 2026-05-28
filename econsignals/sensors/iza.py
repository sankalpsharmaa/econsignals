"""IZA Discussion Papers sensor for EconSignals."""

from __future__ import annotations
import sys

from econsignals.sensors._base import BaseSensor, PROJ_ROOT

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

_BASE_URL = "https://www.iza.org"
_LISTING_URL = "https://www.iza.org/publications/dp"
_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

# 15 pages (~300 papers) lets a one-time backlog fully paginate; the
# page_old early-break keeps steady-state daily runs to ~2 pages.
_MAX_PAGES = 15
# authors now come from the listing, so detail fetches only enrich
# abstract/date/JEL; budget bounds the one-time backlog enrichment.
_DETAIL_BUDGET = 50
_DEFAULT_LOOKBACK_DAYS = 30
_PAPERS_PER_PAGE = 20

_BASE_KEYWORDS: list[str] = ["labor economics", "IZA"]

_RE_DP_HREF = re.compile(r"/publications/dp/(\d+)(?:/[^\s\"'#?]*)?")
_RE_DP_TEXT = re.compile(
    r"(?:IZA\s+)?(?:Discussion\s+Paper|DP)\s*(?:No\.?|#)?\s*(\d{3,7})",
    re.IGNORECASE,
)
_RE_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
_RE_JEL_LABEL = re.compile(
    r"\bJEL(?:\s+Classification)?[:\s]+([A-Z0-9,\s;.-]{2,120})",
    re.IGNORECASE,
)
_RE_JEL_CODE = re.compile(r"\b[A-Z][0-9]{1,2}\b")
_RE_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_RE_MONTH_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[\s,]+(\d{4})\b",
    re.IGNORECASE,
)
_MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _strip_html(raw: str) -> str:
    """Strip tags and collapse whitespace."""
    if not raw:
        return ""
    raw = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        " ",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", raw)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str | None) -> str | None:
    """Parse date-ish text into YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()

    m = _RE_ISO_DATE.search(raw)
    if m:
        return m.group(1)

    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    m2 = _RE_MONTH_YEAR.search(raw)
    if m2:
        month = _MONTH_MAP.get(m2.group(1).lower())
        year = int(m2.group(2))
        if month and 1900 <= year <= 2100:
            return f"{year:04d}-{month:02d}-01"

    for fmt in ("%B %Y", "%b %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-01")
        except ValueError:
            pass
    return None


def _parse_authors(raw: str) -> list[str]:
    """Split an author string into names."""
    if not raw:
        return []
    text = _strip_html(raw)
    text = re.sub(r"^(authors?|by)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\band\b", ",", text, flags=re.IGNORECASE)
    text = text.replace("&", ",").replace(";", ",")
    parts = [part.strip() for part in text.split(",") if part.strip()]

    authors: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not (1 < len(part) < 120):
            continue
        if part.lower() in {"iza", "discussion paper", "iza discussion paper"}:
            continue
        if part not in seen:
            seen.add(part)
            authors.append(part)
    return authors


class _ListingParser(HTMLParser):
    """Parse listing cards from IZA discussion paper pages.

    IZA wraps each paper in an ``<article>`` element with Tailwind utility
    classes (no semantic field classes). Per article:
      - dp_number + url come from the ``/publications/dp/NNNNN`` anchor;
      - title is the inner text of that same anchor;
      - authors are the article's residual text once the DP-number badge and
        the title are removed (this captures both plain-text names and the
        person-/external-link author anchors).
    Abstract and date are not in the listing and are fetched per detail page.

    A title-only anchor scan runs as a degraded fallback for any DP links that
    fall outside an ``<article>`` boundary.
    """

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []
        self.fallback_items: list[dict] = []

        # state for the current <article> card
        self._current: dict | None = None
        self._depth = 0
        self._buf: list[str] = []

        # state for capturing the title anchor's inner text
        self._title_anchor_depth: int | None = None
        self._title_buf: list[str] = []

        # state for the degraded title-only anchor fallback
        self._in_fallback_anchor = False
        self._fallback_href = ""
        self._fallback_buf: list[str] = []

    def _start_card(self) -> None:
        self._current = {
            "dp_number": 0,
            "title": "",
            "authors_raw": "",
            "abstract_raw": "",
            "date_raw": "",
            "url": "",
        }
        self._depth = 0
        self._buf = []
        self._title_anchor_depth = None
        self._title_buf = []

    def _finalize_card(self) -> None:
        if self._current is None:
            return

        # derive authors from the article's residual text: full card text
        # minus the DP-number badge minus the title
        full = _strip_html("".join(self._buf))
        title = re.sub(r"\s+", " ", "".join(self._title_buf)).strip()
        self._current["title"] = title

        residual = _RE_DP_TEXT.sub("", full)
        residual = re.sub(
            r"IZA\s+Discussion\s+Paper\s+No\.?", "", residual, flags=re.IGNORECASE
        )
        if title:
            residual = residual.replace(title, "")
        self._current["authors_raw"] = residual

        dp_number = int(self._current.get("dp_number") or 0)
        url = (self._current.get("url") or "").strip()
        if not dp_number and url:
            m = _RE_DP_HREF.search(url)
            if m:
                dp_number = int(m.group(1))
                self._current["dp_number"] = dp_number

        if dp_number and title:
            if not url:
                url = f"{_BASE_URL}/publications/dp/{dp_number}"
                self._current["url"] = url
            self.items.append(self._current)

        self._current = None
        self._depth = 0
        self._buf = []
        self._title_anchor_depth = None
        self._title_buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = dict(attrs)

        # each paper is one <article>; start a fresh card on its open tag
        if tag_l == "article":
            self._start_card()
            return

        if self._current is not None:
            # track nesting so we know when the title anchor closes
            self._depth += 1
            # a space keeps adjacent text tokens from fusing across tags
            self._buf.append(" ")

            if tag_l == "a":
                href = attr_map.get("href") or ""
                m = _RE_DP_HREF.search(href)
                if m:
                    self._current["url"] = urljoin(_BASE_URL, href)
                    if not self._current.get("dp_number"):
                        self._current["dp_number"] = int(m.group(1))
                    # the first DP anchor wraps the title; capture its text
                    if self._title_anchor_depth is None:
                        self._title_anchor_depth = self._depth
                        self._title_buf = []
            return

        # outside any article: arm the degraded title-only anchor fallback
        if tag_l == "a":
            href = attr_map.get("href") or ""
            if _RE_DP_HREF.search(href):
                self._in_fallback_anchor = True
                self._fallback_href = urljoin(_BASE_URL, href)
                self._fallback_buf = []

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()

        if tag_l == "article" and self._current is not None:
            self._finalize_card()
            return

        if self._current is not None:
            # close the title anchor when it returns to its opening depth
            if (
                self._title_anchor_depth is not None
                and tag_l == "a"
                and self._depth == self._title_anchor_depth
            ):
                self._title_anchor_depth = None
            if self._depth > 0:
                self._depth -= 1
            return

        if tag_l == "a" and self._in_fallback_anchor:
            self._in_fallback_anchor = False
            title = re.sub(r"\s+", " ", "".join(self._fallback_buf)).strip()
            m = _RE_DP_HREF.search(self._fallback_href)
            if m and title:
                self.fallback_items.append(
                    {
                        "dp_number": int(m.group(1)),
                        "title": title,
                        "authors_raw": "",
                        "abstract_raw": "",
                        "date_raw": "",
                        "url": self._fallback_href,
                    }
                )
            self._fallback_href = ""
            self._fallback_buf = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._buf.append(data)
            if self._title_anchor_depth is not None:
                self._title_buf.append(data)
        elif self._in_fallback_anchor:
            self._fallback_buf.append(data)

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        if self._current is not None:
            self._buf.append(f"&{name};")
            if self._title_anchor_depth is not None:
                self._title_buf.append(f"&{name};")
        elif self._in_fallback_anchor:
            self._fallback_buf.append(f"&{name};")

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        if self._current is not None:
            self._buf.append(f"&#{name};")
            if self._title_anchor_depth is not None:
                self._title_buf.append(f"&#{name};")
        elif self._in_fallback_anchor:
            self._fallback_buf.append(f"&#{name};")


def _parse_detail_page(html: str) -> dict:
    """Parse abstract, date, JEL codes, DOI, and fallback authors."""
    result: dict = {}

    for pattern in (
        # IZA's full abstract lives in the cms-richtext copy block
        r'class="[^"]*cms-richtext[^"]*"[^>]*>(.*?)</div>',
        r'class="[^"]*abstract[^"]*"[^>]*>(.*?)</(?:div|section|p)',
        r'id="[^"]*abstract[^"]*"[^>]*>(.*?)</(?:div|section|p)',
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            abstract = _strip_html(m.group(1))
            if len(abstract) > 60:
                result["abstract"] = abstract[:5000]
                break

    if "abstract" not in result:
        for meta_pattern in (
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
        ):
            m = re.search(meta_pattern, html, re.IGNORECASE | re.DOTALL)
            if not m:
                continue
            abstract = _strip_html(m.group(1))
            if len(abstract) > 60:
                result["abstract"] = abstract[:5000]
                break

    date_candidates: list[str] = []
    for pattern in (
        r'<meta[^>]+(?:name|property)=["\'](?:citation_publication_date|dc\.date|article:published_time|date)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:citation_publication_date|dc\.date|article:published_time|date)["\']',
        r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
    ):
        for match in re.finditer(pattern, html, re.IGNORECASE):
            date_candidates.append(match.group(1))

    for candidate in date_candidates:
        parsed = _parse_date(candidate)
        if parsed:
            result["published_at"] = parsed
            break

    jel_codes: list[str] = []
    seen_jel: set[str] = set()
    for match in re.finditer(
        r"search=jel(?:&amp;|&)?term=([A-Z][0-9]{1,2})",
        html,
        re.IGNORECASE,
    ):
        code = match.group(1).upper()
        if code not in seen_jel:
            seen_jel.add(code)
            jel_codes.append(code)

    stripped = _strip_html(html)
    for match in _RE_JEL_LABEL.finditer(stripped):
        for code in _RE_JEL_CODE.findall(match.group(1).upper()):
            if code not in seen_jel:
                seen_jel.add(code)
                jel_codes.append(code)
    if jel_codes:
        result["jel_codes"] = jel_codes

    doi_match = re.search(r"https?://(?:dx\.)?doi\.org/([^\"'\s<]+)", html, re.IGNORECASE)
    if doi_match:
        result["doi"] = doi_match.group(1).rstrip(").,;")
    else:
        doi_match2 = _RE_DOI.search(html)
        if doi_match2:
            result["doi"] = doi_match2.group(0).rstrip(").,;")

    # IZA detail pages have no author class/meta; names live only in
    # /person/NNNNN anchors. This is a fallback -- listing authors are primary.
    person_names = re.findall(
        r"<a [^>]*href=['\"][^'\"]*/person/\d+[^'\"]*['\"][^>]*>([^<]+)</a>",
        html,
        re.IGNORECASE,
    )
    if person_names:
        authors = _parse_authors(", ".join(person_names))
        if authors:
            result["authors"] = authors

    if "authors" not in result:
        for pattern in (
            r'class="[^"]*authors?[^"]*"[^>]*>(.*?)</(?:div|p|span)',
            r'<meta[^>]+name=["\']author["\'][^>]+content=["\'](.*?)["\']',
        ):
            m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if not m:
                continue
            authors = _parse_authors(m.group(1))
            if authors:
                result["authors"] = authors
                break

    return result


def _load_state() -> dict:
    """Load watches/papers/state.json."""
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically save watches/papers/state.json."""
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


class IZASensor(BaseSensor):
    """Scrape IZA discussion paper listings."""

    name = "iza"
    watch = "papers"
    rate_limit = 0.5

    def _from_date(self) -> str:
        """Return lower-bound date for ingestion."""
        state = _load_state()
        last_fetched = (state.get("iza") or {}).get("last_fetched")
        if isinstance(last_fetched, str):
            try:
                datetime.fromisoformat(last_fetched)
                return last_fetched
            except ValueError:
                pass
        return (
            datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

    def _last_dp(self) -> int:
        """Return last seen DP number from state."""
        state = _load_state()
        return int((state.get("iza") or {}).get("last_dp", 0))

    def _update_state(self, max_dp: int) -> None:
        """Update iza state after a successful run."""
        state = _load_state()
        iza_state = state.get("iza") or {}
        iza_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if max_dp > 0:
            iza_state["last_dp"] = max(max_dp, int(iza_state.get("last_dp", 0)))
        state["iza"] = iza_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[iza] failed to save state: {exc}", file=sys.stderr)

    def _listing_url(self, page: int) -> str:
        """Build listing URL for a 1-indexed page."""
        return f"{_LISTING_URL}?page={page}&sortDirection=DESC&sortField=date"

    def _fetch_listing_page(self, page: int) -> list[dict]:
        """Fetch and parse one listing page."""
        url = self._listing_url(page)
        try:
            raw = self.fetch_url(
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[iza] failed to fetch listing page {page}: {exc}", file=sys.stderr)
            return []

        parser = _ListingParser()
        try:
            parser.feed(html)
        except Exception as exc:
            print(f"[iza] failed to parse listing page {page}: {exc}", file=sys.stderr)
            return []

        merged: list[dict] = []
        seen_dp: set[int] = set()
        for item in parser.items + parser.fallback_items:
            dp = int(item.get("dp_number") or 0)
            if dp <= 0 or dp in seen_dp:
                continue
            seen_dp.add(dp)
            merged.append(item)
        return merged

    def _fetch_detail_page(self, dp_number: int, url: str) -> dict:
        """Fetch and parse one detail page."""
        try:
            raw = self.fetch_url(
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[iza] failed to fetch detail DP {dp_number}: {exc}", file=sys.stderr)
            return {}

        try:
            return _parse_detail_page(html)
        except Exception as exc:
            print(f"[iza] failed to parse detail DP {dp_number}: {exc}", file=sys.stderr)
            return {}

    def collect(self) -> list[dict]:
        """Collect new IZA papers."""
        from_date = self._from_date()
        last_dp = self._last_dp()
        print(
            f"[iza] collecting papers from {from_date} (last_dp={last_dp})",
            file=sys.stderr,
        )

        papers: list[dict] = []
        seen_source_ids: set[str] = set()
        detail_budget = _DETAIL_BUDGET
        max_dp_seen = last_dp
        fetched_any_page = False

        try:
            for page in range(1, _MAX_PAGES + 1):
                cards = self._fetch_listing_page(page)
                if not cards:
                    if page == 1:
                        print("[iza] no cards from first listing page", file=sys.stderr)
                    break

                fetched_any_page = True
                page_added = 0
                page_old = 0

                for card in cards:
                    dp_number = int(card.get("dp_number") or 0)
                    title = (card.get("title") or "").strip()
                    if dp_number <= 0 or not title:
                        continue

                    source_id = str(dp_number)
                    if source_id in seen_source_ids:
                        continue
                    seen_source_ids.add(source_id)
                    max_dp_seen = max(max_dp_seen, dp_number)

                    # skip already-ingested papers before spending detail budget;
                    # still count them so the page_old early-break can fire
                    if dp_number <= last_dp:
                        page_old += 1
                        continue

                    source_url = (card.get("url") or f"{_BASE_URL}/publications/dp/{dp_number}").strip()
                    source_url = urljoin(_BASE_URL, source_url)

                    authors = _parse_authors(card.get("authors_raw") or "")
                    abstract = _strip_html(card.get("abstract_raw") or "") or None
                    published_at = _parse_date(card.get("date_raw") or "")
                    jel_codes: list[str] = []
                    doi: str | None = None
                    detail: dict = {}

                    need_detail = detail_budget > 0 and (not authors or not abstract or not published_at)
                    if need_detail:
                        detail = self._fetch_detail_page(dp_number, source_url)
                        detail_budget -= 1

                    if not authors and detail.get("authors"):
                        authors = detail["authors"]
                    if not abstract and detail.get("abstract"):
                        abstract = detail["abstract"]
                    if not published_at and detail.get("published_at"):
                        published_at = detail["published_at"]
                    if detail.get("jel_codes"):
                        jel_codes = detail["jel_codes"]
                    if detail.get("doi"):
                        doi = detail["doi"]

                    # state-based skip already handled above; only date remains
                    is_old_by_date = bool(published_at and published_at < from_date)
                    if is_old_by_date:
                        page_old += 1
                        continue

                    papers.append(
                        {
                            "title": title,
                            "authors": authors,
                            "abstract": abstract,
                            "doi": doi,
                            "url": source_url,
                            "published_at": published_at,
                            "paper_type": "working_paper",
                            "jel_codes": jel_codes,
                            "keywords": list(_BASE_KEYWORDS),
                            "source_id": source_id,
                            "source_url": source_url,
                            "raw_metadata": {
                                "dp_number": dp_number,
                                "date_raw": card.get("date_raw") or "",
                                "authors_raw": card.get("authors_raw") or "",
                                "listing_url": self._listing_url(page),
                            },
                        }
                    )
                    page_added += 1

                print(
                    f"[iza] page={page} cards={len(cards)} added={page_added} old={page_old} detail_budget={detail_budget}",
                    file=sys.stderr,
                )

                if len(cards) < _PAPERS_PER_PAGE:
                    break
                if page_added == 0 and page_old > 0:
                    break

        except Exception as exc:
            print(f"[iza] collect failed: {exc}", file=sys.stderr)
            return []

        if fetched_any_page:
            self._update_state(max_dp_seen)

        print(f"[iza] collected {len(papers)} papers", file=sys.stderr)
        return papers


if __name__ == "__main__":
    from econsignals.lib.db import init_db
    init_db()
    sensor = IZASensor()
    sensor.run()
