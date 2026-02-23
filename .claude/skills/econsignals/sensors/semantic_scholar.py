"""Semantic Scholar sensor for EconSignals.

Fetches recent papers for tracked authors from the Semantic Scholar Graph API.
Also enriches existing DB papers that lack S2 data.  Provides standalone
enrichment helpers (citations, references, recommendations) for use by the
deep_paper lens.

Usage:
    python sensors/semantic_scholar.py

Environment variables:
    SEMANTIC_SCHOLAR_API_KEY   Optional API key for higher rate limits.
                               If absent, falls back to free-tier (1 req/s).
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

_API_BASE = "https://api.semanticscholar.org/graph/v1"
_RECS_BASE = "https://api.semanticscholar.org/recommendations/v1"

_PAPER_FIELDS = (
    "paperId,title,abstract,year,citationCount,referenceCount,"
    "authors,externalIds,publicationDate,journal,fieldsOfStudy"
)
_AUTHOR_FIELDS = "authorId,name,affiliations,paperCount,citationCount,hIndex"

# How many author papers to fetch per request
_AUTHOR_PAPERS_LIMIT = 50
# How many papers to enrich per run (avoid rate-limit exhaustion)
_ENRICH_LIMIT = 20
# How many recommendations to fetch per paper
_RECS_LIMIT = 10
# Default lookback for "recent" paper detection on authors
_DEFAULT_LOOKBACK_DAYS = 30

_STATE_PATH = PROJ_ROOT / "watches" / "authors" / "state.json"

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/authors/state.json.

    Returns:
        State dict, or empty dict if missing or corrupt.
    """
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically write state to watches/authors/state.json.

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


