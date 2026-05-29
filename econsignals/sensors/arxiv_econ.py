"""arXiv economics sensor.

arXiv's economics archive is split into three categories: econ.GN (General
Economics), econ.EM (Econometrics), and econ.TH (Theoretical Economics). Unlike
a curated source, arXiv is author-submitted and unrefereed, so it is the
earliest-available signal layer: applied-micro, development, and econometrics
work often appears here weeks before it reaches a working-paper series or a
journal. The relevance engine downstream is responsible for filtering and
ranking; this sensor's job is breadth of fresh econ pre-prints.

This sensor queries the arXiv Atom API at
    http://export.arxiv.org/api/query
with a category search over econ.GN, econ.EM, and econ.TH (override the set with
the ECONSIGNALS_ARXIV_CATS env var, comma-separated), sorted by submission date
descending so the newest pre-prints come first.

Feed structure (Atom 1.0 with arxiv + opensearch extensions), one <atom:entry>
per paper:
    atom:id                 abstract URL, e.g. http://arxiv.org/abs/2605.27684v1
    atom:title              paper title
    atom:summary            abstract
    atom:published          submission timestamp (RFC 3339, e.g. 2026-05-28T..)
    atom:updated            last-revision timestamp
    atom:author/atom:name   one element per author
    atom:link[@rel=...]     alternate (HTML abs page) and related (pdf) links
    atom:category[@term]    arXiv category code (econ.GN, q-fin.EC, ...)
    arxiv:doi               publisher DOI once the paper is published (optional)
    arxiv:comment           free-text author note, e.g. "23 pages, 8 figures"
    arxiv:primary_category  the paper's primary arXiv category
Feed header carries opensearch:totalResults / startIndex / itemsPerPage.

arXiv has no India-specific feed; India relevance is applied downstream by the
relevance scorer, consistent with the "India is a ranking nudge, not a
collection filter" design.

arXiv asks API clients to make no more than one request every three seconds and
to identify themselves; the sensor's rate_limit and the base User-Agent honour
this.

Usage:
    python -m econsignals.sensors.arxiv_econ
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import quote

from econsignals.sensors._base import BaseSensor

# Parse the Atom feed with defusedxml when available (hardened against XXE and
# billion-laughs), falling back to the stdlib parser. The arXiv API response
# carries no DTD, so defusedxml's entity/DTD rejection does not affect normal
# parsing.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:  # pragma: no cover - defusedxml is the hardened default
    from xml.etree.ElementTree import fromstring as _xml_fromstring

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_URL = "http://export.arxiv.org/api/query"

# Default economics categories, the whole econ archive (econ.GN/EM/TH). q-fin
# papers are reached only when cross-listed into an econ category, which the
# query below already captures via the category match.
_DEFAULT_CATS: list[str] = [
    "econ.GN",  # General Economics (applied micro, development, urban, ...)
    "econ.EM",  # Econometrics
    "econ.TH",  # Theoretical Economics
]

# Atom 1.0 plus the arXiv and OpenSearch extension namespaces, exactly as the
# arXiv API declares them.
_NS: dict[str, str] = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

# An arXiv id with optional version suffix, e.g. "2605.27684v1" or the old-style
# "hep-ex/0307015v2". Used to strip the leading abstract-URL prefix.
_RE_ARXIV_ID = re.compile(r"(?:abs/)?([\w.\-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?$")


# ---------------------------------------------------------------------------
# Helpers (pure functions, unit-testable without network or DB)
# ---------------------------------------------------------------------------


def _clean(text: str | None) -> str:
    """Collapse internal whitespace and strip a text node.

    arXiv wraps titles and abstracts across lines with embedded newlines and
    runs of spaces; normalise them to single spaces.

    Args:
        text: Raw text node content, or None.

    Returns:
        Whitespace-normalised string ("" when the input is falsy).
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str | None) -> str | None:
    """Convert an arXiv RFC 3339 timestamp to an ISO YYYY-MM-DD date, or None.

    arXiv <published>/<updated> values look like "2026-05-28T13:46:39-04:00";
    only the leading date is kept, matching the schema's date-granularity
    published_at field.

    Args:
        raw: Raw timestamp text.

    Returns:
        'YYYY-MM-DD' string, or None if no leading date is present.
    """
    if not raw:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw.strip())
    return m.group(0) if m else None


