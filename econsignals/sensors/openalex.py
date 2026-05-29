"""OpenAlex sensor for EconSignals.

Fetches recent economics papers from the OpenAlex API (openalex.org).
Covers 240M+ works with a free polite-pool API (100K req/day).

Usage:
    python sensors/openalex.py

Environment variables:
    OPENALEX_EMAIL   Email address for the OpenAlex polite pool (higher rate
                     limits). Falls back to the owner's address if unset.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from econsignals.sensors._base import BaseSensor, PROJ_ROOT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPENALEX_BASE = "https://api.openalex.org"
_MAX_PAGES = 3
_PER_PAGE = 50
_DEFAULT_LOOKBACK_DAYS = 7

# Gate on the work's PRIMARY field, not a low-score peripheral concept tag.
# fields/20 = "Economics, Econometrics and Finance" in the OpenAlex
# topics/fields hierarchy. Filtering here drops crypto/coffee/telemedicine/
# CompSci papers that merely carry a 0.27-score "Economics" concept.
_PRIMARY_FIELD_ID = "fields/20"

# Optional author-country scope (ISO-2, pipe-separated). UNSET by default: the
# field gate alone removes the off-field noise, so we collect economics broadly
# and let the relevance engine's India floor handle India-first *ranking* — a
# collection-wide country gate would permanently hide relevant non-India dev/
# urban work (a Kenya RCT, a US-cities JUE paper). Set
# ECONSIGNALS_OPENALEX_COUNTRIES="IN|BD|PK|LK|NP" to restrict collection instead.
_COUNTRIES_ENV = "ECONSIGNALS_OPENALEX_COUNTRIES"

# Polite-pool contact email. OpenAlex routes a valid, monitored address to the
# faster pool; a placeholder example.com does not reliably qualify.
_DEFAULT_EMAIL = "sankalp.sharma437@gmail.com"

# Map OpenAlex work type -> internal paper_type
_TYPE_MAP: dict[str, str] = {
    "article": "journal_article",
}

# Watches state file location
_STATE_PATH = PROJ_ROOT / "watches" / "papers" / "state.json"

# Repository-style DOI prefixes and hosts that frequently introduce
# non-economics drift into the feed.
_BLOCKED_DOI_PREFIXES = (
    "10.5281/",
    "10.17632/",
    "10.6084/",
)
_BLOCKED_DOMAINS = {
    "zenodo.org",
    "hal.science",
    "figshare.com",
    "osf.io",
    "researchsquare.com",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _invert_abstract(inverted_index: dict | None) -> str:
    """Convert an OpenAlex abstract_inverted_index to plain text.

    The inverted index maps each word to the list of positions at which it
    appears.  This function reassembles the tokens into the original order.

    Args:
        inverted_index: Dict mapping token strings to lists of integer
            positions, as returned by the OpenAlex API.  May be None or empty.

    Returns:
        Reconstructed abstract string, or empty string if input is unusable.

    Example:
        >>> _invert_abstract({"Hello": [0], "world": [1]})
        'Hello world'
    """
    if not inverted_index:
        return ""

    try:
        # Build position -> token mapping then sort by position
        position_map: dict[int, str] = {}
        for token, positions in inverted_index.items():
            for pos in positions:
                position_map[pos] = token

        if not position_map:
            return ""

        tokens = [position_map[i] for i in sorted(position_map)]
        return " ".join(tokens)
    except Exception:
        return ""


def _parse_doi(raw_doi: str | None) -> str | None:
    """Strip the 'https://doi.org/' prefix and return the bare DOI.

    Args:
        raw_doi: Full DOI URL string or bare DOI, or None.

    Returns:
        Bare DOI string (e.g. '10.1257/aer.20200707'), or None.
    """
    if not raw_doi:
        return None
    # OpenAlex returns "https://doi.org/10.xxx/..."
    return re.sub(r"^https?://doi\.org/", "", raw_doi.strip())


def _openalex_short_id(work_id: str | None) -> str:
    """Extract the short OpenAlex ID from a full URI.

    Args:
        work_id: Full OpenAlex work URI, e.g. 'https://openalex.org/W1234'.

    Returns:
        Short ID string, e.g. 'W1234'.  Falls back to the full string.
    """
    if not work_id:
        return ""
    # "https://openalex.org/W1234" -> "W1234"
    return work_id.rstrip("/").rsplit("/", 1)[-1]


def _domain_from_url(url: str | None) -> str:
    """Return lowercase netloc from a URL string, else empty string."""
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower().strip()
    except Exception:
        return ""


def _parse_work(work: dict) -> dict:
    """Convert a raw OpenAlex work object to the standard paper dict format.

    Args:
        work: Raw dict from the OpenAlex /works endpoint.

    Returns:
        Paper dict compatible with BaseSensor.collect() return contract.
        Returns an empty dict if the work has no title (unparseable).
    """
    title = (work.get("title") or "").strip()
    if not title:
        return {}

    # --- IDs and URL ---
    work_id = work.get("id", "")
    source_id = _openalex_short_id(work_id)
    doi = _parse_doi(work.get("doi"))

    # Prefer landing page URL; fall back to OpenAlex canonical URL
    primary = work.get("primary_location") or {}
    source_url = primary.get("landing_page_url") or work_id or None

    # Canonical URL: DOI landing page preferred
    url = f"https://doi.org/{doi}" if doi else source_url

    # --- Type ---
    raw_type = work.get("type") or ""
    paper_type = _TYPE_MAP.get(raw_type, "working_paper")

    # --- Date ---
    published_at = work.get("publication_date") or None

    # --- Abstract ---
    abstract = _invert_abstract(work.get("abstract_inverted_index"))

    # --- Authors ---
    authorships = work.get("authorships") or []
    authors: list[str] = []
    author_details: list[dict] = []

    for authorship in authorships:
        author_obj = authorship.get("author") or {}
        name = (author_obj.get("display_name") or "").strip()
        if not name:
            continue
        authors.append(name)

        # Extract affiliation from first institution
        institutions = authorship.get("institutions") or []
        first_inst = institutions[0] if institutions else {}
        affiliation = (first_inst.get("display_name") or "").strip() or None
        country = (first_inst.get("country_code") or "").strip() or None

        # Strip ORCID URL prefix
        raw_orcid = author_obj.get("orcid") or ""
        orcid = re.sub(r"^https?://orcid\.org/", "", raw_orcid).strip() or None

        author_details.append(
            {
                "name": name,
                "openalex_id": _openalex_short_id(author_obj.get("id")),
                "orcid": orcid,
                "affiliation": affiliation,
                "country": country,
            }
        )

    # --- Concepts / keywords ---
    concepts = work.get("concepts") or []
    keywords: list[str] = []
    for concept in concepts:
        level = concept.get("level")
        display = (concept.get("display_name") or "").strip()
        # Include levels 0 and 1 as keywords (top-level disciplinary fields)
        if display and level is not None and level <= 1:
            keywords.append(display)

    # Source display name (journal / repository)
    source_display = (primary.get("source") or {}).get("display_name") or None
    if source_display:
        keywords.append(source_display)

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract or None,
        "doi": doi,
        "url": url,
        "published_at": published_at,
        "paper_type": paper_type,
        "jel_codes": [],          # OpenAlex does not expose JEL codes directly
        "keywords": keywords,
        "source_id": source_id,
        "source_url": source_url,
        "raw_metadata": work,
        # Extra author detail stored for DB upsert in collect()
        "_author_details": author_details,
    }


def _is_repository_noise(parsed: dict) -> bool:
    """Return True if parsed work looks like repository-only noise."""
    doi = (parsed.get("doi") or "").lower()
    if any(doi.startswith(prefix) for prefix in _BLOCKED_DOI_PREFIXES):
        return True

    for key in ("url", "source_url"):
        domain = _domain_from_url(parsed.get(key))
        if any(domain == d or domain.endswith("." + d) for d in _BLOCKED_DOMAINS):
            return True
    return False


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
    # Write to a temp file in the same directory then rename for atomicity
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


def _resolve_email() -> str:
    """Return the OpenAlex polite-pool contact email.

    Prefers OPENALEX_EMAIL when set and non-empty, else the owner's address.
    Used for both the mailto query param and the request User-Agent so the
    polite pool is honored even when the env var is unset.

    Returns:
        A non-empty contact email string.
    """
    return os.environ.get("OPENALEX_EMAIL") or _DEFAULT_EMAIL


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class OpenAlexSensor(BaseSensor):
    """Fetch recent economics papers from the OpenAlex API.

    Queries the OpenAlex /works endpoint for papers whose PRIMARY field is
    Economics and whose authors are in South Asia, published since the last
    successful run (or 7 days ago on first run).  Paginates up to 3 pages
    (150 papers) per execution.

    Attributes:
        name: Sensor identifier, used for DB logging.
        watch: Watch category this sensor belongs to.
        rate_limit: Requests per second (OpenAlex polite pool allows ~10 rps).
    """

    name = "openalex"
    watch = "papers"
    rate_limit = 10.0  # OpenAlex polite pool: 100K req/day

    def _from_date(self) -> str:
        """Return the ISO date string to use as from_publication_date filter.

        Reads last_fetched from watch state.  Falls back to 7 days ago.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        last_fetched = state.get("openalex_last_fetched")
        if last_fetched:
            # Validate format
            try:
                datetime.fromisoformat(last_fetched)
                return last_fetched
            except ValueError:
                pass
        return (datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%d"
        )

    def _build_url(self, from_date: str, page: int) -> str:
        """Build the full OpenAlex /works URL for a given page.

        Gates on the work's primary field (Economics), which drops the
        crypto/coffee/CompSci papers that merely carried a low-score Economics
        concept tag. An optional author-country scope is added only when
        ECONSIGNALS_OPENALEX_COUNTRIES is set (default: collect broadly).

        Args:
            from_date: Lower bound date string 'YYYY-MM-DD'.
            page: 1-based page number.

        Returns:
            Fully-qualified URL string.
        """
        # Primary-field gate; always include a real polite-pool address.
        filters = [
            f"primary_topic.field.id:{_PRIMARY_FIELD_ID}",
            f"from_publication_date:{from_date}",
            "type:article|preprint",
        ]
        countries = os.environ.get(_COUNTRIES_ENV, "").strip()
        if countries:
            filters.insert(1, f"authorships.countries:{countries}")
        filter_str = ",".join(filters)

        params: dict[str, str | int] = {
            "filter": filter_str,
            "sort": "publication_date:desc",
            "per_page": _PER_PAGE,
            "page": page,
            "mailto": _resolve_email(),
        }

        return f"{_OPENALEX_BASE}/works?{urlencode(params)}"

    def collect(self) -> list[dict]:
        """Fetch recent economics papers from OpenAlex.

        Steps:
        1. Determine from_date via watch state.
        2. Paginate /works up to _MAX_PAGES pages.
        3. Parse each work into the standard paper dict.
        4. Upsert author records into the DB.
        5. Update watch state with current timestamp.

        Returns:
            List of paper dicts suitable for BaseSensor.run() ingestion.
        """
        from econsignals.lib.normalize import canonical_paper_id

        from_date = self._from_date()

        # Override the _base placeholder UA with a real polite-pool address.
        email = _resolve_email()
        headers = {"User-Agent": f"EconSignals/1.0 (mailto:{email})"}

        scope = os.environ.get(_COUNTRIES_ENV, "").strip() or "global"
        print(
            f"[openalex] fetching works from {from_date} "
            f"(field={_PRIMARY_FIELD_ID}, countries={scope})",
            file=sys.stderr,
        )

        papers: list[dict] = []
        seen_source_ids: set[str] = set()
        skipped_noise = 0

        for page in range(1, _MAX_PAGES + 1):
            url = self._build_url(from_date, page)
            try:
                response = self.fetch_json(url, headers=headers)
            except Exception as exc:
                print(f"[openalex] page {page} fetch failed: {exc}", file=sys.stderr)
                break

            results = response.get("results") or []
            if not results:
                break  # No more pages

            for work in results:
                parsed = _parse_work(work)
                if not parsed:
                    continue
                if _is_repository_noise(parsed):
                    skipped_noise += 1
                    continue

                source_id = parsed.get("source_id", "")
                if not source_id or source_id in seen_source_ids:
                    continue
                seen_source_ids.add(source_id)

                # Stash author details for post-ingest DB linking then remove
                # the private key before returning the standard dict.
                author_details: list[dict] = parsed.pop("_author_details", [])

                # Generate canonical_id so dedup can work cross-source
                parsed["canonical_id"] = canonical_paper_id(
                    parsed["title"],
                    parsed.get("authors") or [],
                )
                parsed["title_normalized"] = parsed["canonical_id"]  # reuse hash key

                # Attach author details so run() can link them after ingest.
                # We smuggle them through a private key; run() will strip it.
                # (BaseSensor.run() only pops source_id, source_url, raw_metadata.)
                parsed["_author_details"] = author_details

                papers.append(parsed)

            meta = response.get("meta") or {}
            total_count = meta.get("count", 0)
            fetched_so_far = page * _PER_PAGE
            print(
                f"[openalex] page {page}: got {len(results)} results "
                f"(total available: {total_count})",
                file=sys.stderr,
            )
            if fetched_so_far >= total_count:
                break

        # Update watch state with current fetch timestamp
        state = _load_state()
        state["openalex_last_fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[openalex] failed to save state: {exc}", file=sys.stderr)

        print(
            f"[openalex] collected {len(papers)} papers "
            f"(filtered repository noise: {skipped_noise})",
            file=sys.stderr,
        )
        return papers

    def run(self) -> dict:
        """Execute the sensor with author upsert logic after ingestion.

        Overrides BaseSensor.run() to also upsert author records and create
        paper-author links after each paper is ingested.  All other behaviour
        (DB logging, dedup, stats) is delegated back to the base class via
        a two-pass approach: collect, then re-enrich authors.

        Returns:
            Stats dict (same shape as BaseSensor.run()).
        """
        # We delegate most work to the base run(), but we need access to the
        # per-paper author details after ingest.  We achieve this by calling
        # collect() here, attaching author details to a side-channel dict keyed
        # by source_id, then letting BaseSensor.run() do its ingest loop, and
        # finally running the author upsert in a post-pass.
        #
        # To avoid duplicating the ingest/log logic we simply call the parent
        # and handle authors separately by overriding the ingest loop here.
        from econsignals.lib.db import (
            log_sensor_start,
            log_sensor_end,
            upsert_author,
            link_paper_author,
        )
        from econsignals.lib.dedup import ingest_paper
        from econsignals.lib.normalize import normalize_author_name

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                author_details: list[dict] = item.pop("_author_details", [])

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

                    # Upsert authors and link to this paper
                    for position, ad in enumerate(author_details):
                        name = ad.get("name") or ""
                        if not name:
                            continue
                        try:
                            author_id = upsert_author(
                                name=name,
                                name_normalized=normalize_author_name(name),
                                openalex_id=ad.get("openalex_id") or None,
                                orcid=ad.get("orcid"),
                                affiliation=ad.get("affiliation"),
                                country=ad.get("country"),
                                auto_discovered=1,
                            )
                            if author_id and paper_id:
                                link_paper_author(paper_id, author_id, position)
                        except Exception as author_exc:
                            print(
                                f"  [openalex] author upsert failed ({name!r}): {author_exc}",
                                file=sys.stderr,
                            )

                except Exception as exc:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(f"  [openalex] ingest error: {exc}", file=sys.stderr)

            log_sensor_end(run_id, "success", self.stats["found"], self.stats["new"])

        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"Sensor {self.name} failed: {exc}", file=sys.stderr)

        import json as _json

        result = {
            "sensor": self.name,
            "watch": self.watch,
            "status": "error" if "error_message" in self.stats else "success",
            **self.stats,
        }
        print(_json.dumps(result))
        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = OpenAlexSensor()
    sensor.run()
