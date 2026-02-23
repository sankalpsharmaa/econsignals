"""NEP (New Economics Papers) sensor for EconSignals.

NEP is a filtering service built on the RePEc database. It distributes
email digests organized by JEL field. Each report page lists recent
working papers with titles, authors, abstracts, and handles.

Report URLs follow the pattern: https://nep.repec.org/nep-{code}/

Monitored reports are chosen to match the user's JEL interests:
  nep-dev  Development Economics
  nep-ure  Urban & Real Estate Economics
  nep-lab  Labor Economics
  nep-pub  Public Finance
  nep-geo  Economic Geography
  nep-sea  South East Asia (closest proxy for India/South Asia)
  nep-exp  Experimental Economics

State (last-seen paper handles per report) is persisted in
PROJ_ROOT/watches/papers/state.json under the "nep_last_seen" key.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py / rss_feeds.py convention)
# ---------------------------------------------------------------------------
SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
PROJ_ROOT = SKILL_ROOT.parent.parent.parent  # econsignals project root

from sensors._base import BaseSensor  # noqa: E402

# ---------------------------------------------------------------------------
# Report registry
# ---------------------------------------------------------------------------

NEP_REPORTS: dict[str, str] = {
    "nep-dev": "Development Economics",
    "nep-ure": "Urban & Real Estate Economics",
    "nep-lab": "Labor Economics",
    "nep-pub": "Public Finance",
    "nep-geo": "Economic Geography",
    "nep-sea": "South East Asia",
    "nep-exp": "Experimental Economics",
}

_NEP_BASE_URL = "https://nep.repec.org"

# Default lookback when no prior state exists (days)
_DEFAULT_LOOKBACK_DAYS = 14

# State file location (shared with rss_feeds sensor)
_STATE_FILE = PROJ_ROOT / "watches" / "papers" / "state.json"

# Canonical base URL for converting RepEC handles to URLs
# Handle format: repec:xxx:yyy  ->  https://ideas.repec.org/p/xxx/yyy.html
_REPEC_HANDLE_RE = re.compile(r"\brepec:[a-z0-9]+:[a-z0-9_]+\b", re.IGNORECASE)
_IDEAS_BASE = "https://ideas.repec.org/p"


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

class _NEPPageParser(HTMLParser):
    """Parse a NEP report page into a list of paper dicts.

    NEP report pages follow a consistent structure. Each paper block starts
    with a numbered heading containing the title and is followed by:
    - A line listing authors
    - An abstract block
    - A links section containing the RepEC handle

    The parser is intentionally lenient: it extracts whatever fields are
    available and skips partial entries rather than crashing.

    Attributes:
        papers: List of extracted paper dicts accumulated during parsing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.papers: list[dict] = []

        # Current paper being assembled
        self._current: dict | None = None

        # State machine flags
        self._in_title = False
        self._in_abstract = False
        self._in_authors = False
        self._in_date = False
        self._abstract_depth = 0   # track nested tags inside abstract container
        self._title_depth = 0

        # Pending text accumulation
        self._title_buf: list[str] = []
        self._abstract_buf: list[str] = []
        self._authors_buf: list[str] = []
        self._date_buf: list[str] = []

        # Track nesting depth for <li> paper blocks
        self._in_li = False
        self._li_depth = 0

        # Track last seen <a href> for handle extraction
        self._last_href = ""

        # Report-level date: NEP pages show an issue date near the top
        self.issue_date: str = ""
        self._in_h3 = False
        self._h3_buf: list[str] = []

    # ------------------------------------------------------------------
    # HTMLParser overrides
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "") or ""
        href = attr_dict.get("href", "") or ""

        # Track anchor hrefs for handle/link extraction
        if tag == "a" and href:
            self._last_href = href
            # If we are accumulating a title, this anchor IS the title link
            if self._in_title:
                pass  # text will be captured via handle_data

        # NEP titles: <li> items under the paper list; the title is typically
        # wrapped in <b> or directly in an <a> inside the <li>
        # Strategy: detect <li> entries that contain paper metadata by looking
        # for a nested <b> tag that follows an anchor
        if tag == "li":
            self._flush_current()
            self._in_li = True
            self._li_depth = 0
            self._current = {"handle": "", "title": "", "authors": [], "abstract": "", "url": "", "date": ""}
            self._title_buf = []
            self._abstract_buf = []
            self._authors_buf = []

        if self._in_li:
            if tag == "b" and not self._in_title and not self._in_abstract:
                # <b> after <a> inside <li> = title
                self._in_title = True
                self._title_depth = 0
                self._title_buf = []
            elif tag in ("p", "blockquote") and "abstract" in cls.lower():
                self._in_abstract = True
                self._abstract_depth = 0
                self._abstract_buf = []
            elif tag == "p" and self._current and not self._current.get("title"):
                pass  # not yet in title, skip
            elif tag == "p" and self._in_abstract:
                self._abstract_depth += 1

        # H3 captures the issue date line at the top of the page
        if tag == "h3":
            self._in_h3 = True
            self._h3_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            text = " ".join(self._h3_buf).strip()
            if not self.issue_date:
                date = _extract_date_from_text(text)
                if date:
                    self.issue_date = date

        if tag == "b" and self._in_title:
            self._in_title = False
            if self._current is not None:
                raw_title = " ".join(self._title_buf).strip()
                # Strip leading numbering like "1." or "12."
                raw_title = re.sub(r"^\d+\.\s*", "", raw_title)
                if raw_title and not self._current.get("title"):
                    self._current["title"] = raw_title
                    # Capture the last seen href as candidate URL
                    if self._last_href and not self._current.get("url"):
                        self._current["url"] = self._last_href

        if tag in ("p", "blockquote") and self._in_abstract:
            if self._abstract_depth > 0:
                self._abstract_depth -= 1
            else:
                self._in_abstract = False
                if self._current is not None:
                    self._current["abstract"] = _clean_text(
                        " ".join(self._abstract_buf)
                    )

        if tag == "li" and self._in_li:
            self._in_li = False
            self._flush_current()

    def handle_data(self, data: str) -> None:
        if self._in_h3:
            self._h3_buf.append(data)
            return

        if not self._in_li or self._current is None:
            return

        if self._in_title:
            self._title_buf.append(data)
            return

        if self._in_abstract:
            self._abstract_buf.append(data)
            return

        # Authors line: typically plain text containing "By " prefix
        stripped = data.strip()
        if stripped.lower().startswith("by "):
            authors_raw = stripped[3:].strip()
            self._current["authors"] = _parse_authors(authors_raw)

        # Handle detection: look for repec: strings in text
        if "repec:" in stripped.lower():
            match = _REPEC_HANDLE_RE.search(stripped)
            if match and self._current and not self._current.get("handle"):
                self._current["handle"] = match.group(0).lower()

        # Date in item: "Date: YYYY-MM-DD" or "Issue Date: ..."
        if re.match(r"(issue\s+)?date\s*:", stripped, re.IGNORECASE):
            date_val = re.sub(r"^(issue\s+)?date\s*:\s*", "", stripped, flags=re.IGNORECASE)
            date_parsed = _extract_date_from_text(date_val)
            if date_parsed and self._current:
                self._current["date"] = date_parsed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _flush_current(self) -> None:
        """Commit the current paper buffer to self.papers if it has a title."""
        if self._current and self._current.get("title"):
            self.papers.append(self._current)
        self._current = None
        self._in_title = False
        self._in_abstract = False
        self._abstract_buf = []
        self._title_buf = []


