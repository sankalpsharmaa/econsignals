"""Funding deadline sensor for EconSignals.

Scrapes funding opportunity pages for economics research grants and inserts
deadline records into the deadlines table.

Pages are often JavaScript-heavy. The sensor fetches raw HTML and applies
regex heuristics to extract deadline dates and program names. Missing data
is handled defensively: records are still inserted with deadline_date=None
(rolling deadlines) when no date is found.

Usage:
    python sensors/funding.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

from econsignals.sensors._base import BaseSensor

# ---------------------------------------------------------------------------
# Funding sources
# ---------------------------------------------------------------------------

FUNDING_SOURCES: dict[str, dict[str, str]] = {
    "pedl": {
        "name": "PEDL - Private Enterprise Development in Low-Income Countries",
        "url": "https://grp.cepr.org/pedl/funding/open-and-upcoming-calls",
        "org": "CEPR/PEDL",
    },
    "steg": {
        "name": "STEG - Structural Transformation and Economic Growth",
        "url": "https://grp.cepr.org/steg/funding/open-and-upcoming-calls",
        "org": "CEPR/STEG",
    },
    "igc_funding": {
        "name": "IGC - International Growth Centre Funding",
        "url": "https://www.theigc.org/funding",
        "org": "IGC",
    },
    "nsf_ses": {
        "name": "NSF Social, Behavioral and Economic Sciences",
        "url": "https://new.nsf.gov/funding/opportunities?f%5B0%5D=division_name%3ASocial%20and%20Economic%20Sciences",
        "org": "NSF",
    },
    "russell_sage": {
        "name": "Russell Sage Foundation",
        "url": "https://www.russellsage.org/how-to-apply",
        "org": "Russell Sage Foundation",
    },
    "sloan": {
        "name": "Alfred P. Sloan Foundation",
        "url": "https://sloan.org/programs/research",
        "org": "Sloan Foundation",
    },
    "jpal_ri": {
        "name": "J-PAL Research Initiatives",
        "url": "https://www.povertyactionlab.org/initiative/governance-initiative",
        "org": "J-PAL",
    },
    "weiss_fund": {
        "name": "Weiss Fund for Research in Development Economics",
        "url": "https://weissfund.uchicago.edu/",
        "org": "Weiss Fund",
    },
    "usc_econ": {
        "name": "USC Dornsife Economics Research & Grants",
        "url": "https://dornsife.usc.edu/economics/",
        "org": "USC",
    },
}

# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

# Dev econ / poverty / India-adjacent funders score higher
_HIGH_RELEVANCE_ORGS: frozenset[str] = frozenset({
    "J-PAL", "Weiss Fund", "CEPR/PEDL", "CEPR/STEG", "IGC",
})
_DEFAULT_RELEVANCE = 0.5
_HIGH_RELEVANCE = 0.8

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

# Month name variants: "January" / "Jan" / "01"
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
)
_MONTH_ABBR = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
)

# Patterns ordered by specificity (most specific first)
_DATE_PATTERNS: list[re.Pattern[str]] = [
    # "January 15, 2025" or "January 15 2025"
    re.compile(
        rf"({_MONTH_NAMES})\s+(\d{{1,2}})[,\s]+(\d{{4}})",
        re.IGNORECASE,
    ),
    # "Jan 15, 2025" or "Jan. 15, 2025"
    re.compile(
        rf"({_MONTH_ABBR})\.?\s+(\d{{1,2}})[,\s]+(\d{{4}})",
        re.IGNORECASE,
    ),
    # "15 January 2025"
    re.compile(
        rf"(\d{{1,2}})\s+({_MONTH_NAMES})\s+(\d{{4}})",
        re.IGNORECASE,
    ),
    # "15 Jan 2025"
    re.compile(
        rf"(\d{{1,2}})\s+({_MONTH_ABBR})\.?\s+(\d{{4}})",
        re.IGNORECASE,
    ),
    # ISO: "2025-01-15"
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    # US format: "01/15/2025"
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
]

# Context words that signal a nearby date is a deadline
_DEADLINE_CONTEXT: re.Pattern[str] = re.compile(
    r"deadline|due\s+date|submit\s+by|submission\s+deadline|apply\s+by|"
    r"applications?\s+due|proposals?\s+due|closes?|closing\s+date|"
    r"letter\s+of\s+intent|loi\s+due|full\s+proposal",
    re.IGNORECASE,
)

_MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


class _ParsedDate(NamedTuple):
    iso: str       # "YYYY-MM-DD"
    raw: str       # matched text for debugging


def _try_parse_date(text: str) -> _ParsedDate | None:
    """Attempt to parse a date string into ISO format.

    Tries multiple pattern formats in order of specificity. Returns the
    first valid parse, or None if nothing matched.

    Args:
        text: Raw string fragment to parse.

    Returns:
        _ParsedDate with iso and raw fields, or None.
    """
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(0)
        groups = m.groups()

        try:
            if len(groups) == 3:
                g0, g1, g2 = groups
                # ISO: YYYY-MM-DD
                if re.match(r"^\d{4}$", g0):
                    year, month_str, day = int(g0), g1, int(g2)
                    month = int(month_str) if month_str.isdigit() else _MONTH_MAP.get(month_str.lower(), 0)
                # Day-first: DD Month YYYY
                elif re.match(r"^\d{1,2}$", g0) and not g0.isdigit() or (g0.isdigit() and int(g0) <= 31):
                    # Check if g1 is a month name
                    month = _MONTH_MAP.get(g1.lower().rstrip("."), 0)
                    if month:
                        day, year = int(g0), int(g2)
                    else:
                        # US format MM/DD/YYYY
                        month, day, year = int(g0), int(g1), int(g2)
                else:
                    # Month name first: "January 15 2025"
                    month = _MONTH_MAP.get(g0.lower().rstrip("."), 0)
                    if not month:
                        continue
                    day, year = int(g1), int(g2)

                if not (1 <= month <= 12 and 1 <= day <= 31 and 2020 <= year <= 2035):
                    continue

                dt = datetime(year, month, day)
                return _ParsedDate(iso=dt.strftime("%Y-%m-%d"), raw=raw)
        except (ValueError, AttributeError):
            continue

    return None


def _extract_deadline_dates(html: str) -> list[_ParsedDate]:
    """Find dates that appear near deadline-signalling context words.

    Splits the HTML into a sliding window of text chunks anchored on
    deadline keywords. Extracts and deduplicates all candidate dates.

    Args:
        html: Raw HTML content as a string.

    Returns:
        List of unique _ParsedDate objects, sorted chronologically.
    """
    # Strip most tags for cleaner text matching, preserving whitespace structure
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)

    found: dict[str, _ParsedDate] = {}  # iso -> _ParsedDate for dedup

    # Find all positions of deadline context keywords
    for context_match in _DEADLINE_CONTEXT.finditer(text):
        # Examine ±200 characters around the keyword
        start = max(0, context_match.start() - 50)
        end = min(len(text), context_match.end() + 200)
        window = text[start:end]

        parsed = _try_parse_date(window)
        if parsed and parsed.iso not in found:
            found[parsed.iso] = parsed

    return sorted(found.values(), key=lambda d: d.iso)


def _strip_html(html: str, max_chars: int = 300) -> str:
    """Remove HTML tags and return a plain-text snippet.

    Args:
        html: Raw HTML string.
        max_chars: Maximum length of the returned string.

    Returns:
        Cleaned text, truncated to max_chars.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _extract_description(html: str, program_name: str) -> str:
    """Extract a brief description from the page.

    Looks for paragraphs near the program name or near deadline keywords.
    Falls back to the first substantive paragraph on the page.

    Args:
        html: Raw HTML content.
        program_name: Name of the funding program (used to anchor the search).

    Returns:
        A plain-text description up to 300 characters.
    """
    # Try to find a <p> or <div> near a deadline keyword
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    for para in paragraphs:
        stripped = _strip_html(para, max_chars=300)
        if len(stripped) > 60 and (
            _DEADLINE_CONTEXT.search(stripped)
            or any(kw in stripped.lower() for kw in ("grant", "funding", "research", "award", "apply"))
        ):
            return stripped

    # Fall back to first non-trivial paragraph
    for para in paragraphs:
        stripped = _strip_html(para, max_chars=300)
        if len(stripped) > 60:
            return stripped

    return f"{program_name} funding opportunity."


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class FundingSensor(BaseSensor):
    """Scrape economics research funding pages and insert deadline records.

    Fetches each page in FUNDING_SOURCES, applies regex heuristics to
    locate deadline dates and descriptions, and upserts a deadline record
    for each date found. Pages with no extractable date produce a single
    record with deadline_date=None (rolling deadline).

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category ("deadlines").
        rate_limit: 0.3 requests per second; funding pages are slow CDNs.
    """

    name = "funding"
    watch = "deadlines"
    rate_limit = 0.3

    def _relevance_score(self, org: str) -> float:
        """Return relevance score based on organization.

        Dev econ / poverty-focused funders score 0.8; others score 0.5.

        Args:
            org: Organization name string.

        Returns:
            Float relevance score in [0, 1].
        """
        return _HIGH_RELEVANCE if org in _HIGH_RELEVANCE_ORGS else _DEFAULT_RELEVANCE

    def _scrape_source(self, key: str, source: dict[str, str]) -> list[dict]:
        """Fetch and parse a single funding source page.

        Args:
            key: Source key (e.g. "nsf_ses"), used only for logging.
            source: Dict with name, url, org keys.

        Returns:
            List of deadline dicts ready for upsert_deadline().
            Returns a single rolling-deadline record on parse failure.
        """
        name = source["name"]
        url = source["url"]
        org = source["org"]
        score = self._relevance_score(org)

        try:
            raw = self.fetch_url(url, timeout=45)
        except Exception as exc:
            print(f"[funding] fetch failed for {key} ({url}): {exc}", file=sys.stderr)
            return []

        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[funding] decode error for {key}: {exc}", file=sys.stderr)
            return []

        dates = _extract_deadline_dates(html)
        description = _extract_description(html, name)

        print(
            f"[funding] {key}: found {len(dates)} deadline date(s)",
            file=sys.stderr,
        )

        if not dates:
            # Rolling or indeterminate deadline: insert once with no date
            return [
                {
                    "type": "funding",
                    "name": name,
                    "organization": org,
                    "deadline_date": None,
                    "event_date": None,
                    "url": url,
                    "description": description,
                    "relevance_score": score,
                }
            ]

        records = []
        for parsed_date in dates:
            # Skip dates clearly in the past (more than 30 days ago)
            try:
                dt = datetime.strptime(parsed_date.iso, "%Y-%m-%d")
                today = datetime.now(timezone.utc).date()
                if dt.date() < today:
                    # Only skip if well in the past; borderline dates kept
                    if (today - dt.date()).days > 30:
                        continue
            except ValueError:
                pass

            records.append(
                {
                    "type": "funding",
                    "name": name,
                    "organization": org,
                    "deadline_date": parsed_date.iso,
                    "event_date": None,
                    "url": url,
                    "description": description,
                    "relevance_score": score,
                }
            )

        # If all dates were filtered out as past, fall back to rolling
        if not records:
            records.append(
                {
                    "type": "funding",
                    "name": name,
                    "organization": org,
                    "deadline_date": None,
                    "event_date": None,
                    "url": url,
                    "description": description,
                    "relevance_score": score,
                }
            )

        return records

    def collect(self) -> list[dict]:
        """Scrape all configured funding sources and return deadline dicts.

        Iterates over FUNDING_SOURCES. Each source may yield zero or more
        deadline records. Errors on individual sources are logged to stderr
        and skipped (partial success is acceptable).

        Returns:
            List of deadline dicts with keys: type, name, organization,
            deadline_date, event_date, url, description, relevance_score.
        """
        all_deadlines: list[dict] = []

        for key, source in FUNDING_SOURCES.items():
            print(f"[funding] scraping {key}: {source['url']}", file=sys.stderr)
            records = self._scrape_source(key, source)
            all_deadlines.extend(records)

        print(
            f"[funding] collected {len(all_deadlines)} deadline record(s) total",
            file=sys.stderr,
        )
        return all_deadlines

    def run(self) -> dict:
        """Execute the sensor: collect deadlines, upsert to DB, log run.

        Overrides BaseSensor.run() because deadlines bypass the paper
        deduplication pipeline and are inserted via upsert_deadline().

        Returns:
            Stats dict with keys: sensor, watch, status, found, new, errors,
            and optionally error_message on failure.
        """
        from econsignals.lib.db import log_sensor_start, log_sensor_end, upsert_deadline

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                try:
                    deadline_id = upsert_deadline(item)
                    if deadline_id > 0:
                        self.stats["new"] = int(self.stats["new"]) + 1
                except Exception as exc:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(f"[funding] upsert error: {exc}", file=sys.stderr)

            log_sensor_end(
                run_id,
                "success",
                int(self.stats["found"]),
                int(self.stats["new"]),
            )
        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"[funding] sensor failed: {exc}", file=sys.stderr)

        result = {
            "sensor": self.name,
            "watch": self.watch,
            "status": "error" if "error_message" in self.stats else "success",
            **self.stats,
        }
        print(json.dumps(result))
        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = FundingSensor()
    sensor.run()