def _extract_arxiv_id(id_url: str) -> str | None:
    """Return the bare arXiv id from an <atom:id> abstract URL, or None.

    Args:
        id_url: The entry id, e.g. "http://arxiv.org/abs/2605.27684v1".

    Returns:
        The id including any version, e.g. "2605.27684v1", or None.
    """
    if not id_url:
        return None
    m = _RE_ARXIV_ID.search(id_url.strip())
    if not m:
        return None
    return m.group(1) + (m.group(2) or "")


def _strip_version(arxiv_id: str | None) -> str | None:
    """Drop the trailing version suffix from an arXiv id, or return it unchanged.

    arXiv ids carry a revision suffix (e.g. "2410.03524v1"). Removing it gives a
    stable per-paper key so a later revision dedups to the same paper.

    Args:
        arxiv_id: An arXiv id, possibly versioned.

    Returns:
        The id without any "vN" suffix, or None when the input is falsy.
    """
    if not arxiv_id:
        return None
    return re.sub(r"v\d+$", "", arxiv_id)


def _pick_links(entry, ns: dict[str, str]) -> tuple[str | None, str | None]:
    """Return (html_abstract_url, pdf_url) from an entry's <atom:link> children.

    arXiv emits a rel="alternate" type="text/html" link to the abstract page and
    a rel="related" title="pdf" link to the PDF. Either may be absent.

    Args:
        entry: The <atom:entry> element.
        ns: Namespace map.

    Returns:
        Tuple of (abstract page URL or None, PDF URL or None).
    """
    html_url: str | None = None
    pdf_url: str | None = None
    for link in entry.findall("atom:link", ns):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        rel = link.get("rel") or ""
        link_type = link.get("type") or ""
        title = link.get("title") or ""
        if link_type == "application/pdf" or title == "pdf":
            pdf_url = href
        elif rel == "alternate" or link_type == "text/html":
            html_url = href
    return html_url, pdf_url


