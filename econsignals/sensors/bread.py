"""BREAD (Bureau for Research and Economic Analysis of Development) working papers sensor.

Scrapes the BREAD working papers listing at ibread.org/working-papers for recent
development economics papers. BREAD has no public API or stable RSS feed.

The BREAD site is a Google Sites page whose content is server-side rendered into
the initial HTML response. Each paper entry is anchored to a GitHub-hosted PDF link
with the format:
    https://github.com/GunnRobert/BREAD-Working-Papers/blob/<sha>/WP NNN - Title.pdf

Immediately after each PDF link, the HTML contains the paper metadata block:
    Date: <season year>
    WP#: <number>
    Authors: <comma-separated names>
    Abstract: <text>

This sensor parses that structure using html.parser and regex. No detail-page
fetches are needed because the listing page itself contains all metadata.

State is persisted under the "bread" key in watches/papers/state.json,
tracking the last successfully fetched date to avoid re-ingesting old papers.

Usage:
    python sensors/bread.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote

from econsignals.sensors._base import BaseSensor, PROJ_ROOT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LISTING_URL = "https://ibread.org/working-papers"
_DEFAULT_LOOKBACK_DAYS = 60   # BREAD publishes infrequently (~4-5 batches/year)
_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"
_BASE_KEYWORDS: list[str] = ["development economics", "BREAD"]

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# GitHub PDF links for BREAD papers
_RE_PDF_LINK = re.compile(
    r'href="(https://github\.com/GunnRobert/BREAD-Working-Papers/blob/[^"]+\.pdf)"',
    re.IGNORECASE,
)

# WP number from the decoded PDF filename (e.g. "WP 647 - ...")
_RE_WP_IN_URL = re.compile(r"WP\s*(\d{3,4})", re.IGNORECASE)

# Season + year (e.g. "Fall 2025", "Spring 2025", "Winter 2024")
_RE_SEASON_YEAR = re.compile(
    r"\b(Fall|Spring|Summer|Winter|Autumn)\s+(20\d{2}|19\d{2})\b",
    re.IGNORECASE,
)
_RE_MONTH_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[\s,]+(20\d{2}|19\d{2})\b",
    re.IGNORECASE,
)
_RE_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_RE_YEAR_ONLY = re.compile(r"\b(20\d{2}|19\d{2})\b")

_SEASON_MONTH: dict[str, int] = {
    "spring": 3,
    "summer": 6,
    "fall": 9,
    "autumn": 9,
    "winter": 12,
}
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

# Field labels inside the metadata block that follows each PDF link
_RE_DATE_FIELD = re.compile(
    r"Date\s*:\s*(.+?)(?=WP#|Authors?:|Abstract:|$)",
    re.IGNORECASE | re.DOTALL,
)
_RE_WP_FIELD = re.compile(r"WP\s*#?\s*[:\s]\s*(\d{3,4})", re.IGNORECASE)
_RE_AUTHORS_FIELD = re.compile(
    r"Authors?\s*:\s*(.+?)(?=Abstract\s*:|$)",
    re.IGNORECASE | re.DOTALL,
)
_RE_ABSTRACT_FIELD = re.compile(r"Abstract\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)

# JEL codes
_RE_JEL_LABEL = re.compile(
    r"\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)", re.IGNORECASE
)
_RE_JEL_CODE = re.compile(r"[A-Z][0-9]{1,2}")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _strip_html(fragment: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace.

    Args:
        fragment: Raw HTML string.

    Returns:
        Plain-text string with normalised whitespace.
    """
    if not fragment:
        return ""
    fragment = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        " ",
        fragment,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str | None) -> str | None:
    """Convert a raw date string to ISO YYYY-MM-DD, or return None.

    Handles ISO dates, month+year, season+year, and bare four-digit years.

    Args:
        raw: Raw date string (e.g. "Fall 2025", "March 2024", "2024-03-15").

    Returns:
        'YYYY-MM-DD' string, or None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()

    m = _RE_ISO_DATE.search(raw)
    if m:
        return m.group(1)

    m2 = _RE_MONTH_YEAR.search(raw)
    if m2:
        month = _MONTH_MAP.get(m2.group(1).lower())
        year = int(m2.group(2))
        if month and 1900 <= year <= 2100:
            return f"{year:04d}-{month:02d}-01"

    m3 = _RE_SEASON_YEAR.search(raw)
    if m3:
        month = _SEASON_MONTH.get(m3.group(1).lower(), 1)
        year = int(m3.group(2))
        if 1900 <= year <= 2100:
            return f"{year:04d}-{month:02d}-01"

    m4 = _RE_YEAR_ONLY.search(raw)
    if m4:
        year = int(m4.group(1))
        if 1990 <= year <= 2100:
            return f"{year}-01-01"

    return None


def _parse_authors(raw: str) -> list[str]:
    """Split a comma/semicolon/ampersand-delimited author string into a list.

    Args:
        raw: Raw author text, e.g. "Gharad Bryan, Simon Franklin and Sarah Winton".

    Returns:
        List of individual author name strings.
    """
    if not raw:
        return []
    normalised = re.sub(r"\band\b", ",", raw, flags=re.IGNORECASE)
    normalised = normalised.replace(";", ",").replace("&", ",")
    parts = [p.strip() for p in normalised.split(",") if p.strip()]
    return [p for p in parts if 2 < len(p) < 100]


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL classification codes from plain text.

    Args:
        text: Plain text to search.

    Returns:
        Deduplicated list of JEL code strings.
    """
    codes: list[str] = []
    seen: set[str] = set()
    for m in _RE_JEL_LABEL.finditer(text):
        for code in _RE_JEL_CODE.findall(m.group(1)):
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# Anchor text extractor
# ---------------------------------------------------------------------------


