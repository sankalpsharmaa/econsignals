"""NBER working paper sensor for EconSignals.

Collects newly published NBER working papers from nber.org.

Primary approach: NBER's listing API at
    https://www.nber.org/api/v1/working_page_listing/contentType/working_paper/_/_/search

Fallback: HTML scraping of the listing page at https://www.nber.org/papers, then
individual paper pages for metadata.

NBER working paper DOIs follow the pattern ``10.3386/{handle}`` (e.g. "10.3386/w32000").
JEL codes are extracted from both the API response and individual paper pages.

Usage:
    python sensors/nber.py

State:
    Persisted under the "nber" key in watches/papers/state.json.
    Tracks last successful fetch date to avoid re-ingesting old papers.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py convention)
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_URL = (
    "https://www.nber.org/api/v1/working_page_listing"
    "/contentType/working_paper/_/_/search"
)
_LISTING_URL = "https://www.nber.org/papers"
_PAPER_BASE = "https://www.nber.org/papers/"
_NBER_DOI_PREFIX = "10.3386/"

_MAX_PAGES = 3
_PER_PAGE = 50
_DEFAULT_LOOKBACK_DAYS = 7

_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

# Regex patterns for HTML scraping
_RE_PAPER_HANDLE = re.compile(r"/papers/(w\d+)", re.IGNORECASE)
_RE_JEL = re.compile(r"\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)", re.IGNORECASE)
_RE_JEL_CODE = re.compile(r"[A-Z][0-9]{1,2}")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/papers/state.json, returning empty dict on failure.

    Returns:
        Full state dict. May be empty if file is absent or corrupt.
    """
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist state dict to watches/papers/state.json.

    Uses a temp-file + rename to avoid truncating state on write errors.

    Args:
        state: Dict to serialize as JSON.
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
# Parsing helpers
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace.

    Args:
        html: Raw HTML string.

    Returns:
        Plain-text string with collapsed whitespace.

    Examples:
        >>> _strip_html("<p>Hello <b>world</b></p>")
        'Hello world'
    """
    if not html:
        return ""
    from html import unescape

    html = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL classification codes from a block of text.

    Matches patterns like "JEL: C1, D8" or standalone two-character codes
    in a JEL context.

    Args:
        text: Plain text or HTML string to search.

    Returns:
        Deduplicated list of JEL code strings (e.g. ['C1', 'D83']).

    Examples:
        >>> _extract_jel_codes("JEL: C1, D8, F21")
        ['C1', 'D8', 'F21']
    """
    codes: list[str] = []
    seen: set[str] = set()

    for match in _RE_JEL.finditer(text):
        segment = match.group(1)
        for code in _RE_JEL_CODE.findall(segment):
            if code not in seen:
                seen.add(code)
                codes.append(code)

    return codes


def _handle_to_doi(handle: str) -> str:
    """Convert an NBER paper handle to its DOI.

    NBER DOIs follow the pattern ``10.3386/{handle}`` for all working papers.

    Args:
        handle: NBER paper handle, e.g. "w32000".

    Returns:
        Bare DOI string, e.g. "10.3386/w32000".

    Examples:
        >>> _handle_to_doi("w32000")
        '10.3386/w32000'
    """
    return f"{_NBER_DOI_PREFIX}{handle}"