def _parse_feed(xml_text: str) -> list[dict]:
    """Parse an arXiv Atom API response into standard paper dicts.

    Args:
        xml_text: Decoded Atom feed body.

    Returns:
        List of paper dicts conforming to the BaseSensor.collect() contract.
        Returns an empty list on a parse error.
    """
    try:
        root = _xml_fromstring(xml_text)
    except Exception as exc:
        # A malformed or hostile feed must fail safely rather than abort the run.
        print(f"[arxiv] XML parse rejected: {exc}", file=sys.stderr)
        return []

    papers: list[dict] = []
    for entry in root.findall("atom:entry", _NS):
        title = _clean(entry.findtext("atom:title", default="", namespaces=_NS))
        if not title:
            continue

        id_url = (entry.findtext("atom:id", default="", namespaces=_NS) or "").strip()
        # The <id> carries the version suffix (e.g. ".../2410.03524v1"); strip it
        # so a v1->v2 revision dedups to the same source_id rather than
        # re-ingesting as a new paper. The full versioned id is kept in
        # raw_metadata. Fall back to the id URL if the id is unparseable.
        arxiv_id = _extract_arxiv_id(id_url)
        base_id = _strip_version(arxiv_id) if arxiv_id else None
        source_id = base_id or (id_url or None)
        if not source_id:
            continue

        abstract = _clean(entry.findtext("atom:summary", default="", namespaces=_NS))

        authors = [
            _clean(name.text)
            for name in entry.findall("atom:author/atom:name", _NS)
            if _clean(name.text)
        ]

        published_at = _parse_date(
            entry.findtext("atom:published", default="", namespaces=_NS)
        )

        # arxiv:doi is present only once a publisher DOI is assigned.
        doi = (entry.findtext("arxiv:doi", default="", namespaces=_NS) or "").strip()
        doi = doi or None

        categories = [
            (cat.get("term") or "").strip()
            for cat in entry.findall("atom:category", _NS)
            if (cat.get("term") or "").strip()
        ]

        html_url, pdf_url = _pick_links(entry, _NS)
        # Prefer the abstract HTML page; otherwise derive it from the versioned id.
        url = html_url or (
            f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else (id_url or None)
        )

        comment = _clean(entry.findtext("arxiv:comment", default="", namespaces=_NS))
        primary_cat_el = entry.find("arxiv:primary_category", _NS)
        primary_category = (
            (primary_cat_el.get("term") or "").strip() if primary_cat_el is not None else None
        )

        papers.append(
            {
                "title": title,
                "authors": authors,
                "abstract": abstract or None,
                "doi": doi,
                "url": url,
                "published_at": published_at,
                "paper_type": "working_paper",
                "jel_codes": [],
                "keywords": categories,
                "source_id": source_id,
                "source_url": url,
                "raw_metadata": {
                    "source": "arxiv",
                    "arxiv_id": arxiv_id,
                    "base_id": base_id,
                    "primary_category": primary_category,
                    "categories": categories,
                    "pdf_url": pdf_url,
                    "comment": comment or None,
                },
            }
        )

    return papers


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class ArxivEconSensor(BaseSensor):
    """Collect fresh economics pre-prints from the arXiv Atom API.

    Issues one category query over the configured econ categories, sorted by
    submission date descending, parses the Atom response, and emits standard
    paper dicts. Cross-run de-duplication is handled by the ingest layer
    (source='arxiv', source_id=arXiv id).

    Attributes:
        name: Sensor identifier ('arxiv'); the prestige table has no 'arxiv'
            key yet (see register_steps).
        watch: Watch category ('papers').
        rate_limit: 0.25 req/s -- one request every 4 seconds, honouring arXiv's
            request-no-more-than-once-per-three-seconds guidance.
    """

    name = "arxiv"
    watch = "papers"
    rate_limit = 0.25

    def _categories(self) -> list[str]:
        """Return the arXiv category codes to query.

        Reads ECONSIGNALS_ARXIV_CATS (comma-separated) if set, else the default
        econ archive set.

        Returns:
            List of arXiv category codes.
        """
        raw = os.environ.get("ECONSIGNALS_ARXIV_CATS", "").strip()
        cats = [c.strip() for c in raw.split(",") if c.strip()] if raw else list(
            _DEFAULT_CATS
        )
        deduped: list[str] = []
        for c in cats:
            if c not in deduped:
                deduped.append(c)
        return deduped

    def _max_results(self) -> int:
        """Return the per-query result cap.

        Reads ECONSIGNALS_ARXIV_MAX (default 100). arXiv recommends paging in
        slices of at most a few thousand; one slice of recent submissions is
        ample for a daily feed.

        Returns:
            Positive integer result cap.
        """
        raw = os.environ.get("ECONSIGNALS_ARXIV_MAX", "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        return 100

    def _build_url(self) -> str:
        """Build the arXiv Atom API query URL for the configured categories.

        The search_query ORs the categories (``cat:econ.GN OR cat:econ.EM``) and
        sorts by submission date descending so the newest pre-prints lead.

        Returns:
            Fully-formed query URL.
        """
        cats = self._categories()
        clause = " OR ".join(f"cat:{c}" for c in cats)
        # quote() leaves '+' alone, so encode the clause with spaces -> %20 and
        # build the rest of the query string explicitly.
        search_query = quote(clause, safe="")
        return (
            f"{_API_URL}?search_query={search_query}"
            f"&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={self._max_results()}"
        )

    def collect(self) -> list[dict]:
        """Fetch and parse the arXiv econ Atom feed.

        Steps:
        1. Build the category query URL.
        2. Fetch the Atom response (a fetch failure raises; the base run()
           records the sensor run as an error).
        3. Parse entries into paper dicts and de-duplicate within the run by
           source_id (a paper cross-listed in two econ categories appears once).

        Returns:
            List of unique paper dicts.
        """
        url = self._build_url()
        print(f"[arxiv] fetching: {url}", file=sys.stderr)

        raw = self.fetch_url(url, timeout=30)
        items = _parse_feed(raw.decode("utf-8", errors="replace"))
        print(f"[arxiv] parsed {len(items)} entries", file=sys.stderr)

        unique: list[dict] = []
        seen: set[str] = set()
        for paper in items:
            source_id = paper["source_id"]
            if source_id in seen:
                continue
            seen.add(source_id)
            unique.append(paper)

        print(f"[arxiv] collected {len(unique)} unique papers", file=sys.stderr)
        return unique


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = ArxivEconSensor()
    sensor.run()
