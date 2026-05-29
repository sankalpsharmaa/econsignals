"""World Bank Policy Research Working Papers sensor.

The World Bank's Development Economics (DEC) group publishes the Policy Research
Working Paper (PRWP) series, the single largest stream of applied-development
working papers in economics. Many India-focused development papers (poverty,
agriculture, urbanization, public finance) appear here months before, or instead
of, a journal version, and OpenAlex/Crossref coverage of the series is patchy and
lagged. The series is the development-economics working-paper layer the broad
OpenAlex field query routinely misses.

This sensor reads the World Bank Documents & Reports (WDS) JSON API at
    https://search.worldbank.org/api/v3/wds
filtered to docty="Policy Research Working Paper" over a configurable lookback
window (default 30 days). Override the window with the ECONSIGNALS_WORLDBANK_DAYS
env var.

Response structure (JSON), one entry per document under the ``documents`` object,
keyed by a ``D``-prefixed id (the object also carries a non-document ``facets``
key, which is skipped):
    id              clean numeric document id (source_id)
    display_title   document title (falls back to docna["0"]["docna"])
    authors         dict {"0": {"author": name}, ...}, ordered by integer key
    abstracts        {"cdata!": abstract text}  (key literally "cdata!")
    docdt           publication date as a full ISO timestamp
    dois            registered DOI (prefix 10.1596), enabling cross-source dedup
    url             canonical documents.worldbank.org URL
    pdfurl          direct PDF link (recorded in raw_metadata)
    repnb           report number, e.g. "WPS11398" (recorded in raw_metadata)

Every PRWP carries a real Crossref-registered DOI, so this sensor populates
``doi``; that lets a World Bank paper merge with the same paper arriving from
OpenAlex or Crossref instead of duplicating.

Usage:
    python -m econsignals.sensors.worldbank
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from econsignals.sensors._base import BaseSensor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_URL = "https://search.worldbank.org/api/v3/wds"

# Document type filter: the Policy Research Working Paper series only.
_DOCTY = "Policy Research Working Paper"

# Default lookback window in days, overridable via ECONSIGNALS_WORLDBANK_DAYS.
_DEFAULT_DAYS = 30

# Rows per request. A 30-day window yields ~30-60 PRWPs; 100 covers it without
# needing the API's offset ("os") pagination.
_ROWS = 100

# Collapse runs of whitespace (including the literal newlines WDS embeds in
# titles and abstracts) into single spaces.
_RE_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Helpers (pure functions, unit-testable without network or DB)
# ---------------------------------------------------------------------------


def _clean_text(raw: str | None) -> str | None:
    """Collapse embedded newlines and whitespace runs, or return None.

    WDS embeds raw newlines inside title and abstract text. Normalise them to
    single spaces so downstream display and dedup see clean strings.

    Args:
        raw: Raw text from the API.

    Returns:
        Whitespace-normalised text, or None if empty.
    """
    if not raw:
        return None
    cleaned = _RE_WHITESPACE.sub(" ", raw).strip()
    return cleaned or None


def _extract_title(doc: dict) -> str | None:
    """Return a document's title, preferring display_title.

    Args:
        doc: One WDS document object.

    Returns:
        Cleaned title string, or None if absent.
    """
    title = doc.get("display_title")
    if not title:
        # Fall back to the structured docna list when display_title is absent.
        docna = doc.get("docna")
        if isinstance(docna, dict):
            first = docna.get("0")
            if isinstance(first, dict):
                title = first.get("docna")
    return _clean_text(title)


def _extract_authors(doc: dict) -> list[str]:
    """Return a document's authors, ordered by their integer index key.

    WDS stores authors as a dict {"0": {"author": name}, "1": {...}, ...}.
    Iteration order is restored by sorting on the integer key.

    Args:
        doc: One WDS document object.

    Returns:
        List of author name strings (possibly empty).
    """
    authors = doc.get("authors")
    if not isinstance(authors, dict):
        return []
    ordered: list[str] = []
    for _, entry in sorted(authors.items(), key=lambda kv: int(kv[0])):
        if isinstance(entry, dict):
            name = (entry.get("author") or "").strip()
            if name:
                ordered.append(name)
    return ordered


def _extract_abstract(doc: dict) -> str | None:
    """Return a document's abstract, or None.

    The abstract lives under abstracts["cdata!"] (the key carries a literal
    exclamation mark). Roughly one document in thirty has no abstract.

    Args:
        doc: One WDS document object.

    Returns:
        Cleaned abstract string, or None.
    """
    abstracts = doc.get("abstracts")
    if isinstance(abstracts, dict):
        return _clean_text(abstracts.get("cdata!"))
    return None


def _parse_response(payload: dict) -> list[dict]:
    """Parse one WDS JSON response into standard paper dicts.

    Iterates the ``documents`` object, skipping the non-document ``facets``
    entry (identified by a missing ``id``), and maps each document to the
    BaseSensor.collect() paper contract.

    Args:
        payload: Decoded WDS JSON response.

    Returns:
        List of paper dicts. Returns an empty list when ``documents`` is absent
        or malformed.
    """
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        return []

    papers: list[dict] = []
    for doc in documents.values():
        # The "facets" entry is not a document and carries no id; skip it.
        if not isinstance(doc, dict):
            continue
        doc_id = doc.get("id")
        if not doc_id:
            continue

        title = _extract_title(doc)
        if not title:
            continue

        docdt = doc.get("docdt") or ""
        published_at = docdt[:10] if len(docdt) >= 10 else None

        doi = (doc.get("dois") or "").strip() or None
        url = (doc.get("url") or "").strip() or None

        papers.append(
            {
                "title": title,
                "authors": _extract_authors(doc),
                "abstract": _extract_abstract(doc),
                "doi": doi,
                "url": url,
                "published_at": published_at,
                "paper_type": "working_paper",
                "jel_codes": [],
                "keywords": [],
                "source_id": str(doc_id),
                "source_url": url,
                "raw_metadata": {
                    "source": "worldbank_wds",
                    "repnb": doc.get("repnb"),
                    "pdfurl": doc.get("pdfurl"),
                    "guid": doc.get("guid"),
                },
            }
        )

    return papers


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class WorldBankSensor(BaseSensor):
    """Collect recent World Bank Policy Research Working Papers from the WDS API.

    Fetches the PRWP series over a lookback window, parses each document, and
    emits standard paper dicts. Cross-run de-duplication is handled by the
    ingest layer (source='worldbank', source_id=numeric document id); the
    registered DOI lets papers merge with the same work from other sources.

    Attributes:
        name: Sensor identifier ('worldbank'), matching the prestige table.
        watch: Watch category ('papers').
        rate_limit: 0.5 req/s -- one request every 2 seconds (polite to the API).
    """

    name = "worldbank"
    watch = "papers"
    rate_limit = 0.5

    def _lookback_days(self) -> int:
        """Return the lookback window in days.

        Reads ECONSIGNALS_WORLDBANK_DAYS if set and valid, else the default.

        Returns:
            Positive integer number of days.
        """
        raw = os.environ.get("ECONSIGNALS_WORLDBANK_DAYS", "").strip()
        if raw:
            try:
                days = int(raw)
                if days > 0:
                    return days
            except ValueError:
                print(
                    f"[worldbank] invalid ECONSIGNALS_WORLDBANK_DAYS={raw!r}; "
                    f"using default {_DEFAULT_DAYS}",
                    file=sys.stderr,
                )
        return _DEFAULT_DAYS

    def _build_url(self) -> str:
        """Build the WDS query URL for the configured lookback window.

        Returns:
            Fully-encoded request URL.
        """
        start = datetime.now(timezone.utc) - timedelta(days=self._lookback_days())
        params = {
            "format": "json",
            "rows": _ROWS,
            "docty": _DOCTY,
            "strdate": start.strftime("%Y-%m-%d"),
        }
        return f"{_API_URL}?{urlencode(params)}"

    def collect(self) -> list[dict]:
        """Fetch and parse recent Policy Research Working Papers.

        Steps:
        1. Build the WDS query URL for the lookback window.
        2. Fetch and decode the JSON response.
        3. Parse documents into paper dicts.

        Returns:
            List of paper dicts for the window.
        """
        url = self._build_url()
        print(
            f"[worldbank] fetching PRWPs since "
            f"{self._lookback_days()}d ago",
            file=sys.stderr,
        )

        payload = self.fetch_json(url)
        papers = _parse_response(payload)

        print(f"[worldbank] collected {len(papers)} papers", file=sys.stderr)
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = WorldBankSensor()
    sensor.run()
