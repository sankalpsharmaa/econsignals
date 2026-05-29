"""RePEc NEP (New Economics Papers) sensor.

NEP is a set of human-curated weekly working-paper reports, one per economics
field, each edited by a subject-area economist from the RePEc corpus. Because a
human editor has already decided every listed paper belongs to the field, NEP is
a far higher-signal source than a broad OpenAlex "Economics field" query, which
pulls in regional, predatory, and tangential work. NEP is the curated-corpus
layer the relevance engine cannot synthesize on its own.

This sensor reads the per-report RSS 1.0 (RDF) feeds at
    https://nep.repec.org/rss/nep-<code>.rss.xml
for a configurable set of field codes (default: the development, urban, labour,
public, agricultural, and economic-geography reports that match the research
profile). Override with the ECONSIGNALS_NEP_CODES env var (comma-separated).

Feed structure (RSS 1.0 / RDF), one <rss:item> per paper:
    rdf:about        canonical d.repec.org redirect carrying the RePEc handle
    rss:title        paper title
    rss:link         same redirect URL
    rss:description  abstract
    dc:creator       author name (one element per author)
    dc:date          YYYY-MM (occasionally YYYY or YYYY-MM-DD)
    dc:subject       semicolon/comma-separated keywords (optional)

NEP has no India-specific report; India relevance is applied downstream by the
relevance scorer, consistent with the "India is a ranking nudge, not a
collection filter" design.

Usage:
    python -m econsignals.sensors.nep
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from urllib.parse import unquote

from econsignals.sensors._base import BaseSensor

# Parse feed XML with defusedxml when available (hardened against XXE and
# billion-laughs), falling back to the stdlib parser. NEP feeds carry no DTD,
# so defusedxml's entity/DTD rejection does not affect normal parsing.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:  # pragma: no cover - defusedxml is the hardened default
    from xml.etree.ElementTree import fromstring as _xml_fromstring

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FEED_URL_TMPL = "https://nep.repec.org/rss/{code}.rss.xml"

# Default report set, chosen to match the applied-micro / development / urban /
# India research profile (ag is included for the misallocation work). Each code
# is a human-curated weekly report; channel titles in parentheses.
_DEFAULT_CODES: list[str] = [
    "nep-dev",  # Development
    "nep-uep",  # Urban Economics and Policy
    "nep-lab",  # Labour Economics
    "nep-pbe",  # Public Economics
    "nep-agr",  # Agricultural Economics
    "nep-geo",  # Economic Geography
]

# RSS 1.0 / RDF namespaces used by the NEP feeds.
_NS: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rss": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"

# A RePEc handle inside the redirect URL, e.g. "...u=RePEc:ehl:lserod:137937&r=..".
_RE_REPEC_HANDLE = re.compile(r"RePEc:[^&\s]+", re.IGNORECASE)

# JEL codes, when an editor or author included them in the abstract text.
_RE_JEL_LABEL = re.compile(
    r"\bJEL[:\s]+([A-Z][0-9]{1,2}(?:[,;\s]+[A-Z][0-9]{1,2})*)", re.IGNORECASE
)
_RE_JEL_CODE = re.compile(r"[A-Z][0-9]{1,2}")


# ---------------------------------------------------------------------------
# Helpers (pure functions, unit-testable without network or DB)
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> str | None:
    """Convert a NEP dc:date string to ISO YYYY-MM-DD, or return None.

    NEP dates are usually year-month ("2026-04"); occasionally a full ISO date
    or a bare year. A year-month maps to the first of that month.

    Args:
        raw: Raw dc:date text.

    Returns:
        'YYYY-MM-DD' string, or None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)[:10]
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = re.match(r"^(\d{4})$", raw)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _extract_handle(url: str) -> str | None:
    """Return the RePEc handle embedded in a NEP redirect URL, or None.

    Args:
        url: A d.repec.org redirect URL carrying a ``u=RePEc:..`` parameter.

    Returns:
        The handle string (e.g. "RePEc:ehl:lserod:137937"), or None.
    """
    if not url:
        return None
    m = _RE_REPEC_HANDLE.search(unquote(url))
    return m.group(0) if m else None