class _NEPPageParserV2(HTMLParser):
    """Alternative NEP page parser that operates on the full page structure.

    NEP pages have changed layout over time. This parser takes a less
    state-machine-intensive approach: it buffers all visible text per block
    element and then post-processes the buffers to extract papers.

    It works on the assumption that papers are separated by <hr> tags or
    numbered headings and contain recognisable patterns like "By " for authors
    and "Abstract" for abstracts.
    """

    def __init__(self) -> None:
        super().__init__()
        self._blocks: list[str] = []       # text collected per block
        self._cur_block: list[str] = []
        self._skip_tags = {"script", "style", "nav", "footer", "header"}
        self._in_skip = False
        self._hrefs: list[str] = []        # all hrefs seen, in order
        self._block_hrefs: list[str] = []  # hrefs in current block
        self._last_tag = ""
        self.issue_date: str = ""
        self._in_h3 = False
        self._h3_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._in_skip = True
        if self._in_skip:
            return

        attr_dict = dict(attrs)
        href = attr_dict.get("href", "") or ""
        if href:
            self._block_hrefs.append(href)
            self._hrefs.append(href)

        if tag == "h3":
            self._in_h3 = True

        # Block-level tags start a new block
        if tag in ("li", "p", "div", "hr", "h1", "h2", "h3", "h4", "h5"):
            self._flush_block()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags:
            self._in_skip = False

        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            text = " ".join(self._h3_buf).strip()
            if not self.issue_date:
                date = _extract_date_from_text(text)
                if date:
                    self.issue_date = date
            self._h3_buf = []

        if tag in ("li", "p", "div", "hr", "h1", "h2", "h3", "h4", "h5"):
            self._flush_block()

    def handle_data(self, data: str) -> None:
        if self._in_skip:
            return
        if self._in_h3:
            self._h3_buf.append(data)
        if data.strip():
            self._cur_block.append(data)

    def _flush_block(self) -> None:
        text = " ".join(self._cur_block).strip()
        if text:
            self._blocks.append((text, list(self._block_hrefs)))
        self._cur_block = []
        self._block_hrefs = []

    def get_blocks(self) -> list[tuple[str, list[str]]]:
        self._flush_block()
        return self._blocks


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Unescape HTML entities and collapse whitespace.

    Args:
        text: Raw text possibly containing HTML entities and extra whitespace.

    Returns:
        Clean plain text.
    """
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_authors(raw: str) -> list[str]:
    """Split an author string on common delimiters.

    Handles patterns like:
      "Alice Smith & Bob Jones"
      "Smith, Alice; Jones, Bob"
      "Alice Smith and Bob Jones"

    Args:
        raw: Raw author string.

    Returns:
        List of stripped author name strings.
    """
    raw = _clean_text(raw)
    # Normalise delimiters to semicolon
    raw = re.sub(r"\s+&\s+|\s+and\s+", ";", raw, flags=re.IGNORECASE)
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    return parts if parts else ["Unknown"]


def _extract_date_from_text(text: str) -> str:
    """Extract an ISO date from a free-form text string.

    Tries multiple patterns: YYYY-MM-DD, Month YYYY, YYYY/MM/DD.
    Falls back to empty string if nothing matches.

    Args:
        text: Free-form text that may contain a date.

    Returns:
        ISO date string (YYYY-MM-DD) or empty string.
    """
    text = text.strip()

    # ISO date
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)

    # Slash date: 2025/01/15
    m = re.search(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # "January 2025" or "Jan 2025"
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        month_num = months[m.group(1)[:3].lower()]
        return f"{m.group(2)}-{month_num:02d}-01"

    # Just a year
    m = re.search(r"\b(20\d{2})\b", text)
    if m:
        return f"{m.group(1)}-01-01"

    return ""


def _handle_to_url(handle: str) -> str:
    """Convert a RepEC handle to an IDEAS.repec.org URL.

    Handle format: repec:series_code:paper_id
    URL format:    https://ideas.repec.org/p/series_code/paper_id.html

    Args:
        handle: RepEC handle string, e.g. "repec:nbr:nberwo:12345".

    Returns:
        IDEAS URL string, or empty string if handle is malformed.
    """
    handle = handle.strip().lower()
    # Remove "repec:" prefix
    rest = re.sub(r"^repec:", "", handle)
    parts = rest.split(":", 1)
    if len(parts) == 2:
        series, paper_id = parts
        return f"{_IDEAS_BASE}/{series}/{paper_id}.html"
    return ""


def _source_id_from_handle(report_code: str, handle: str, title: str) -> str:
    """Generate a stable source_id for a NEP paper.

    Prefers the RepEC handle (globally unique); falls back to hashing the
    report code + title when no handle is available.

    Args:
        report_code: NEP report code, e.g. "nep-dev".
        handle: RepEC handle string (may be empty).
        title: Paper title (used as fallback).

    Returns:
        Source ID string of the form "repec_nep:{report_code}:{hash}".
    """
    raw = handle.strip() if handle.strip() else f"{report_code}:{title}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"repec_nep:{report_code}:{digest}"


def _parse_nep_page(html_bytes: bytes, report_code: str) -> list[dict]:
    """Parse a NEP report HTML page into a list of raw paper dicts.

    Uses a two-pass strategy: first tries the structured _NEPPageParser;
    if that yields no papers, falls back to _NEPPageParserV2 which
    extracts text blocks and post-processes them with heuristics.

    Args:
        html_bytes: Raw HTML response bytes from the NEP report page.
        report_code: NEP report code, e.g. "nep-dev".

    Returns:
        List of raw dicts with keys: title, authors, abstract, handle,
        url, date, report_code.
    """
    html = html_bytes.decode("utf-8", errors="replace")

    # --- Pass 1: structured parser ---
    parser1 = _NEPPageParser()
    try:
        parser1.feed(html)
    except Exception as exc:
        warnings.warn(f"repec_nep: _NEPPageParser error for {report_code}: {exc}")

    issue_date = parser1.issue_date

    if parser1.papers:
        for paper in parser1.papers:
            paper["report_code"] = report_code
            if not paper.get("date") and issue_date:
                paper["date"] = issue_date
        return parser1.papers

    # --- Pass 2: block-level heuristic parser ---
    parser2 = _NEPPageParserV2()
    try:
        parser2.feed(html)
    except Exception as exc:
        warnings.warn(f"repec_nep: _NEPPageParserV2 error for {report_code}: {exc}")
        return []

    if not issue_date:
        issue_date = parser2.issue_date

    blocks = parser2.get_blocks()
    papers = _extract_papers_from_blocks(blocks, report_code, issue_date)
    return papers


def _extract_papers_from_blocks(
    blocks: list[tuple[str, list[str]]],
    report_code: str,
    issue_date: str,
) -> list[dict]:
    """Post-process text blocks from _NEPPageParserV2 into paper dicts.

    Heuristics:
    - A block whose text matches a numbered pattern "N. Title text" is a
      title block.
    - The immediately following blocks may contain "By Author" and an abstract.
    - Handle strings (repec:...) anywhere nearby are attributed to that paper.

    Args:
        blocks: List of (text, hrefs) tuples from _NEPPageParserV2.
        report_code: NEP report code string.
        issue_date: ISO date from the page header, used as fallback date.

    Returns:
        List of raw paper dicts.
    """
    papers: list[dict] = []
    current: dict | None = None
    i = 0

    while i < len(blocks):
        text, hrefs = blocks[i]

        # Numbered title: "1. Some Title Here" or "12. Another Title"
        title_match = re.match(r"^(\d+)\.\s+(.+)", text.strip())
        if title_match and len(text) > 10:
            # Flush previous paper
            if current and current.get("title"):
                papers.append(current)

            title = title_match.group(2).strip()
            # Extract URL from hrefs in this block
            url = hrefs[0] if hrefs else ""
            current = {
                "title": title,
                "authors": [],
                "abstract": "",
                "handle": "",
                "url": url,
                "date": issue_date,
                "report_code": report_code,
            }
            i += 1
            continue

        if current is not None:
            # Author line
            if re.match(r"^by\s+", text, re.IGNORECASE) and not current["authors"]:
                authors_raw = re.sub(r"^by\s+", "", text, flags=re.IGNORECASE)
                current["authors"] = _parse_authors(authors_raw)

            # Abstract text (longer blocks after title)
            elif len(text) > 80 and not current["abstract"]:
                current["abstract"] = _clean_text(text)[:2000]

            # Handle
            elif "repec:" in text.lower():
                m = _REPEC_HANDLE_RE.search(text)
                if m and not current["handle"]:
                    current["handle"] = m.group(0).lower()

            # Date line
            elif re.match(r"(issue\s+)?date\s*:", text, re.IGNORECASE):
                date_raw = re.sub(r"^(issue\s+)?date\s*:\s*", "", text, flags=re.IGNORECASE)
                date_parsed = _extract_date_from_text(date_raw)
                if date_parsed:
                    current["date"] = date_parsed

        i += 1

    # Flush last paper
    if current and current.get("title"):
        papers.append(current)

    return papers


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Load the shared sensor state JSON file.

    Returns:
        State dict; empty dict if the file is missing or corrupt.
    """
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    """Persist state to the JSON file.

    Args:
        state: Full state dict to serialize.
    """
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

