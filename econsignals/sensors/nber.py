"""NBER working-papers sensor.

The National Bureau of Economic Research is the single highest-prestige source
of U.S. economics working papers (relevance prestige 1.0): nearly every major
empirical and theoretical contribution circulates as an NBER WP months before
journal publication. Tracking the NBER new-papers stream surfaces frontier work
in development, public, labour, and urban economics before it reaches the
broader corpus, which is exactly where this agent's profile sits.

Source feed (verified live, 2026-05): the documented JSON listing endpoint
``/api/v1/working_page_listing/...`` no longer returns JSON -- it 301-redirects
to a styleformat HTML page -- so this sensor parses the canonical RSS 2.0 feed
instead:

    https://back.nber.org/rss/new.xml

(``https://www.nber.org/rss/new.xml`` meta-refreshes to that host.) The feed
carries the latest ~35 working papers, one ``<item>`` per paper:

    title        "<Paper Title> -- by Author One, Author Two, ..."
    description  abstract
    link, guid   https://www.nber.org/papers/w35164#fromrss

The RSS carries no publication date and no separate author or JEL field, so
the date-based lookback the source policy expects is not satisfiable from the
feed alone. To recover a real date (and a DOI, which sharpens cross-source
dedup since NBER WPs later resurface elsewhere), this sensor does one
lightweight follow-up GET per paper to the paper's landing page and reads two
Google-Scholar citation meta tags present on every page:

    <meta name="citation_publication_date" content="2026/05/25" />
    <meta name="citation_doi" content="10.3386/w35164" />

The follow-up is bounded (the feed lists ~35 papers) and rate-limited. If a
page fetch fails or omits the date, the paper still ingests (published_at and
doi left None); only papers whose recovered date is older than the lookback
window are dropped. NBER exposes no JEL codes on these pages, so jel_codes is
always empty.

Override the lookback with ECONSIGNALS_NBER_LOOKBACK_DAYS (default 30). Set it
to 0 to keep every paper in the feed regardless of date. Disable the per-paper
page fetch (faster, but no dates or DOIs) with ECONSIGNALS_NBER_FETCH_PAGES=0.

Usage:
    python -m econsignals.sensors.nber
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys

from econsignals.sensors._base import BaseSensor

# Parse the RSS feed with defusedxml when available (hardened against XXE and
# billion-laughs), falling back to the stdlib parser. The NBER feed carries no
# DTD, so defusedxml's entity/DTD rejection does not affect normal parsing.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:  # pragma: no cover - defusedxml is the hardened default
    from xml.etree.ElementTree import fromstring as _xml_fromstring

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical feed host. www.nber.org/rss/new.xml meta-refreshes here; back.* is
# the backend that actually serves the RSS 2.0 body.
_FEED_URL = "https://back.nber.org/rss/new.xml"

# Per-paper landing page (carries citation_* meta tags with date and DOI).
_PAPER_URL_TMPL = "https://www.nber.org/papers/{number}"

_DEFAULT_LOOKBACK_DAYS = 30

# Splits an RSS item title into "<title> -- by <authors>". NBER uses a literal
# " -- by " separator; tolerate flexible surrounding whitespace.
_RE_TITLE_BY = re.compile(r"\s+--\s*by\s+", re.IGNORECASE)

# Splits an author run ("A, B, C and D") into individual names.
_RE_AUTHOR_SEP = re.compile(r",|\band\b", re.IGNORECASE)

# NBER working-paper number embedded in the landing-page URL, e.g. ".../papers/w35164".
_RE_PAPER_NUMBER = re.compile(r"/papers/([a-z]?\d+)", re.IGNORECASE)

# Google-Scholar citation meta tags on every paper landing page.
_RE_CITATION_DATE = re.compile(
    r'<meta\s+name="citation_publication_date"\s+content="([^"]+)"', re.IGNORECASE
)
_RE_CITATION_DOI = re.compile(
    r'<meta\s+name="citation_doi"\s+content="([^"]+)"', re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Helpers (pure functions, unit-testable without network or DB)
# ---------------------------------------------------------------------------


def _split_title_authors(raw_title: str) -> tuple[str, list[str]]:
    """Split an NBER RSS item title into the paper title and its authors.

    NBER encodes authors in the title as ``<title> -- by <A>, <B> and <C>``.

    Args:
        raw_title: Raw ``<title>`` text from an RSS item.

    Returns:
        A (title, authors) tuple. If no ``-- by`` separator is present, the
        whole string is the title and authors is empty.
    """
    parts = _RE_TITLE_BY.split(raw_title.strip(), maxsplit=1)
    title = parts[0].strip()
    if len(parts) < 2:
        return title, []
    authors = [a.strip() for a in _RE_AUTHOR_SEP.split(parts[1]) if a.strip()]
    return title, authors


def _extract_paper_number(url: str) -> str | None:
    """Return the NBER paper number embedded in a landing-page URL, or None.

    Args:
        url: A ``.../papers/w35164#fromrss`` style URL.

    Returns:
        The paper number (e.g. "w35164"), or None.
    """
    if not url:
        return None
    m = _RE_PAPER_NUMBER.search(url)
    return m.group(1).lower() if m else None


def _parse_citation_date(html: str) -> str | None:
    """Extract the citation_publication_date as ISO YYYY-MM-DD, or None.

    NBER pages format the date as ``YYYY/MM/DD``; occasionally a bare year or
    year-month appears, mapped to the first of the period.

    Args:
        html: Paper landing-page HTML.

    Returns:
        'YYYY-MM-DD' string, or None if absent or unparseable.
    """
    m = _RE_CITATION_DATE.search(html or "")
    if not m:
        return None
    raw = m.group(1).strip().replace("/", "-")
    parts = raw.split("-")
    try:
        if len(parts) == 3:
            return dt.date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
        if len(parts) == 2:
            return dt.date(int(parts[0]), int(parts[1]), 1).isoformat()
        if len(parts) == 1 and len(parts[0]) == 4:
            return dt.date(int(parts[0]), 1, 1).isoformat()
    except ValueError:
        return None
    return None


def _extract_doi(html: str) -> str | None:
    """Extract the citation_doi from paper landing-page HTML, or None.

    Args:
        html: Paper landing-page HTML.

    Returns:
        The DOI string (e.g. "10.3386/w35164"), or None.
    """
    m = _RE_CITATION_DOI.search(html or "")
    return m.group(1).strip() if m else None


def _parse_feed(xml_text: str) -> list[dict]:
    """Parse the NBER RSS feed into partial paper dicts.

    The returned dicts carry everything available from the feed; published_at
    and doi are left None and filled later from each paper's landing page.

    Args:
        xml_text: Decoded RSS 2.0 feed body.

    Returns:
        List of partial paper dicts. Returns an empty list on a parse error.
    """
    try:
        root = _xml_fromstring(xml_text)
    except Exception as exc:
        # A malformed or hostile feed must abort safely, never crash the run.
        print(f"[nber] XML parse rejected: {exc}", file=sys.stderr)
        return []

    papers: list[dict] = []
    for item in root.findall(".//item"):
        raw_title = (item.findtext("title") or "").strip()
        if not raw_title:
            continue

        title, authors = _split_title_authors(raw_title)
        if not title:
            continue

        link = (item.findtext("link") or "").strip()
        if not link:
            link = (item.findtext("guid") or "").strip()
        # Drop the RSS-tracking fragment so the stored URL is canonical.
        url = link.split("#", 1)[0] or None

        number = _extract_paper_number(link)
        if not number:
            # Without a paper number there is no stable source_id; skip.
            continue

        abstract_raw = (item.findtext("description") or "").strip()

        papers.append(
            {
                "title": title,
                "authors": authors,
                "abstract": abstract_raw or None,
                "doi": None,
                "url": url,
                "published_at": None,
                "paper_type": "working_paper",
                "jel_codes": [],
                "keywords": [],
                "source_id": number,
                "source_url": url,
                "raw_metadata": {
                    "source": "nber",
                    "nber_number": number,
                },
            }
        )

    return papers


def _within_lookback(published_at: str | None, lookback_days: int) -> bool:
    """Return whether a paper is recent enough to keep.

    A paper with no recovered date is always kept (we cannot prove it is old).
    A lookback of 0 disables date filtering entirely.

    Args:
        published_at: ISO 'YYYY-MM-DD' date or None.
        lookback_days: Window size in days; 0 means keep everything.

    Returns:
        True if the paper should be kept.
    """
    if lookback_days <= 0 or not published_at:
        return True
    try:
        published = dt.date.fromisoformat(published_at)
    except ValueError:
        return True
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    return published >= cutoff


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class NberSensor(BaseSensor):
    """Collect the latest NBER working papers from the new-papers RSS feed.

    Fetches the RSS feed, parses each item into a paper dict, then enriches
    each with the publication date and DOI from its landing page (unless
    disabled). Papers older than the lookback window are dropped.

    Attributes:
        name: Sensor identifier ('nber'), matching the prestige table (1.0).
        watch: Watch category ('papers').
        rate_limit: 1 req/s -- polite to NBER given the per-paper page fetches.
    """

    name = "nber"
    watch = "papers"
    rate_limit = 1.0

    def _lookback_days(self) -> int:
        """Return the lookback window in days from env, default 30."""
        raw = os.environ.get("ECONSIGNALS_NBER_LOOKBACK_DAYS", "").strip()
        if not raw:
            return _DEFAULT_LOOKBACK_DAYS
        try:
            return max(0, int(raw))
        except ValueError:
            return _DEFAULT_LOOKBACK_DAYS

    def _fetch_pages_enabled(self) -> bool:
        """Return whether to fetch per-paper landing pages for date/DOI."""
        return os.environ.get("ECONSIGNALS_NBER_FETCH_PAGES", "1").strip() != "0"

    def _enrich(self, paper: dict) -> None:
        """Fill published_at and doi from the paper's landing page, in place.

        A failed fetch or missing meta tag leaves the fields as None; the paper
        still ingests. Logs and continues on any per-paper error.

        Args:
            paper: A partial paper dict from _parse_feed, mutated in place.
        """
        number = paper["source_id"]
        page_url = _PAPER_URL_TMPL.format(number=number)
        try:
            raw = self.fetch_url(page_url, timeout=30)
        except Exception as exc:
            print(f"[nber] {number}: page fetch failed: {exc}", file=sys.stderr)
            return

        html = raw.decode("utf-8", errors="replace")
        paper["published_at"] = _parse_citation_date(html)
        paper["doi"] = _extract_doi(html)
        paper["raw_metadata"]["page_url"] = page_url

    def collect(self) -> list[dict]:
        """Fetch the NBER RSS feed, enrich each paper, and apply the lookback.

        Steps:
        1. Fetch and parse the new-papers RSS feed.
        2. For each paper, fetch its landing page to recover date and DOI
           (unless ECONSIGNALS_NBER_FETCH_PAGES=0).
        3. Drop papers older than the lookback window.

        Returns:
            List of paper dicts conforming to the BaseSensor.collect() contract.
        """
        lookback_days = self._lookback_days()
        fetch_pages = self._fetch_pages_enabled()
        print(
            f"[nber] fetching feed (lookback={lookback_days}d, "
            f"page_fetch={'on' if fetch_pages else 'off'})",
            file=sys.stderr,
        )

        try:
            raw = self.fetch_url(_FEED_URL, timeout=30)
        except Exception as exc:
            print(f"[nber] feed fetch failed: {exc}", file=sys.stderr)
            return []

        papers = _parse_feed(raw.decode("utf-8", errors="replace"))
        print(f"[nber] parsed {len(papers)} items from feed", file=sys.stderr)

        if fetch_pages:
            for paper in papers:
                self._enrich(paper)

        kept = [p for p in papers if _within_lookback(p["published_at"], lookback_days)]
        print(
            f"[nber] kept {len(kept)} of {len(papers)} papers "
            f"within {lookback_days}d lookback",
            file=sys.stderr,
        )
        return kept


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = NberSensor()
    sensor.run()
