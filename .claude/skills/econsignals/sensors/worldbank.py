"""World Bank Documents & Reports API sensor for EconSignals.

Fetches recent working papers from the World Bank Documents & Reports API
(search.worldbank.org/api/v2/wds).  Covers policy research working papers
and other working paper series published by the World Bank.

Usage:
    python sensors/worldbank.py

Environment variables:
    OPENALEX_EMAIL   Used in User-Agent string for polite identification.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Bootstrap path so _base imports work when run directly
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WDS_BASE = "https://search.worldbank.org/api/v2/wds"
_ROWS_PER_PAGE = 50
_MAX_PAGES = 3
_DEFAULT_LOOKBACK_DAYS = 30

# Document types to query. Each is fetched as a separate request because the
# WDS API does not support OR-ing docty values in a single call.
_DOC_TYPES: list[str] = [
    "Working Paper",
    "Policy Research Working Paper",
]

# State file shared with other paper sensors
_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load the watches/papers/state.json file.

    Returns:
        Parsed state dict, or empty dict if file is absent or malformed.
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


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_abstract(doc: dict) -> str:
    """Pull abstract text from the WDS document dict.

    The API returns abstracts under an 'abstracts' key whose value is a dict
    with a 'cdata!!' key containing the text.  Falls back to an empty string
    if the structure is absent or unexpected.

    Args:
        doc: Raw document dict from the WDS API response.

    Returns:
        Abstract string, stripped of whitespace, or empty string.

    Example:
        >>> _extract_abstract({"abstracts": {"cdata!!": "This paper..."}})
        'This paper...'
    """
    abstracts = doc.get("abstracts")
    if not abstracts:
        return ""
    if isinstance(abstracts, dict):
        text = abstracts.get("cdata!!", "") or ""
        return text.strip()
    if isinstance(abstracts, str):
        return abstracts.strip()
    return ""


def _extract_authors(doc: dict) -> list[str]:
    """Parse author names from a WDS document dict.

    The API returns authors in one of three shapes:

    1. ``{"authors": {"author": [{"name": "Smith, John"}, ...]}}`` (list)
    2. ``{"authors": {"author": {"name": "Smith, John"}}}`` (single dict)
    3. ``{"authors": "Smith, John; Doe, Jane"}`` (semicolon-delimited string)

    All names are returned as-is (typically "Last, First" format).

    Args:
        doc: Raw document dict from the WDS API response.

    Returns:
        List of author name strings.  Empty list if none are found.
    """
    raw = doc.get("authors")
    if not raw:
        return []

    # Shape 3: plain string with semicolon separation
    if isinstance(raw, str):
        return [n.strip() for n in raw.split(";") if n.strip()]

    if not isinstance(raw, dict):
        return []

    author_field = raw.get("author")
    if not author_field:
        return []

    # Shape 2: single author as dict
    if isinstance(author_field, dict):
        name = (author_field.get("name") or "").strip()
        return [name] if name else []

    # Shape 1: list of author dicts
    if isinstance(author_field, list):
        names: list[str] = []
        for entry in author_field:
            if isinstance(entry, dict):
                name = (entry.get("name") or "").strip()
            elif isinstance(entry, str):
                name = entry.strip()
            else:
                continue
            if name:
                names.append(name)
        return names

    return []


def _extract_keywords(doc: dict) -> list[str]:
    """Extract keyword strings from the WDS document dict.

    Handles both list and dict forms of the 'keywords' field:

    - ``{"keywords": {"keyword": ["poverty", "education"]}}``
    - ``{"keywords": {"keyword": "poverty"}}``

    Args:
        doc: Raw document dict from the WDS API response.

    Returns:
        List of keyword strings.
    """
    raw = doc.get("keywords")
    if not raw:
        return []

    if isinstance(raw, dict):
        kw_field = raw.get("keyword")
        if not kw_field:
            return []
        if isinstance(kw_field, list):
            return [str(k).strip() for k in kw_field if str(k).strip()]
        if isinstance(kw_field, str):
            return [kw_field.strip()] if kw_field.strip() else []
        return []

    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]

    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []

    return []


def _extract_country_codes(doc: dict) -> list[str]:
    """Pull ISO country codes from the WDS 'count' field.

    The API uses 'count' (confusingly named) to store country data:
    ``{"count": {"iso_code": ["IND", "BGD"]}}`` or a plain string/list.

    Args:
        doc: Raw document dict from the WDS API response.

    Returns:
        List of uppercase ISO 3166-1 alpha-3 country code strings.
    """
    raw = doc.get("count")
    if not raw:
        return []

    if isinstance(raw, dict):
        iso = raw.get("iso_code")
        if not iso:
            return []
        if isinstance(iso, list):
            return [str(c).strip().upper() for c in iso if str(c).strip()]
        if isinstance(iso, str):
            return [iso.strip().upper()] if iso.strip() else []
        return []

    if isinstance(raw, list):
        return [str(c).strip().upper() for c in raw if str(c).strip()]

    return []


def _parse_date(docdt: str | None) -> str | None:
    """Normalise the WDS docdt field to a YYYY-MM-DD string.

    The API typically returns dates as 'YYYY-MM-DDT00:00:00Z' but may return
    just 'YYYY-MM-DD' or other partial forms.

    Args:
        docdt: Raw date string from the API, or None.

    Returns:
        'YYYY-MM-DD' string, or None if input is absent or unparseable.
    """
    if not docdt:
        return None
    # Strip time component if present
    date_part = docdt.split("T")[0].strip()
    # Validate
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
        return date_part
    except ValueError:
        return None


def _source_id_for(doc: dict) -> str:
    """Return the best available stable identifier for a WDS document.

    Preference order: report number (e.g. 'WPS1234'), document id.

    Args:
        doc: Raw document dict from the WDS API response.

    Returns:
        Non-empty source_id string.
    """
    repnb = (doc.get("repnb") or "").strip()
    if repnb:
        return repnb
    doc_id = (doc.get("id") or "").strip()
    return doc_id or f"wb-{hash(doc.get('display_title', ''))}"


def _parse_doc(doc: dict) -> dict | None:
    """Convert a raw WDS document dict to the standard paper dict format.

    Returns None for documents that lack a title (cannot be meaningfully
    ingested).

    Args:
        doc: Raw document dict keyed by field name as returned by the WDS API.

    Returns:
        Paper dict compatible with BaseSensor.collect() return contract,
        or None if the document is unusable.
    """
    title = (doc.get("display_title") or "").strip()
    if not title:
        return None

    source_id = _source_id_for(doc)
    authors = _extract_authors(doc)
    abstract = _extract_abstract(doc)
    keywords = _extract_keywords(doc)
    country_codes = _extract_country_codes(doc)
    published_at = _parse_date(doc.get("docdt"))

    # Prefer the human-facing URL; fall back to PDF URL
    url = (doc.get("url") or doc.get("pdfurl") or "").strip() or None
    source_url = url

    # Major themes as additional keywords
    majtheme = doc.get("majtheme")
    if isinstance(majtheme, dict):
        themes = majtheme.get("theme") or []
        if isinstance(themes, str):
            themes = [themes]
        for theme in themes:
            t = str(theme).strip()
            if t and t not in keywords:
                keywords.append(t)

    # Include country codes as keywords for downstream India filtering
    for code in country_codes:
        tag = f"country:{code}"
        if tag not in keywords:
            keywords.append(tag)

    # Preserve country codes in raw_metadata for explicit filtering
    raw: dict = {**doc, "_country_codes": country_codes}

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract or None,
        "doi": None,  # WDS documents rarely have DOIs in the metadata
        "url": url,
        "published_at": published_at,
        "paper_type": "working_paper",
        "jel_codes": [],
        "keywords": keywords,
        "source_id": source_id,
        "source_url": source_url,
        "raw_metadata": raw,
    }


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class WorldBankSensor(BaseSensor):
    """Fetch recent working papers from the World Bank Documents & Reports API.

    Queries the WDS v2 endpoint for each configured document type, paginating
    up to _MAX_PAGES pages per document type.  Uses a start-date filter
    derived from watch state (or a 30-day lookback on first run).

    Attributes:
        name: Sensor identifier used for DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: Conservative 0.5 requests/second (2s between calls).
        API_URL: Base URL for the World Bank WDS API.
        DOC_TYPES: List of document type strings to query.
    """

    name = "worldbank"
    watch = "papers"
    rate_limit = 0.5  # 2 seconds between requests -- polite

    API_URL = _WDS_BASE
    DOC_TYPES = _DOC_TYPES

    def _from_date(self) -> str:
        """Return the ISO date string to use as the strdate filter parameter.

        Reads worldbank_last_fetched from watch state.  Falls back to 30 days
        ago on first run.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = state.get("worldbank_last_fetched")
        if last_fetched:
            try:
                datetime.strptime(last_fetched, "%Y-%m-%d")
                return last_fetched
            except ValueError:
                pass
        return (
            datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

    def _build_url(self, doc_type: str, from_date: str, offset: int) -> str:
        """Construct a WDS API query URL.

        Args:
            doc_type: Document type string, e.g. 'Working Paper'.
            from_date: Lower bound date filter in 'YYYY-MM-DD' format.
            offset: Zero-based result offset for pagination.

        Returns:
            Fully-qualified URL string.
        """
        params: dict[str, str | int] = {
            "format": "json",
            "docty": doc_type,
            "srt": "docdt",
            "order": "desc",
            "rows": _ROWS_PER_PAGE,
            "os": offset,
            "strdate": from_date,
        }
        return f"{self.API_URL}?{urlencode(params)}"

    def _fetch_doc_type(self, doc_type: str, from_date: str) -> list[dict]:
        """Fetch all pages for a single document type.

        Args:
            doc_type: Document type string to query.
            from_date: Lower bound date filter string.

        Returns:
            List of raw document dicts from the API.
        """
        all_docs: list[dict] = []

        for page in range(_MAX_PAGES):
            offset = page * _ROWS_PER_PAGE
            url = self._build_url(doc_type, from_date, offset)

            try:
                response = self.fetch_json(url)
            except Exception as exc:
                print(
                    f"[worldbank] fetch failed (docty={doc_type!r}, offset={offset}): {exc}",
                    file=sys.stderr,
                )
                break

            # The 'documents' field is a dict of doc_id -> doc_data, not a list.
            # Some responses also include numeric total/rows keys at the top level.
            documents_raw = response.get("documents")
            if not documents_raw or not isinstance(documents_raw, dict):
                break

            # Filter out the top-level metadata keys the API includes inline
            # (e.g. 'facets', 'total', etc.) -- real documents have a string id key.
            docs: list[dict] = []
            for key, val in documents_raw.items():
                if isinstance(val, dict) and "display_title" in val:
                    docs.append(val)

            if not docs:
                break

            all_docs.extend(docs)
            print(
                f"[worldbank] docty={doc_type!r} page={page + 1}: "
                f"fetched {len(docs)} docs",
                file=sys.stderr,
            )

            # Stop early if we got fewer results than a full page
            if len(docs) < _ROWS_PER_PAGE:
                break

        return all_docs

    def collect(self) -> list[dict]:
        """Fetch recent World Bank working papers via the WDS API.

        Steps:
        1. Determine the from_date via watch state (or 30-day fallback).
        2. For each configured document type, paginate the WDS API.
        3. Parse each document into the standard paper dict.
        4. Deduplicate by source_id across document types.
        5. Update watch state with today's date.

        Returns:
            List of paper dicts suitable for BaseSensor.run() ingestion.
        """
        from_date = self._from_date()
        print(
            f"[worldbank] fetching papers from {from_date} "
            f"across {len(self.DOC_TYPES)} document type(s)",
            file=sys.stderr,
        )

        papers: list[dict] = []
        seen_source_ids: set[str] = set()

        for doc_type in self.DOC_TYPES:
            raw_docs = self._fetch_doc_type(doc_type, from_date)

            for doc in raw_docs:
                parsed = _parse_doc(doc)
                if parsed is None:
                    continue

                source_id = parsed["source_id"]
                if not source_id or source_id in seen_source_ids:
                    continue
                seen_source_ids.add(source_id)

                papers.append(parsed)

        # Update watch state
        state = _load_state()
        state["worldbank_last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[worldbank] failed to save state: {exc}", file=sys.stderr)

        print(f"[worldbank] collected {len(papers)} papers", file=sys.stderr)
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = WorldBankSensor()
    sensor.run()