class _AnchorTextParser(HTMLParser):
    """Extract plain text from an anchor tag's inner HTML.

    Google Sites wraps anchor text in nested <span> elements. This parser
    collects raw character data, ignoring all tags.

    Attributes:
        text: Accumulated plain text.
    """

    def __init__(self) -> None:
        super().__init__()
        self.text: str = ""

    def handle_data(self, data: str) -> None:
        self.text += data

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        self.text += f"&{name};"

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        self.text += f"&#{name};"

    @staticmethod
    def extract(html_fragment: str) -> str:
        """Return plain text from an HTML fragment.

        Args:
            html_fragment: Raw inner HTML of an anchor tag.

        Returns:
            Plain text with normalised whitespace and unescaped entities.
        """
        p = _AnchorTextParser()
        try:
            p.feed(html_fragment)
        except Exception:
            pass
        return unescape(re.sub(r"\s+", " ", p.text).strip())


# ---------------------------------------------------------------------------
# Core listing parser
# ---------------------------------------------------------------------------


def _parse_bread_listing(html: str) -> list[dict]:
    """Extract all paper entries from the BREAD Google Sites listing HTML.

    The BREAD working papers page renders as static HTML (Google Sites SSR).
    Each paper is represented by:
      1. A GitHub PDF anchor tag containing the title as its inner text.
      2. A subsequent metadata block with labeled fields:
         Date, WP#, Authors, Abstract.

    This function finds all PDF anchors matching the BREAD GitHub pattern,
    then slices the HTML between consecutive anchors to extract the metadata
    block that follows each link.

    Args:
        html: Decoded HTML content of the BREAD working papers listing page.

    Returns:
        List of dicts, one per paper, with keys:
            wp_number (str | None), title (str), pdf_url (str),
            date_raw (str), authors_raw (str), abstract (str | None).
        Returns empty list if no papers are found.
    """
    entries: list[dict] = []
    link_matches = list(_RE_PDF_LINK.finditer(html))
    if not link_matches:
        return []

    for i, m in enumerate(link_matches):
        pdf_url = m.group(1)
        decoded_url = unquote(pdf_url)

        # WP number from decoded URL filename
        wp_match = _RE_WP_IN_URL.search(decoded_url)
        wp_number_from_url: str | None = wp_match.group(1) if wp_match else None

        # Title: inner HTML of this <a> tag
        tag_start = m.start()
        anchor_open_end = html.find(">", tag_start)
        if anchor_open_end == -1:
            continue
        anchor_close = html.find("</a>", anchor_open_end)
        if anchor_close == -1:
            continue
        inner_html = html[anchor_open_end + 1 : anchor_close]
        title = _AnchorTextParser.extract(inner_html)
        if not title:
            # Fallback: derive title from URL filename
            fn = decoded_url.rsplit("/", 1)[-1]
            fn = re.sub(r"^WP\s*\d+\s*[-\u2013]\s*", "", fn)
            title = fn.replace(".pdf", "").strip()

        # Metadata block: between this anchor's closing </a> and the next link
        meta_start = anchor_close + len("</a>")
        meta_end = (
            link_matches[i + 1].start() if i + 1 < len(link_matches) else len(html)
        )
        meta_text = _strip_html(html[meta_start:meta_end])

        # Extract labeled fields
        date_raw = ""
        m_date = _RE_DATE_FIELD.search(meta_text)
        if m_date:
            date_raw = m_date.group(1).strip()[:60]

        wp_number_from_block: str | None = None
        m_wp = _RE_WP_FIELD.search(meta_text)
        if m_wp:
            wp_number_from_block = m_wp.group(1).strip()

        wp_number = wp_number_from_url or wp_number_from_block

        authors_raw = ""
        m_authors = _RE_AUTHORS_FIELD.search(meta_text)
        if m_authors:
            authors_raw = m_authors.group(1).strip()
            # Guard against abstract leaking into authors field
            authors_raw = re.sub(
                r"\s*Abstract\s*:.*", "", authors_raw, flags=re.IGNORECASE
            ).strip()
            authors_raw = authors_raw[:300]

        abstract: str | None = None
        m_abs = _RE_ABSTRACT_FIELD.search(meta_text)
        if m_abs:
            candidate = m_abs.group(1).strip()
            if len(candidate) > 40:
                abstract = candidate[:4000]

        entries.append(
            {
                "wp_number": wp_number,
                "title": title,
                "pdf_url": pdf_url,
                "date_raw": date_raw,
                "authors_raw": authors_raw,
                "abstract": abstract,
            }
        )

    return entries


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/papers/state.json, returning empty dict on failure.

    Returns:
        Full state dict, or empty dict if the file is absent or corrupt.
    """
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically write state dict to watches/papers/state.json.

    Args:
        state: Dict to serialise as JSON.
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
# Sensor
# ---------------------------------------------------------------------------


class BreadSensor(BaseSensor):
    """Collect BREAD working papers from ibread.org.

    BREAD (Bureau for Research and Economic Analysis of Development) is a
    selective network of development economists. The site publishes papers
    in seasonal batches on a Google Sites page. This sensor scrapes that
    page and extracts structured metadata from the embedded HTML.

    All metadata (title, authors, date, WP number, abstract) is available
    in the listing page itself -- no per-paper detail fetches are needed.

    Attributes:
        name: Sensor identifier ('bread').
        watch: Watch category ('papers').
        rate_limit: 0.5 req/s -- one request every 2 seconds.
    """

    name = "bread"
    watch = "papers"
    rate_limit = 0.5  # one request every 2 seconds

    LISTING_URL = _LISTING_URL

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return the ISO date lower-bound for filtering papers by date.

        Reads bread.last_fetched from watches/papers/state.json. Falls back
        to _DEFAULT_LOOKBACK_DAYS days ago on first run.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = (state.get("bread") or {}).get("last_fetched")
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
        """Record today as the last successful BREAD fetch in state."""
        state = _load_state()
        bread_state = state.get("bread") or {}
        bread_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state["bread"] = bread_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[bread] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Scrape BREAD working papers and return standard paper dicts.

        Steps:
        1. Determine from_date via watch state (default: 60 days ago).
        2. Fetch the single BREAD working papers listing page.
        3. Parse all PDF anchor tags and their following metadata blocks.
        4. Filter out papers older than from_date when date is known.
        5. Build the standard paper dict for each item.
        6. Update watch state on success.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        from_date = self._from_date()
        print(
            f"[bread] collecting papers on or after {from_date}", file=sys.stderr
        )

        try:
            raw = self.fetch_url(self.LISTING_URL, timeout=30)
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[bread] failed to fetch listing: {exc}", file=sys.stderr)
            return []

        raw_entries = _parse_bread_listing(html)
        print(
            f"[bread] found {len(raw_entries)} paper entries in listing",
            file=sys.stderr,
        )

        papers: list[dict] = []
        seen_ids: set[str] = set()

        for entry in raw_entries:
            title = entry["title"].strip()
            if not title:
                continue

            wp_number = entry.get("wp_number")
            pdf_url = entry["pdf_url"]
            date_raw = entry.get("date_raw", "")
            authors_raw = entry.get("authors_raw", "")
            abstract = entry.get("abstract")

            published_at = _parse_date(date_raw)

            # Skip papers older than from_date when date is known
            if published_at and published_at < from_date:
                print(
                    f"[bread] skipping old paper ({published_at}): "
                    f"WP {wp_number} {title[:50]}",
                    file=sys.stderr,
                )
                continue

            authors = _parse_authors(authors_raw)
            jel_codes = _extract_jel_codes(abstract or "") or _extract_jel_codes(title)

            # source_id: prefer WP number, fall back to URL hash
            if wp_number:
                source_id = f"bread:wp{wp_number}"
            else:
                source_id = f"bread:{hashlib.sha1(pdf_url.encode()).hexdigest()[:8]}"

            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)

            papers.append(
                {
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "doi": None,
                    "url": pdf_url,
                    "published_at": published_at,
                    "paper_type": "working_paper",
                    "jel_codes": jel_codes,
                    "keywords": list(_BASE_KEYWORDS),
                    "source_id": source_id,
                    "source_url": pdf_url,
                    "raw_metadata": {
                        "source": "bread_html_scrape",
                        "wp_number": wp_number,
                        "pdf_url": pdf_url,
                        "date_raw": date_raw,
                        "authors_raw": authors_raw,
                    },
                }
            )

        self._update_state()
        print(f"[bread] collected {len(papers)} papers", file=sys.stderr)
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = BreadSensor()
    sensor.run()