def _s2_headers(api_key: str | None) -> dict:
    """Build request headers, injecting API key when available.

    Args:
        api_key: Semantic Scholar API key, or None for anonymous requests.

    Returns:
        Header dict.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _parse_paper(raw: dict) -> dict | None:
    """Convert a raw S2 paper object to the standard paper dict format.

    Args:
        raw: Dict from the Semantic Scholar Graph API.

    Returns:
        Paper dict compatible with BaseSensor.collect() return contract, or
        None if the record lacks a usable title.
    """
    title = (raw.get("title") or "").strip()
    if not title:
        return None

    paper_id = raw.get("paperId") or ""

    # External IDs
    ext_ids = raw.get("externalIds") or {}
    doi = (ext_ids.get("DOI") or "").strip() or None
    arxiv_id = (ext_ids.get("ArXiv") or "").strip() or None

    # Canonical URL: DOI preferred, then S2 page
    if doi:
        url = f"https://doi.org/{doi}"
        source_url = url
    elif arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
        source_url = url
    else:
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
        source_url = url

    # Publication date: prefer publicationDate (YYYY-MM-DD), fall back to year
    pub_date: str | None = (raw.get("publicationDate") or "").strip() or None
    if not pub_date:
        year = raw.get("year")
        if year:
            pub_date = f"{year}-01-01"

    # Authors
    author_objects: list[dict] = raw.get("authors") or []
    author_names: list[str] = []
    author_details: list[dict] = []
    for a in author_objects:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        author_names.append(name)
        affiliations = a.get("affiliations") or []
        affiliation = affiliations[0].strip() if affiliations else None
        author_details.append(
            {
                "name": name,
                "semantic_scholar_id": (a.get("authorId") or "").strip() or None,
                "affiliation": affiliation,
            }
        )

    # Fields of study as keywords
    fields_of_study: list[str] = raw.get("fieldsOfStudy") or []
    journal_obj = raw.get("journal") or {}
    journal_name = (journal_obj.get("name") or "").strip()
    keywords: list[str] = [f for f in fields_of_study if f]
    if journal_name:
        keywords.append(journal_name)

    # Paper type heuristic
    if doi and journal_name:
        paper_type = "journal_article"
    elif arxiv_id:
        paper_type = "working_paper"
    else:
        paper_type = "working_paper"

    # S2-specific metadata for raw_metadata
    s2_meta = {
        "paperId": paper_id,
        "citationCount": raw.get("citationCount"),
        "referenceCount": raw.get("referenceCount"),
        "journal": journal_obj,
        "fieldsOfStudy": fields_of_study,
        "externalIds": ext_ids,
    }

    return {
        "title": title,
        "authors": author_names,
        "abstract": (raw.get("abstract") or "").strip() or None,
        "doi": doi,
        "url": url,
        "published_at": pub_date,
        "paper_type": paper_type,
        "jel_codes": [],
        "keywords": keywords,
        "source_id": paper_id,
        "source_url": source_url,
        "raw_metadata": s2_meta,
        "_author_details": author_details,
        "_s2_paper_id": paper_id,
    }


def _cutoff_date(state: dict, author_s2_id: str) -> str:
    """Determine the earliest publication date to accept for a given author.

    Args:
        state: Loaded state dict.
        author_s2_id: Semantic Scholar author ID.

    Returns:
        ISO date string 'YYYY-MM-DD'.
    """
    key = f"s2_author_last_fetched_{author_s2_id}"
    last = state.get(key)
    if last:
        try:
            datetime.fromisoformat(last)
            return last
        except ValueError:
            pass
    return (datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d"
    )


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class SemanticScholarSensor(BaseSensor):
    """Fetch author papers and enrich existing records via Semantic Scholar.

    Steps per run:
    1. Fetch recent papers for every tracked author with a semantic_scholar_id.
    2. Enrich high-relevance DB papers that have a DOI but no S2 source record.

    Also exposes citation graph helpers (get_paper_citations,
    get_paper_references, get_recommendations) for use by the deep_paper lens.

    Attributes:
        name: Sensor identifier used for DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: Requests per second (free tier = 1; authenticated = higher).
    """

    name = "semantic_scholar"
    watch = "authors"
    rate_limit = 1.0  # Free tier: 1 req/s

    API_BASE = _API_BASE
    PAPER_FIELDS = _PAPER_FIELDS
    AUTHOR_FIELDS = _AUTHOR_FIELDS

    def __init__(self) -> None:
        super().__init__()
        self._api_key: str | None = os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None
        # Bump rate limit when authenticated (S2 allows ~10 req/s with a key)
        if self._api_key:
            self.rate_limit = 10.0
            self.limiter.min_interval = 1.0 / self.rate_limit

    def _headers(self) -> dict:
        return _s2_headers(self._api_key)

    # ------------------------------------------------------------------
    # Author paper fetching
    # ------------------------------------------------------------------

    def _fetch_author_papers(self, author_s2_id: str) -> list[dict]:
        """Fetch recent papers for a single Semantic Scholar author.

        Args:
            author_s2_id: Semantic Scholar author ID.

        Returns:
            List of raw S2 paper dicts from the API response.
        """
        params = urlencode({"fields": _PAPER_FIELDS, "limit": _AUTHOR_PAPERS_LIMIT})
        url = f"{_API_BASE}/author/{author_s2_id}/papers?{params}"
        try:
            response = self.fetch_json(url, headers=self._headers())
        except Exception as exc:
            print(
                f"[semantic_scholar] author {author_s2_id} fetch failed: {exc}",
                file=sys.stderr,
            )
            return []
        return response.get("data") or []

    # ------------------------------------------------------------------
    # Paper search / enrichment
    # ------------------------------------------------------------------

    def _search_paper(self, query: str) -> list[dict]:
        """Search S2 for papers matching a title query.

        Args:
            query: Title string to search.

        Returns:
            List of raw S2 paper dicts from the search endpoint.
        """
        params = urlencode({"query": query, "fields": _PAPER_FIELDS, "limit": 5})
        url = f"{_API_BASE}/paper/search?{params}"
        try:
            response = self.fetch_json(url, headers=self._headers())
        except Exception as exc:
            print(
                f"[semantic_scholar] search failed for {query!r}: {exc}",
                file=sys.stderr,
            )
            return []
        return response.get("data") or []

    def _fetch_paper_by_id(self, s2_id: str) -> dict | None:
        """Fetch full S2 paper details by S2 paper ID.

        Args:
            s2_id: Semantic Scholar paper ID (hash string).

        Returns:
            Raw S2 paper dict, or None on failure.
        """
        params = urlencode({"fields": _PAPER_FIELDS})
        url = f"{_API_BASE}/paper/{s2_id}?{params}"
        try:
            return self.fetch_json(url, headers=self._headers())
        except Exception as exc:
            print(f"[semantic_scholar] paper fetch failed ({s2_id}): {exc}", file=sys.stderr)
            return None

    def _fetch_paper_by_doi(self, doi: str) -> dict | None:
        """Fetch S2 paper details by DOI using the DOI: prefix syntax.

        Args:
            doi: Bare DOI string (e.g. '10.1257/aer.20200707').

        Returns:
            Raw S2 paper dict, or None on failure.
        """
        params = urlencode({"fields": _PAPER_FIELDS})
        url = f"{_API_BASE}/paper/DOI:{doi}?{params}"
        try:
            return self.fetch_json(url, headers=self._headers())
        except Exception as exc:
            print(f"[semantic_scholar] DOI fetch failed ({doi}): {exc}", file=sys.stderr)
            return None

    # ------------------------------------------------------------------
    # Collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Run the main collection loop.

        1. Query tracked authors with semantic_scholar_id from DB.
        2. For each: fetch their recent papers, filter by cutoff date.
        3. Enrich DB papers that have a DOI but no S2 source record yet.
        4. Update per-author fetch timestamps in state.

        Returns:
            List of paper dicts ready for BaseSensor.run() ingestion.
        """
        from lib.db import get_tracked_authors, get_db

        state = _load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        papers: list[dict] = []
        seen_s2_ids: set[str] = set()

        # --- Phase 1: tracked author papers ---
        tracked = get_tracked_authors()
        s2_authors = [a for a in tracked if a.get("semantic_scholar_id")]

        print(
            f"[semantic_scholar] {len(s2_authors)} tracked authors with S2 ID",
            file=sys.stderr,
        )

        for author in s2_authors:
            s2_id = author["semantic_scholar_id"]
            cutoff = _cutoff_date(state, s2_id)

            print(
                f"[semantic_scholar] fetching papers for author {author['name']!r} "
                f"({s2_id}) since {cutoff}",
                file=sys.stderr,
            )

            raw_papers = self._fetch_author_papers(s2_id)
            author_count = 0

            for raw in raw_papers:
                # Filter by publication date where available
                pub_date = (raw.get("publicationDate") or "").strip()
                if not pub_date:
                    year = raw.get("year")
                    pub_date = f"{year}-01-01" if year else ""

                if pub_date and pub_date < cutoff:
                    continue

                parsed = _parse_paper(raw)
                if not parsed:
                    continue

                s2_paper_id = parsed.get("_s2_paper_id") or ""
                if not s2_paper_id or s2_paper_id in seen_s2_ids:
                    continue
                seen_s2_ids.add(s2_paper_id)

                papers.append(parsed)
                author_count += 1

            print(
                f"[semantic_scholar] {author['name']!r}: {author_count} new papers",
                file=sys.stderr,
            )

            # Record fetch timestamp for this author
            state[f"s2_author_last_fetched_{s2_id}"] = today

        # --- Phase 2: enrich DB papers that have DOI but no S2 source ---
        print("[semantic_scholar] enriching DB papers without S2 data...", file=sys.stderr)

        conn = get_db()
        rows = conn.execute(
            """
            SELECT p.id, p.doi, p.title
            FROM papers p
            WHERE p.doi IS NOT NULL
              AND p.doi != ''
              AND NOT EXISTS (
                  SELECT 1 FROM paper_sources ps
                  WHERE ps.paper_id = p.id AND ps.source = 'semantic_scholar'
              )
            ORDER BY p.relevance_score DESC
            LIMIT ?
            """,
            (_ENRICH_LIMIT,),
        ).fetchall()
        conn.close()

        enrich_count = 0
        for row in rows:
            doi = row["doi"]
            raw = self._fetch_paper_by_doi(doi)
            if not raw:
                continue

            parsed = _parse_paper(raw)
            if not parsed:
                continue

            s2_paper_id = parsed.get("_s2_paper_id") or ""
            if s2_paper_id in seen_s2_ids:
                continue
            seen_s2_ids.add(s2_paper_id)

            # Preserve the DB DOI for dedup; it may differ slightly from S2's
            if not parsed.get("doi"):
                parsed["doi"] = doi

            papers.append(parsed)
            enrich_count += 1

        print(
            f"[semantic_scholar] enriched {enrich_count} papers via DOI lookup",
            file=sys.stderr,
        )

        # Persist updated state
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[semantic_scholar] failed to save state: {exc}", file=sys.stderr)

        print(f"[semantic_scholar] total collected: {len(papers)}", file=sys.stderr)
        return papers

    # ------------------------------------------------------------------
    # run() override: author upsert + paper-author linking
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute the sensor with S2-specific author upsert logic.

        Overrides BaseSensor.run() to also upsert semantic_scholar_id on
        author records and create paper-author links after ingest.

        Returns:
            Stats dict (same shape as BaseSensor.run()).
        """
        from lib.db import (
            log_sensor_start,
            log_sensor_end,
            upsert_author,
            link_paper_author,
        )
        from lib.dedup import ingest_paper
        from lib.normalize import normalize_author_name

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                author_details: list[dict] = item.pop("_author_details", [])
                item.pop("_s2_paper_id", None)  # internal key, not for ingest

                try:
                    source_id = item.pop("source_id")
                    source_url = item.pop("source_url", None)
                    raw_metadata = item.pop("raw_metadata", None)

                    paper_id, is_new = ingest_paper(
                        paper_data=item,
                        source=self.name,
                        source_id=source_id,
                        source_url=source_url,
                        raw_metadata=raw_metadata,
                    )
                    if is_new:
                        self.stats["new"] = int(self.stats["new"]) + 1

                    # Upsert authors with semantic_scholar_id
                    for position, ad in enumerate(author_details):
                        name = ad.get("name") or ""
                        if not name:
                            continue
                        try:
                            author_id = upsert_author(
                                name=name,
                                name_normalized=normalize_author_name(name),
                                semantic_scholar_id=ad.get("semantic_scholar_id"),
                                affiliation=ad.get("affiliation"),
                                auto_discovered=1,
                            )
                            if author_id and paper_id:
                                link_paper_author(paper_id, author_id, position)
                        except Exception as author_exc:
                            print(
                                f"  [semantic_scholar] author upsert failed ({name!r}): "
                                f"{author_exc}",
                                file=sys.stderr,
                            )

                except Exception as exc:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(f"  [semantic_scholar] ingest error: {exc}", file=sys.stderr)

            log_sensor_end(run_id, "success", self.stats["found"], self.stats["new"])

        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"Sensor {self.name} failed: {exc}", file=sys.stderr)

        result = {
            "sensor": self.name,
            "watch": self.watch,
            "status": "error" if "error_message" in self.stats else "success",
            **self.stats,
        }
        print(json.dumps(result))
        return result


# ---------------------------------------------------------------------------
# Enrichment helpers (for deep_paper lens, not called during collect)
# ---------------------------------------------------------------------------


def _resolve_s2_id(sensor: SemanticScholarSensor, doi: str) -> str | None:
    """Resolve a DOI to a Semantic Scholar paper ID.

    Args:
        sensor: A SemanticScholarSensor instance (for fetch_json + rate limiter).
        doi: Bare DOI string.

    Returns:
        S2 paper ID string, or None if not found.
    """
    raw = sensor._fetch_paper_by_doi(doi)
    if raw:
        return (raw.get("paperId") or "").strip() or None
    return None


def get_paper_citations(paper_doi: str) -> list[dict]:
    """Get papers that cite the given paper.

    Fetches the /paper/{id}/citations endpoint and returns parsed paper dicts
    for each citing work.

    Args:
        paper_doi: Bare DOI string (e.g. '10.1257/aer.20200707').

    Returns:
        List of parsed paper dicts.  Returns empty list on failure.

    Example:
        >>> results = get_paper_citations("10.1257/aer.20200707")
        >>> results[0]["title"]
        'Some Citing Paper Title'
    """
    sensor = SemanticScholarSensor()
    s2_id = _resolve_s2_id(sensor, paper_doi)
    if not s2_id:
        print(
            f"[semantic_scholar] citations: could not resolve DOI {paper_doi!r}",
            file=sys.stderr,
        )
        return []

    params = urlencode({"fields": _PAPER_FIELDS, "limit": 100})
    url = f"{_API_BASE}/paper/{s2_id}/citations?{params}"
    try:
        response = sensor.fetch_json(url, headers=sensor._headers())
    except Exception as exc:
        print(f"[semantic_scholar] citations fetch failed: {exc}", file=sys.stderr)
        return []

    results: list[dict] = []
    for entry in response.get("data") or []:
        # Citations endpoint wraps the paper under 'citingPaper'
        raw = entry.get("citingPaper") or {}
        parsed = _parse_paper(raw)
        if parsed:
            parsed.pop("_author_details", None)
            parsed.pop("_s2_paper_id", None)
            results.append(parsed)

    return results


def get_paper_references(paper_doi: str) -> list[dict]:
    """Get papers referenced by the given paper.

    Fetches the /paper/{id}/references endpoint and returns parsed paper dicts
    for each referenced work.

    Args:
        paper_doi: Bare DOI string (e.g. '10.1257/aer.20200707').

    Returns:
        List of parsed paper dicts.  Returns empty list on failure.

    Example:
        >>> results = get_paper_references("10.1257/aer.20200707")
        >>> len(results)
        42
    """
    sensor = SemanticScholarSensor()
    s2_id = _resolve_s2_id(sensor, paper_doi)
    if not s2_id:
        print(
            f"[semantic_scholar] references: could not resolve DOI {paper_doi!r}",
            file=sys.stderr,
        )
        return []

    params = urlencode({"fields": _PAPER_FIELDS, "limit": 100})
    url = f"{_API_BASE}/paper/{s2_id}/references?{params}"
    try:
        response = sensor.fetch_json(url, headers=sensor._headers())
    except Exception as exc:
        print(f"[semantic_scholar] references fetch failed: {exc}", file=sys.stderr)
        return []

    results: list[dict] = []
    for entry in response.get("data") or []:
        # References endpoint wraps the paper under 'citedPaper'
        raw = entry.get("citedPaper") or {}
        parsed = _parse_paper(raw)
        if parsed:
            parsed.pop("_author_details", None)
            parsed.pop("_s2_paper_id", None)
            results.append(parsed)

    return results


def get_recommendations(paper_doi: str) -> list[dict]:
    """Get semantically similar paper recommendations for the given paper.

    Uses the Semantic Scholar Recommendations API (/recommendations/v1/papers/
    forpaper/{id}).

    Args:
        paper_doi: Bare DOI string (e.g. '10.1257/aer.20200707').

    Returns:
        List of parsed paper dicts (up to _RECS_LIMIT).  Returns empty list
        on failure or when the paper is not indexed by S2.

    Example:
        >>> recs = get_recommendations("10.1257/aer.20200707")
        >>> recs[0]["title"]
        'A Related Paper Title'
    """
    sensor = SemanticScholarSensor()
    s2_id = _resolve_s2_id(sensor, paper_doi)
    if not s2_id:
        print(
            f"[semantic_scholar] recommendations: could not resolve DOI {paper_doi!r}",
            file=sys.stderr,
        )
        return []

    params = urlencode({"fields": _PAPER_FIELDS, "limit": _RECS_LIMIT})
    url = f"{_RECS_BASE}/papers/forpaper/{s2_id}?{params}"
    try:
        response = sensor.fetch_json(url, headers=sensor._headers())
    except Exception as exc:
        print(f"[semantic_scholar] recommendations fetch failed: {exc}", file=sys.stderr)
        return []

    results: list[dict] = []
    for raw in response.get("recommendedPapers") or []:
        parsed = _parse_paper(raw)
        if parsed:
            parsed.pop("_author_details", None)
            parsed.pop("_s2_paper_id", None)
            results.append(parsed)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = SemanticScholarSensor()
    sensor.run()