def _extract_jel_codes(text: str) -> list[str]:
    """Extract JEL classification codes from plain text.

    Args:
        text: Plain text to search (typically the abstract).

    Returns:
        Deduplicated list of JEL code strings.
    """
    codes: list[str] = []
    seen: set[str] = set()
    for m in _RE_JEL_LABEL.finditer(text or ""):
        for code in _RE_JEL_CODE.findall(m.group(1)):
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _parse_feed(xml_text: str, code: str) -> list[dict]:
    """Parse one NEP RSS 1.0 feed into standard paper dicts.

    Args:
        xml_text: Decoded RSS/RDF feed body.
        code: NEP report code (e.g. "nep-dev"), recorded in raw_metadata.

    Returns:
        List of paper dicts conforming to the BaseSensor.collect() contract.
        Returns an empty list on a parse error.
    """
    try:
        root = _xml_fromstring(xml_text)
    except Exception as exc:
        # A malformed or hostile feed must skip this report safely, never abort
        # the whole sensor run.
        print(f"[nep] {code}: XML parse rejected: {exc}", file=sys.stderr)
        return []

    papers: list[dict] = []
    for item in root.findall("rss:item", _NS):
        title = (item.findtext("rss:title", default="", namespaces=_NS) or "").strip()
        if not title:
            continue

        link = (item.findtext("rss:link", default="", namespaces=_NS) or "").strip()
        if not link:
            link = (item.get(_RDF_ABOUT) or "").strip()

        abstract_raw = (
            item.findtext("rss:description", default="", namespaces=_NS) or ""
        ).strip()
        abstract: str | None = abstract_raw or None

        authors = [
            (e.text or "").strip()
            for e in item.findall("dc:creator", _NS)
            if (e.text or "").strip()
        ]

        published_at = _parse_date(
            item.findtext("dc:date", default="", namespaces=_NS)
        )

        subjects_raw = item.findtext("dc:subject", default="", namespaces=_NS) or ""
        keywords = [s.strip() for s in re.split(r"[;,]", subjects_raw) if s.strip()]

        handle = _extract_handle(link)
        if handle:
            source_id = handle.lower()
        else:
            # No RePEc handle in the URL: fall back to a title hash so the item
            # still dedups deterministically across runs.
            source_id = f"{code}:{hashlib.sha1(title.encode()).hexdigest()[:10]}"

        jel_codes = _extract_jel_codes(abstract or "")

        papers.append(
            {
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "doi": None,
                "url": link or None,
                "published_at": published_at,
                "paper_type": "working_paper",
                "jel_codes": jel_codes,
                "keywords": keywords,
                "source_id": source_id,
                "source_url": link or None,
                "raw_metadata": {
                    "source": "repec_nep",
                    "nep_code": code,
                    "repec_handle": handle,
                    "dc_subject": subjects_raw or None,
                },
            }
        )

    return papers


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class NepSensor(BaseSensor):
    """Collect human-curated working papers from RePEc NEP weekly reports.

    Fetches each configured NEP report's RSS feed, parses its items, and emits
    standard paper dicts. Papers appearing in more than one report within a run
    are de-duplicated by RePEc handle; cross-run de-duplication is handled by the
    ingest layer (source='repec_nep', source_id=handle).

    Attributes:
        name: Sensor identifier ('repec_nep'), matching the prestige table.
        watch: Watch category ('papers').
        rate_limit: 0.5 req/s -- one request every 2 seconds (polite to RePEc).
    """

    name = "repec_nep"
    watch = "papers"
    rate_limit = 0.5

    def _codes(self) -> list[str]:
        """Return the NEP report codes to fetch.

        Reads ECONSIGNALS_NEP_CODES (comma-separated) if set, else the default
        profile-matched set. Each code is normalised to a ``nep-`` prefix.

        Returns:
            List of NEP report codes.
        """
        raw = os.environ.get("ECONSIGNALS_NEP_CODES", "").strip()
        codes = [c.strip() for c in raw.split(",") if c.strip()] if raw else list(
            _DEFAULT_CODES
        )
        normalised: list[str] = []
        for c in codes:
            c = c.lower()
            if not c.startswith("nep-"):
                c = f"nep-{c}"
            if c not in normalised:
                normalised.append(c)
        return normalised

    def collect(self) -> list[dict]:
        """Fetch and parse every configured NEP report.

        Steps:
        1. Resolve the report codes from env or the default set.
        2. Fetch each report's RSS feed (a fetch failure on one code is logged
           and skipped; the others still run).
        3. Parse items into paper dicts.
        4. De-duplicate within the run by source_id (a paper can appear in
           several reports).

        Returns:
            List of unique paper dicts across all configured reports.
        """
        codes = self._codes()
        print(f"[nep] fetching {len(codes)} reports: {', '.join(codes)}", file=sys.stderr)

        all_papers: list[dict] = []
        seen: set[str] = set()

        for code in codes:
            url = _FEED_URL_TMPL.format(code=code)
            try:
                raw = self.fetch_url(url, timeout=30)
            except Exception as exc:
                print(f"[nep] {code}: fetch failed: {exc}", file=sys.stderr)
                continue

            items = _parse_feed(raw.decode("utf-8", errors="replace"), code)
            print(f"[nep] {code}: parsed {len(items)} items", file=sys.stderr)

            for paper in items:
                source_id = paper["source_id"]
                if source_id in seen:
                    continue
                seen.add(source_id)
                all_papers.append(paper)

        print(
            f"[nep] collected {len(all_papers)} unique papers across "
            f"{len(codes)} reports",
            file=sys.stderr,
        )
        return all_papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = NepSensor()
    sensor.run()
