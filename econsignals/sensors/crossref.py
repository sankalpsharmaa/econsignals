"""Crossref sensor for EconSignals.

Monitors top economics journals via the Crossref API for new publications
and provides DOI-based deduplication.

Usage:
    python sensors/crossref.py

Environment variables:
    OPENALEX_EMAIL   Email for the Crossref polite pool (recommended).
                     Falls back to a placeholder if unset.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from econsignals.sensors._base import BaseSensor, PROJ_ROOT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CROSSREF_BASE = "https://api.crossref.org"
_ROWS = 25
_DEFAULT_LOOKBACK_DAYS = 7

# Core journals — scraped every run, processed first.
# Crossref uses ISSN; URLs below are for reference only (metadata is free, no paywall).
CORE_JOURNALS: dict[str, str] = {
    "Quarterly Journal of Economics": "0033-5533",      # academic.oup.com/qje
    "American Economic Review": "0002-8282",           # aeaweb.org/journals/aer
    "Review of Economic Studies": "0034-6527",         # direct.mit.edu/rest, academic.oup.com/restud
    "Econometrica": "0012-9682",                       # econometricsociety.org/econometrica
    "Journal of Political Economy": "0022-3808",       # journals.uchicago.edu/loi/jpe
    "Journal of Urban Economics": "0094-1190",         # sciencedirect.com/journal-of-urban-economics
    "Journal of Development Economics": "0304-3878",   # sciencedirect.com/journal-of-development-economics
    "World Development": "0305-750X",                   # journals.elsevier.com/world-development
    "World Bank Economic Review": "0258-6770",          # academic.oup.com/wber
}

# Additional economics journals (processed after CORE_JOURNALS)
_EXTRA_JOURNALS: dict[str, str] = {
    "Journal of Human Resources": "0022-166X",
    "Journal of Labor Economics": "0734-306X",
    "Journal of Public Economics": "0047-2727",
    "Review of Economics and Statistics": "0034-6535",
    "American Economic Journal: Applied": "1945-7782",
    "American Economic Journal: Economic Policy": "1945-7731",
    "Journal of the European Economic Association": "1542-4766",
    "Economic Journal": "0013-0133",
    "Journal of Development Studies": "0022-0388",
    "Economic and Political Weekly": "0012-9976",
    "Indian Economic Review": "0019-4670",
}

ECON_JOURNALS: dict[str, str] = {**CORE_JOURNALS, **_EXTRA_JOURNALS}

_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

# Crossref state key prefix: "crossref_last_fetched_{issn}"
_STATE_KEY_PREFIX = "crossref_last_fetched_"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _parse_date_parts(date_parts: list) -> str | None:
    """Convert Crossref date-parts array to an ISO date string.

    Crossref returns dates as a nested list: [[YYYY, MM, DD]] or [[YYYY, MM]]
    or [[YYYY]].  This function handles all three variants, padding missing
    month/day components with 1.

    Args:
        date_parts: The raw date-parts value from the Crossref API, e.g.
            [[2024, 1, 15]], [[2024, 6]], or [[2023]].

    Returns:
        ISO date string 'YYYY-MM-DD', or None if the input is empty/invalid.

    Examples:
        >>> _parse_date_parts([[2024, 1, 15]])
        '2024-01-15'
        >>> _parse_date_parts([[2024, 6]])
        '2024-06-01'
        >>> _parse_date_parts([[2023]])
        '2023-01-01'
        >>> _parse_date_parts([])
        None
    """
    if not date_parts or not date_parts[0]:
        return None
    parts = date_parts[0]
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError, IndexError):
        return None


def _strip_jats(text: str) -> str:
    """Remove JATS XML tags from an abstract string.

    Publishers encode abstracts using JATS (Journal Article Tag Suite) XML.
    This function strips all tags (e.g. <jats:p>, <jats:italic>) and
    collapses excess whitespace.

    Args:
        text: Raw abstract string that may contain JATS XML tags.

    Returns:
        Plain-text string with all XML/HTML tags removed and whitespace
        normalised to single spaces.

    Examples:
        >>> _strip_jats("<jats:p>Hello <jats:italic>world</jats:italic>.</jats:p>")
        'Hello world.'
        >>> _strip_jats("  plain text  ")
        'plain text'
    """
    if not text:
        return ""
    # Remove all XML/HTML tags (greedy-safe non-greedy match)
    cleaned = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    return " ".join(cleaned.split())


def _parse_authors(authors: list[dict]) -> list[str]:
    """Convert Crossref author objects to a list of name strings.

    Crossref author objects contain 'given' and 'family' fields.  Either may
    be absent (some records have only a name or only a family field).

    Args:
        authors: List of Crossref author dicts, each with optional 'given'
            and 'family' keys.

    Returns:
        List of full name strings.  Authors without any name information
        are skipped.

    Examples:
        >>> _parse_authors([{"given": "John", "family": "Smith"}])
        ['John Smith']
        >>> _parse_authors([{"family": "Smith"}, {"given": "Jane"}])
        ['Smith', 'Jane']
        >>> _parse_authors([{}])
        []
    """
    names: list[str] = []
    for author in authors:
        given = (author.get("given") or "").strip()
        family = (author.get("family") or "").strip()
        if given and family:
            names.append(f"{given} {family}")
        elif family:
            names.append(family)
        elif given:
            names.append(given)
    return names


def _load_state() -> dict:
    """Load the watches/papers/state.json file.

    Returns:
        State dict. Returns empty dict if the file does not exist or is invalid.
    """
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically write state to watches/papers/state.json.

    Args:
        state: Dict to persist as JSON.
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


def _state_key(issn: str) -> str:
    """Return the state.json key for a given ISSN.

    Args:
        issn: Journal ISSN string (may contain hyphens).

    Returns:
        State dict key string, e.g. 'crossref_last_fetched_0002-8282'.
    """
    return f"{_STATE_KEY_PREFIX}{issn}"


def _from_date(issn: str, state: dict) -> str:
    """Determine the earliest publication date to query for a journal.

    Reads the per-journal last_fetched date from state.  Falls back to
    _DEFAULT_LOOKBACK_DAYS ago if no prior run is recorded.

    Args:
        issn: Journal ISSN used to look up state.
        state: Loaded state dict from state.json.

    Returns:
        ISO date string 'YYYY-MM-DD'.
    """
    key = _state_key(issn)
    last_fetched = state.get(key)
    if last_fetched:
        try:
            datetime.fromisoformat(last_fetched)
            return last_fetched
        except ValueError:
            pass
    return (datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d"
    )


def _build_journal_url(issn: str, from_date: str, email: str) -> str:
    """Build the Crossref journal works URL.

    Args:
        issn: Journal ISSN.
        from_date: Lower bound date string 'YYYY-MM-DD'.
        email: Email address for Crossref polite pool.

    Returns:
        Fully-qualified URL string.
    """
    params: dict[str, str | int] = {
        "filter": f"from-pub-date:{from_date}",
        "sort": "published",
        "order": "desc",
        "rows": _ROWS,
    }
    if email:
        params["mailto"] = email
    return f"{_CROSSREF_BASE}/journals/{issn}/works?{urlencode(params)}"


def _parse_work(work: dict) -> dict | None:
    """Convert a raw Crossref work object to the standard paper dict format.

    Handles partial records gracefully: missing abstract, authors, or date
    fields are stored as None rather than raising.

    Args:
        work: Raw dict from the Crossref /journals/{issn}/works endpoint.

    Returns:
        Paper dict compatible with BaseSensor.collect() return contract, or
        None if the work lacks a usable title or DOI.
    """
    # Title is a list in Crossref
    title_list = work.get("title") or []
    title = title_list[0].strip() if title_list else ""
    if not title:
        return None

    doi = (work.get("DOI") or "").strip()
    if not doi:
        return None

    # URL: Crossref provides a dx.doi.org URL
    url = (work.get("URL") or f"https://doi.org/{doi}").strip()

    # Authors
    authors = _parse_authors(work.get("author") or [])

    # Abstract: strip JATS tags
    raw_abstract = work.get("abstract") or ""
    abstract = _strip_jats(raw_abstract) or None

    # Published date: prefer published-print, fall back to published-online
    published_at: str | None = None
    for date_field in ("published-print", "published-online", "created"):
        date_obj = work.get(date_field) or {}
        date_parts = date_obj.get("date-parts") or []
        published_at = _parse_date_parts(date_parts)
        if published_at:
            break

    # Keywords from subject array
    keywords: list[str] = [s.strip() for s in (work.get("subject") or []) if s.strip()]

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "url": url,
        "published_at": published_at,
        "paper_type": "journal_article",
        "jel_codes": [],
        "keywords": keywords,
        "source_id": doi,
        "source_url": url,
        "raw_metadata": work,
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class CrossrefSensor(BaseSensor):
    """Fetch recent journal articles from the Crossref API.

    Queries the Crossref /journals/{issn}/works endpoint for each journal in
    ECON_JOURNALS, collecting papers published since the last successful run.
    Uses DOI as the canonical unique identifier for deduplication.

    Attributes:
        name: Sensor identifier used for DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: Requests per second (conservative; Crossref allows ~50/s
            in the polite pool).
    """

    name = "crossref"
    watch = "papers"
    rate_limit = 5.0  # conservative; Crossref allows ~50 req/s polite pool

    def collect(self) -> list[dict]:
        """Fetch recent papers from all configured economics journals.

        For each journal ISSN:
        1. Determines the from-date via per-journal state.
        2. Queries /journals/{issn}/works with polite-pool mailto param.
        3. Parses each work into the standard paper dict.

        After all journals are queried, persists updated fetch dates to
        state.json so the next run only retrieves new papers.

        Returns:
            List of paper dicts suitable for BaseSensor.run() ingestion.
            DOI is used as source_id for cross-source deduplication.
        """
        email = os.environ.get("OPENALEX_EMAIL", "")
        state = _load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        papers: list[dict] = []
        seen_dois: set[str] = set()

        for journal_name, issn in ECON_JOURNALS.items():
            from_date = _from_date(issn, state)
            url = _build_journal_url(issn, from_date, email)

            print(
                f"[crossref] {journal_name} ({issn}): fetching from {from_date}",
                file=sys.stderr,
            )

            try:
                response = self.fetch_json(url)
            except Exception as exc:
                print(
                    f"[crossref] {journal_name} ({issn}): fetch failed: {exc}",
                    file=sys.stderr,
                )
                # Do not update state for this journal; retry next run
                continue

            message = response.get("message") or {}
            items = message.get("items") or []

            journal_count = 0
            for work in items:
                parsed = _parse_work(work)
                if not parsed:
                    continue

                doi = parsed["source_id"]
                if doi in seen_dois:
                    continue
                seen_dois.add(doi)

                papers.append(parsed)
                journal_count += 1

            print(
                f"[crossref] {journal_name} ({issn}): {journal_count} papers collected",
                file=sys.stderr,
            )

            # Mark this journal as fetched up to today
            state[_state_key(issn)] = today

        # Persist updated state atomically
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[crossref] failed to save state: {exc}", file=sys.stderr)

        print(f"[crossref] total collected: {len(papers)} papers", file=sys.stderr)
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = CrossrefSensor()
    sensor.run()
