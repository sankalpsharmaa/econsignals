"""Semantic Scholar Recommendations sensor.

Semantic Scholar's recommendation engine surfaces papers that are close to a
set of seed papers in its citation/embedding space. EconSignals already knows
which papers matter to this user -- the highest-relevance papers the other
sensors have scored -- so this sensor turns that signal back into discovery:
it hands the user's top-scoring DOIs to Semantic Scholar as positive seeds and
ingests the recommendations. This is the one channel whose input is the user's
own revealed preferences rather than a fixed query or curated report, so it
fills gaps the JEL/keyword scorers cannot reach (adjacent work that shares no
keywords but sits in the same citation neighbourhood).

API (public, no key required; see verification notes below):
    POST https://api.semanticscholar.org/recommendations/v1/papers/
        body: {"positivePaperIds": ["DOI:10.1257/aer.20190623", ...],
               "negativePaperIds": []}
        query: ?fields=title,abstract,year,authors,externalIds,url&limit=N
        -> {"recommendedPapers": [{paperId, externalIds{DOI,...}, url, title,
                                   year, authors:[{authorId,name}], abstract}, ...]}

    The forpaper GET variant exists too
    (GET .../recommendations/v1/papers/forpaper/{id}) but the POST pooled-seed
    form is preferred: it issues one request for the whole seed set, and S2
    weights all seeds jointly, giving recommendations centred on the user's
    profile rather than a single paper.

Rate limits: the public (no-key) endpoint is shared and aggressively throttled
(roughly 1 request / second, often less under load) and returns HTTP 429 when
exceeded. This sensor makes a single POST per run, so it stays well inside the
limit; ``_fetch_recommendations`` mirrors fetch_url's retry/backoff (it
hand-rolls the POST because fetch_url is GET-only) so a transient 429, 5xx, or
network blip is retried rather than collapsing the run.

Seeds: built from the user's highest-relevance DOIs via
``db.get_top_papers``. With no seeds (empty DB, no DOIs) the sensor returns []
without calling the API.

Usage:
    python -m econsignals.sensors.semantic_scholar

Configuration (all optional):
    ECONSIGNALS_S2_SEED_COUNT   number of top papers to seed from (default 25)
    ECONSIGNALS_S2_LIMIT        max recommendations to request (default 100)
    ECONSIGNALS_S2_MIN_SCORE    min relevance_score a paper needs to seed
                                (default 0.0)
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from econsignals.sensors._base import _SSL_CTX, BaseSensor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECOMMEND_URL = "https://api.semanticscholar.org/recommendations/v1/papers/"

# Fields requested from S2. externalIds carries the DOI (when S2 has one);
# authors is a list of {authorId, name}.
_FIELDS = "title,abstract,year,authors,externalIds,url"

# Default seed/limit sizing. One paper-recommendation request per run.
_DEFAULT_SEED_COUNT = 25
_DEFAULT_LIMIT = 100


# ---------------------------------------------------------------------------
# Helpers (pure functions, unit-testable without network or DB)
# ---------------------------------------------------------------------------


def _seed_ids_from_papers(papers: list[dict], max_seeds: int) -> list[str]:
    """Turn EconSignals paper dicts into S2 ``DOI:<doi>`` seed identifiers.

    Only papers with a DOI can seed; S2's positivePaperIds accepts the
    ``DOI:<doi>`` prefix form. Order is preserved (callers pass papers already
    sorted by relevance), and duplicate DOIs are dropped.

    Args:
        papers: Paper dicts as returned by ``db.get_top_papers`` (each may
            carry a ``doi`` key).
        max_seeds: Maximum number of seed ids to return.

    Returns:
        List of ``DOI:<doi>`` strings, at most ``max_seeds`` long.
    """
    seeds: list[str] = []
    seen: set[str] = set()
    for paper in papers:
        doi = (paper.get("doi") or "").strip()
        if not doi or doi in seen:
            continue
        seen.add(doi)
        seeds.append(f"DOI:{doi}")
        if len(seeds) >= max_seeds:
            break
    return seeds


def _year_to_iso(year: object) -> str | None:
    """Map an S2 integer ``year`` to an ISO ``YYYY-01-01`` date, or None.

    S2 recommendations expose only a publication year, not a full date; the
    standard paper dict wants an ISO date, so the year maps to Jan 1.

    Args:
        year: The raw ``year`` field from an S2 record (int, str, or None).

    Returns:
        'YYYY-01-01', or None when the year is missing or implausible.
    """
    if year is None:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    if y < 1500 or y > 2100:
        return None
    return f"{y:04d}-01-01"


def _infer_paper_type(external_ids: dict) -> str:
    """Guess a paper_type from an S2 record's externalIds.

    S2 recommendations mix preprints, working papers, conference papers, and
    journal articles. A bare arXiv id with no DOI is almost always a preprint /
    working paper; anything carrying a DOI is treated as a published
    journal_article. This is a coarse heuristic, not authoritative metadata.

    Args:
        external_ids: The ``externalIds`` object from an S2 record.

    Returns:
        'working_paper' for DOI-less arXiv records, else 'journal_article'.
    """
    ids = external_ids or {}
    if not ids.get("DOI") and ids.get("ArXiv"):
        return "working_paper"
    return "journal_article"


def _parse_recommendations(payload: dict) -> list[dict]:
    """Parse an S2 recommendations response into standard paper dicts.

    Args:
        payload: Decoded JSON body with a ``recommendedPapers`` list.

    Returns:
        List of paper dicts conforming to the BaseSensor.collect() contract.
        Records without a usable S2 paperId are skipped (paperId is the
        source_id and must be present and unique).
    """
    recommended = payload.get("recommendedPapers") or []
    papers: list[dict] = []

    for rec in recommended:
        paper_id = (rec.get("paperId") or "").strip()
        if not paper_id:
            # No stable id to dedup on; skip rather than synthesise one.
            continue

        title = (rec.get("title") or "").strip()
        if not title:
            continue

        authors = [
            (a.get("name") or "").strip()
            for a in (rec.get("authors") or [])
            if (a.get("name") or "").strip()
        ]

        abstract_raw = (rec.get("abstract") or "").strip()
        abstract: str | None = abstract_raw or None

        external_ids = rec.get("externalIds") or {}
        doi = external_ids.get("DOI")
        doi = doi.strip() if isinstance(doi, str) and doi.strip() else None

        url = (rec.get("url") or "").strip() or None
        published_at = _year_to_iso(rec.get("year"))

        papers.append(
            {
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "doi": doi,
                "url": url,
                "published_at": published_at,
                "paper_type": _infer_paper_type(external_ids),
                "jel_codes": [],
                "keywords": [],
                "source_id": paper_id,
                "source_url": url,
                "raw_metadata": {
                    "source": "semantic_scholar",
                    "s2_paper_id": paper_id,
                    "external_ids": external_ids or None,
                    "year": rec.get("year"),
                },
            }
        )

    return papers


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class SemanticScholarSensor(BaseSensor):
    """Recommend papers from the user's own highest-relevance DOIs.

    Seeds Semantic Scholar's recommendation engine with the top-scoring papers
    EconSignals already holds and ingests the returned neighbours. Emits
    standard paper dicts; cross-run de-duplication is handled by the ingest
    layer (source='semantic_scholar', source_id=S2 paperId). Recommendations
    that re-surface a seed paper dedup against the existing row by DOI.

    Attributes:
        name: Sensor identifier ('semantic_scholar'), matching the prestige
            table entry (0.45).
        watch: Watch category ('papers').
        rate_limit: 0.5 req/s -- the public S2 endpoint is shared and throttled;
            this sensor makes a single request per run regardless.
    """

    name = "semantic_scholar"
    watch = "papers"
    rate_limit = 0.5

    def _config(self) -> tuple[int, int, float]:
        """Resolve seed count, recommendation limit, and min seed score.

        Reads ECONSIGNALS_S2_SEED_COUNT, ECONSIGNALS_S2_LIMIT, and
        ECONSIGNALS_S2_MIN_SCORE; falls back to module defaults on an unset or
        unparseable value.

        Returns:
            (seed_count, limit, min_score).
        """
        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, "").strip())
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "").strip())
            except (TypeError, ValueError):
                return default

        return (
            _int("ECONSIGNALS_S2_SEED_COUNT", _DEFAULT_SEED_COUNT),
            _int("ECONSIGNALS_S2_LIMIT", _DEFAULT_LIMIT),
            _float("ECONSIGNALS_S2_MIN_SCORE", 0.0),
        )

    def _fetch_recommendations(self, seed_ids: list[str], limit: int) -> dict:
        """POST the seed ids to S2 and return the decoded JSON body.

        Issues a JSON POST (BaseSensor.fetch_url only does GET), reusing the
        base SSL context and User-Agent and honouring the rate limiter and
        circuit breaker. Mirrors fetch_url's retry policy because the keyless
        S2 endpoint is throttled: a 429 backs off and retries, a 5xx retries
        with exponential backoff, network errors retry, and a non-retryable
        4xx (other than 429) opens the breaker and raises.

        Args:
            seed_ids: ``DOI:<doi>`` positive-seed identifiers.
            limit: Max recommendations to request.

        Returns:
            Decoded JSON dict (``{"recommendedPapers": [...]}``).

        Raises:
            RuntimeError: If the circuit breaker is open.
            HTTPError | URLError | TimeoutError: After all retries are exhausted.
        """
        if self.breaker.is_open:
            raise RuntimeError(f"Circuit breaker open for {self.name}")

        url = f"{_RECOMMEND_URL}?fields={_FIELDS}&limit={limit}"
        body = json.dumps(
            {"positivePaperIds": seed_ids, "negativePaperIds": []}
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                f"EconSignals/1.0 "
                f"(mailto:{os.environ.get('OPENALEX_EMAIL', 'econsignals@example.com')})"
            ),
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                req = Request(url, data=body, headers=headers, method="POST")
                with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                    data = resp.read()
                self.breaker.record_success()
                return json.loads(data)
            except HTTPError as exc:
                last_error = exc
                if exc.code == 429:
                    time.sleep(self.retry_backoff ** (attempt + 1) * 2)
                elif exc.code >= 500:
                    time.sleep(self.retry_backoff ** attempt)
                else:
                    self.breaker.record_failure()
                    raise
            except (URLError, TimeoutError) as exc:
                last_error = exc
                time.sleep(self.retry_backoff ** attempt)

        self.breaker.record_failure()
        raise last_error  # type: ignore[misc]

    def collect(self) -> list[dict]:
        """Build seeds from top papers, fetch recommendations, parse them.

        Steps:
        1. Resolve seed count / limit / min-score from env or defaults.
        2. Pull the user's highest-relevance papers and turn their DOIs into
           S2 seed ids.
        3. With no seeds, return [] without calling the API.
        4. POST the seeds, parse the response into paper dicts.

        Returns:
            List of recommended paper dicts (possibly empty).
        """
        from econsignals.lib.db import get_top_papers

        seed_count, limit, min_score = self._config()

        top_papers = get_top_papers(limit=seed_count * 4, min_score=min_score)
        seed_ids = _seed_ids_from_papers(top_papers, seed_count)

        if not seed_ids:
            print(
                "[semantic_scholar] no seed DOIs available; skipping",
                file=sys.stderr,
            )
            return []

        print(
            f"[semantic_scholar] seeding with {len(seed_ids)} DOIs, "
            f"requesting up to {limit} recommendations",
            file=sys.stderr,
        )

        try:
            payload = self._fetch_recommendations(seed_ids, limit)
        except Exception as exc:
            print(
                f"[semantic_scholar] recommendation request failed: {exc}",
                file=sys.stderr,
            )
            return []

        papers = _parse_recommendations(payload)
        print(
            f"[semantic_scholar] parsed {len(papers)} recommended papers",
            file=sys.stderr,
        )
        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = SemanticScholarSensor()
    sensor.run()