def _parse_date(raw: str | None) -> str | None:
    """Parse a date string to ISO YYYY-MM-DD format.

    Attempts common NBER date formats: ISO 8601, month/day/year, and
    natural language like "January 2024".

    Args:
        raw: Raw date string from the API or HTML.

    Returns:
        ISO date string, or None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%m/%d/%Y",
        "%B %Y",
        "%b %Y",
    ):
        try:
            return datetime.strptime(raw[:len(fmt) + 5], fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    # Try stripping just the YYYY-MM-DD prefix
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if match:
        return match.group(1)

    return None


def _parse_api_paper(record: dict) -> dict | None:
    """Convert a raw NBER API record to the standard paper dict.

    Handles the NBER listing API response structure defensively, since the
    API schema may change without notice.

    Args:
        record: Raw dict from the NBER API ``results`` or ``items`` list.

    Returns:
        Standard paper dict for BaseSensor.collect(), or None if the record
        cannot be parsed (missing title or handle).
    """
    # NBER API uses several possible field names for the handle
    handle = (
        record.get("handle")
        or record.get("id")
        or record.get("paper_id")
        or record.get("slug")
        or ""
    ).strip()

    # Normalise: ensure handle starts with "w" and is numeric after
    if not handle:
        return None
    if not re.match(r"^w\d+$", handle, re.IGNORECASE):
        # Try to extract from a URL-like value
        m = _RE_PAPER_HANDLE.search(handle)
        if m:
            handle = m.group(1)
        else:
            return None
    handle = handle.lower()

    title = (
        record.get("title")
        or record.get("name")
        or ""
    ).strip()
    if not title:
        return None

    # Authors: may be a list of dicts or a list of strings
    raw_authors = record.get("authors") or record.get("authorList") or []
    authors: list[str] = []
    for a in raw_authors:
        if isinstance(a, str):
            name = a.strip()
        elif isinstance(a, dict):
            name = (
                a.get("name")
                or a.get("display_name")
                or a.get("full_name")
                or ""
            ).strip()
        else:
            name = ""
        if name:
            authors.append(name)

    # Abstract
    abstract_raw = record.get("abstract") or record.get("description") or ""
    abstract = _strip_html(abstract_raw).strip() or None

    # Date: NBER uses publicationDate or pubdate or issued
    date_raw = (
        record.get("publicationDate")
        or record.get("pubdate")
        or record.get("issued")
        or record.get("date")
        or ""
    )
    published_at = _parse_date(date_raw)

    # JEL codes
    jel_raw = record.get("jel") or record.get("jelCodes") or record.get("jel_codes") or []
    jel_codes: list[str] = []
    if isinstance(jel_raw, list):
        for code in jel_raw:
            if isinstance(code, str):
                jel_codes.append(code.strip())
            elif isinstance(code, dict):
                c = (code.get("code") or code.get("id") or "").strip()
                if c:
                    jel_codes.append(c)
    elif isinstance(jel_raw, str) and jel_raw:
        # "C1,D8,F21" or "C1 D8 F21"
        jel_codes = [c.strip() for c in re.split(r"[,;\s]+", jel_raw) if c.strip()]

    # Supplement JEL from abstract text if none found
    if not jel_codes and abstract_raw:
        jel_codes = _extract_jel_codes(abstract_raw)

    # Keywords
    keywords_raw = record.get("keywords") or record.get("topics") or []
    keywords: list[str] = []
    if isinstance(keywords_raw, list):
        for k in keywords_raw:
            if isinstance(k, str) and k.strip():
                keywords.append(k.strip())
            elif isinstance(k, dict):
                kw = (k.get("name") or k.get("label") or "").strip()
                if kw:
                    keywords.append(kw)
    elif isinstance(keywords_raw, str) and keywords_raw:
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

    doi = _handle_to_doi(handle)
    source_url = f"{_PAPER_BASE}{handle}"

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "url": f"https://doi.org/{doi}",
        "published_at": published_at,
        "paper_type": "working_paper",
        "jel_codes": jel_codes,
        "keywords": keywords,
        "source_id": handle,
        "source_url": source_url,
        "raw_metadata": record,
    }


def _parse_paper_page_html(html: str, handle: str) -> dict | None:
    """Extract paper metadata from an NBER individual paper page HTML.

    Used as a fallback when the listing API is unavailable. Applies a series
    of regex patterns against the raw HTML string without a full HTML parser.

    Args:
        html: Raw HTML content of the paper page.
        handle: NBER paper handle (e.g. "w32000"), used for source fields.

    Returns:
        Standard paper dict or None if title cannot be extracted.
    """
    # Title: typically in <h1> or og:title
    title = ""
    for pattern in (
        r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
        r"<h1[^>]*>(.*?)</h1>",
        r"<title>(.*?)</title>",
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            candidate = _strip_html(m.group(1)).strip()
            # NBER page titles often include "NBER" suffix; trim it
            candidate = re.sub(r"\s*[|\-–]\s*NBER.*$", "", candidate).strip()
            if candidate:
                title = candidate
                break

    if not title:
        return None

    # Authors: look for structured author blocks or og:author
    authors: list[str] = []
    # Try <span class="...author..."> or similar
    for pattern in (
        r'class="[^"]*author[^"]*"[^>]*>(.*?)</(?:span|div|a|p)>',
        r'itemprop="author"[^>]*>(.*?)</(?:span|div|a|p)>',
        r'<meta\s+name=["\']author["\']\s+content=["\'](.*?)["\']',
    ):
        for m in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
            name = _strip_html(m.group(1)).strip()
            if name and len(name) < 100:
                authors.append(name)
        if authors:
            break

    # Abstract: look for abstract div or section
    abstract: str | None = None
    for pattern in (
        r'class="[^"]*abstract[^"]*"[^>]*>(.*?)</(?:div|section|p)>',
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
        r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            candidate = _strip_html(m.group(1)).strip()
            if len(candidate) > 50:
                abstract = candidate[:3000]
                break

    # Publication date: look for pubDate or similar
    published_at: str | None = None
    for pattern in (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<meta\s+(?:name|property)=["\'](?:pubdate|article:published_time)["\'][^>]*content=["\'](.*?)["\']',
        r'Published:\s*<[^>]+>([^<]+)<',
    ):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            published_at = _parse_date(m.group(1).strip())
            if published_at:
                break

    # JEL codes
    jel_codes = _extract_jel_codes(html)
    # Also look for structured JEL code elements
    jel_pattern = r'JEL[^:]*:\s*((?:[A-Z][0-9]{1,2}[,;\s]*)+)'
    for m in re.finditer(jel_pattern, html, re.IGNORECASE):
        extra = _RE_JEL_CODE.findall(m.group(1))
        for code in extra:
            if code not in jel_codes:
                jel_codes.append(code)

    doi = _handle_to_doi(handle)
    source_url = f"{_PAPER_BASE}{handle}"

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "url": f"https://doi.org/{doi}",
        "published_at": published_at,
        "paper_type": "working_paper",
        "jel_codes": jel_codes,
        "keywords": [],
        "source_id": handle,
        "source_url": source_url,
        "raw_metadata": {"handle": handle, "source": "html_scrape"},
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class NBERSensor(BaseSensor):
    """Fetch recent NBER working papers from nber.org.

    Tries the NBER listing API first. Falls back to HTML scraping if the
    API returns an error or an unrecognised response shape. Both approaches
    are wrapped defensively: any failure returns an empty list and logs a
    warning rather than raising.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: 1 request per second, respecting NBER's servers.
    """

    name = "nber"
    watch = "papers"
    rate_limit = 1.0

    # Public constants (also used by tests)
    LISTING_URL = _LISTING_URL
    API_URL = _API_URL
    PAPER_BASE = _PAPER_BASE

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return the ISO date string to use as the publication date filter.

        Reads ``nber_last_fetched`` from watch state. Falls back to 7 days ago.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = (state.get("nber") or {}).get("last_fetched")
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
        """Record today's date as the last successful NBER fetch in state."""
        state = _load_state()
        nber_state = state.get("nber") or {}
        nber_state["last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state["nber"] = nber_state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[nber] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # API approach
    # ------------------------------------------------------------------

    def _build_api_url(self, page: int) -> str:
        """Construct a full NBER listing API URL for a given page.

        Args:
            page: 1-based page number.

        Returns:
            Fully-qualified URL string with query parameters.
        """
        params = {
            "page": page,
            "perPage": _PER_PAGE,
        }
        return f"{_API_URL}?{urlencode(params)}"

    def _fetch_api_page(self, page: int) -> list[dict]:
        """Fetch one page of results from the NBER listing API.

        Parses the JSON response and returns raw record dicts. Handles both
        ``results`` and ``items`` as the top-level key.

        Args:
            page: 1-based page number.

        Returns:
            List of raw paper record dicts, or empty list on any error.

        Raises:
            Nothing. All errors are caught and logged.
        """
        url = self._build_api_url(page)
        try:
            data = self.fetch_json(url)
        except Exception as exc:
            print(f"[nber] API page {page} fetch failed: {exc}", file=sys.stderr)
            return []

        if not isinstance(data, dict):
            print(
                f"[nber] API page {page}: unexpected response type {type(data).__name__}",
                file=sys.stderr,
            )
            return []

        # Try common top-level keys
        records = (
            data.get("results")
            or data.get("items")
            or data.get("papers")
            or data.get("data")
            or []
        )
        if not isinstance(records, list):
            print(
                f"[nber] API page {page}: results field is not a list",
                file=sys.stderr,
            )
            return []

        return records

    def _collect_via_api(self, from_date: str) -> list[dict] | None:
        """Collect working papers using the NBER listing API.

        Paginates up to _MAX_PAGES pages. Stops early when either no results
        are returned or all remaining papers predate from_date.

        Args:
            from_date: Lower bound date string 'YYYY-MM-DD'. Papers published
                before this date are skipped.

        Returns:
            List of parsed paper dicts, or None if the API appears broken
            (first page returned nothing parseable at all).
        """
        papers: list[dict] = []
        seen: set[str] = set()
        first_page_attempted = False

        for page in range(1, _MAX_PAGES + 1):
            records = self._fetch_api_page(page)
            first_page_attempted = True

            if not records:
                if page == 1:
                    # API returned nothing on first page - signal failure
                    return None
                break

            page_papers: list[dict] = []
            all_old = True  # flag: every paper on this page predates from_date

            for rec in records:
                parsed = _parse_api_paper(rec)
                if not parsed:
                    continue

                handle = parsed["source_id"]
                if handle in seen:
                    continue
                seen.add(handle)

                pub_date = parsed.get("published_at") or ""
                if pub_date and pub_date < from_date:
                    # Paper predates our window; keep checking the rest of the
                    # page in case the listing is not strictly sorted by date.
                    continue

                all_old = False
                page_papers.append(parsed)

            papers.extend(page_papers)

            print(
                f"[nber] API page {page}: {len(records)} records, "
                f"{len(page_papers)} within date window",
                file=sys.stderr,
            )

            # If every paper on this page predated from_date, we can stop
            if all_old and page_papers == [] and records:
                print(
                    f"[nber] all papers on page {page} predate {from_date}, stopping",
                    file=sys.stderr,
                )
                break

        if not first_page_attempted:
            return None

        return papers

    # ------------------------------------------------------------------
    # HTML scraping fallback
    # ------------------------------------------------------------------

    def _collect_via_scrape(self, from_date: str) -> list[dict]:
        """Collect working papers by scraping the NBER HTML listing page.

        Fetches the listing, extracts paper handles via regex, then fetches
        each individual paper page to extract metadata. Stops when papers
        predate from_date.

        Args:
            from_date: Lower bound date string; papers older than this are skipped.

        Returns:
            List of parsed paper dicts. Returns empty list on any error.
        """
        listing_url = f"{_LISTING_URL}?page=1&perPage={_PER_PAGE}"
        print(f"[nber] scraping listing page: {listing_url}", file=sys.stderr)

        try:
            html_bytes = self.fetch_url(listing_url)
            html = html_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            warnings.warn(f"[nber] failed to fetch listing page: {exc}")
            return []

        # Extract all unique handles from the listing
        handles = list(dict.fromkeys(
            m.group(1).lower()
            for m in _RE_PAPER_HANDLE.finditer(html)
        ))

        if not handles:
            warnings.warn("[nber] scraping: no paper handles found on listing page")
            return []

        print(f"[nber] scraping: found {len(handles)} handles on listing page", file=sys.stderr)

        papers: list[dict] = []
        for handle in handles:
            paper_url = f"{_PAPER_BASE}{handle}"
            try:
                page_bytes = self.fetch_url(paper_url)
                page_html = page_bytes.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[nber] failed to fetch {paper_url}: {exc}", file=sys.stderr)
                continue

            parsed = _parse_paper_page_html(page_html, handle)
            if not parsed:
                print(f"[nber] could not parse paper page {handle}", file=sys.stderr)
                continue

            pub_date = parsed.get("published_at") or ""
            if pub_date and pub_date < from_date:
                # Papers are listed newest-first; once we hit old papers, stop
                print(
                    f"[nber] {handle} ({pub_date}) predates {from_date}, stopping",
                    file=sys.stderr,
                )
                break

            papers.append(parsed)

        return papers

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Fetch recent NBER working papers.

        Steps:
        1. Determine from_date via watch state (default: 7 days ago).
        2. Try the NBER listing API; parse and filter by date.
        3. If the API returns nothing usable, fall back to HTML scraping.
        4. Update watch state with today's date.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        from_date = self._from_date()
        print(
            f"[nber] collecting working papers published on or after {from_date}",
            file=sys.stderr,
        )

        papers: list[dict] | None = None

        # --- Primary: API ---
        try:
            papers = self._collect_via_api(from_date)
        except Exception as exc:
            warnings.warn(f"[nber] API collection raised unexpectedly: {exc}")
            papers = None

        # --- Fallback: HTML scraping ---
        if papers is None:
            warnings.warn("[nber] API unavailable or returned no usable data, falling back to HTML scraping")
            try:
                papers = self._collect_via_scrape(from_date)
            except Exception as exc:
                warnings.warn(f"[nber] HTML scraping also failed: {exc}")
                papers = []

        print(f"[nber] collected {len(papers)} papers", file=sys.stderr)

        # Update state only on (at least partial) success
        self._update_state()

        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = NBERSensor()
    sensor.run()