class RepecNepSensor(BaseSensor):
    """Sensor that collects new working papers from NEP report pages.

    Fetches each configured NEP report HTML page, parses paper listings,
    and deduplicates against previously seen handles stored in state.

    Attributes:
        name: Sensor identifier used in DB logs.
        watch: Watch category this sensor belongs to.
        rate_limit: 0.5 requests per second (2-second pause between fetches).
    """

    name = "repec_nep"
    watch = "papers"
    rate_limit = 0.5  # polite crawl; NEP is a small academic service

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Fetch all configured NEP report pages and return new papers.

        Skips papers whose RepEC handles were seen in a previous run.
        On first run, collects everything present in the current issue.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        state = _load_state()
        seen_handles: dict[str, set] = {
            code: set(v)
            for code, v in state.get("nep_seen_handles", {}).items()
        }

        results: list[dict] = []
        new_seen: dict[str, set] = {}

        for report_code in NEP_REPORTS:
            url = f"{_NEP_BASE_URL}/{report_code}/"
            try:
                html_bytes = self.fetch_url(url)
            except Exception as exc:
                warnings.warn(
                    f"repec_nep: failed to fetch {report_code} ({url}): {exc}"
                )
                new_seen[report_code] = seen_handles.get(report_code, set())
                continue

            raw_papers = _parse_nep_page(html_bytes, report_code)
            prior_seen = seen_handles.get(report_code, set())
            current_handles: set[str] = set()

            for raw in raw_papers:
                handle = raw.get("handle", "")
                title = raw.get("title", "").strip()

                # Use handle as dedup key; fall back to title hash
                dedup_key = handle if handle else hashlib.sha256(title.encode()).hexdigest()[:16]
                current_handles.add(dedup_key)

                if dedup_key in prior_seen:
                    continue  # already ingested in a previous run

                paper = self._to_paper_dict(raw, report_code)
                if paper is not None:
                    results.append(paper)

            new_seen[report_code] = current_handles

        # Persist state
        state["nep_seen_handles"] = {k: list(v) for k, v in new_seen.items()}
        _save_state(state)

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _to_paper_dict(self, raw: dict, report_code: str) -> dict | None:
        """Convert a raw NEP paper dict to the standard paper dict format.

        Args:
            raw: Raw paper dict from _parse_nep_page.
            report_code: NEP report code, e.g. "nep-dev".

        Returns:
            Paper dict or None if the entry lacks a title.
        """
        title = raw.get("title", "").strip()
        if not title:
            return None

        handle = raw.get("handle", "")
        authors = raw.get("authors") or ["Unknown"]
        abstract = raw.get("abstract", "") or ""
        date = raw.get("date", "") or datetime.now(timezone.utc).date().isoformat()
        raw_url = raw.get("url", "") or ""

        # Prefer IDEAS URL derived from handle; fall back to raw_url
        if handle:
            ideas_url = _handle_to_url(handle)
            url = ideas_url if ideas_url else raw_url
        else:
            url = raw_url

        source_id = _source_id_from_handle(report_code, handle, title)

        report_label = NEP_REPORTS.get(report_code, report_code)

        return {
            "title": title,
            "authors": authors,
            "abstract": abstract[:2000] if abstract else None,
            "url": url,
            "published_at": date,
            "paper_type": "working_paper",
            "jel_codes": [],
            "keywords": [report_label, report_code],
            "source_id": source_id,
            "source_url": url,
            "raw_metadata": {
                "report_code": report_code,
                "report_label": report_label,
                "repec_handle": handle,
                "raw_url": raw_url,
            },
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from lib.db import init_db

    init_db()
    sensor = RepecNepSensor()
    sensor.run()
