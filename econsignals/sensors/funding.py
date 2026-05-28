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
from datetime import date, datetime, timezone
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

from econsignals.sensors._base import BaseSensor

# ---------------------------------------------------------------------------
# Funding sources
# ---------------------------------------------------------------------------

# Each source carries an optional ``known_deadlines`` list of curated,
# human-verified recurring calls. The target funder pages are JavaScript-
# rendered SPAs whose deadline tables never appear in the initial HTML, so
# regex-on-HTML scraping captures zero real dates. The curated registry is the
# primary data source; live scraping is demoted to an override that only
# supplements it when a real date is parsed from the page.
#
# A curated entry has:
#   label:         short call name, appended to the source name
#   month:         typical deadline month (1-12)
#   recurrence:    "annual" or "biannual" (biannual fires twice a year)
#   second_month:  the second month for biannual calls (1-12)
#   last_verified: ISO date the recurrence was last checked by a human
FUNDING_SOURCES: dict[str, dict] = {
    "pedl": {
        "name": "PEDL - Private Enterprise Development in Low-Income Countries",
        "url": "https://grp.cepr.org/pedl/funding/open-and-upcoming-calls",
        "org": "CEPR/PEDL",
        "known_deadlines": [
            {
                "label": "Major Research Grants call",
                "month": 4,
                "recurrence": "biannual",
                "second_month": 10,
                "last_verified": "2026-05-28",
            },
        ],
    },
    "steg": {
        "name": "STEG - Structural Transformation and Economic Growth",
        "url": "https://grp.cepr.org/steg/funding/open-and-upcoming-calls",
        "org": "CEPR/STEG",
        "known_deadlines": [
            {
                "label": "Research Grants call",
                "month": 4,
                "recurrence": "biannual",
                "second_month": 10,
                "last_verified": "2026-05-28",
            },
        ],
    },
    "igc_funding": {
        "name": "IGC - International Growth Centre Funding",
        "url": "https://www.theigc.org/funding",
        "org": "IGC",
        "known_deadlines": [
            {
                "label": "Research call",
                "month": 3,
                "recurrence": "biannual",
                "second_month": 9,
                "last_verified": "2026-05-28",
            },
        ],
    },
    "nsf_ses": {
        "name": "NSF Social, Behavioral and Economic Sciences",
        "url": "https://new.nsf.gov/funding/opportunities?f%5B0%5D=division_name%3ASocial%20and%20Economic%20Sciences",
        "org": "NSF",
        "known_deadlines": [
            {
                "label": "Economics Program target date",
                "month": 1,
                "recurrence": "biannual",
                "second_month": 8,
                "last_verified": "2026-05-28",
            },
        ],
    },
    "russell_sage": {
        "name": "Russell Sage Foundation",
        "url": "https://www.russellsage.org/apply",
        "org": "Russell Sage Foundation",
        "known_deadlines": [
            {
                "label": "Letters of inquiry",
                "month": 5,
                "recurrence": "biannual",
                "second_month": 11,
                "last_verified": "2026-05-28",
            },
        ],
    },
    "sloan": {
        "name": "Alfred P. Sloan Foundation",
        "url": "https://sloan.org/programs/research",
        "org": "Sloan Foundation",
        # No fixed public deadline; Sloan accepts LOIs on a rolling basis
        "known_deadlines": [],
    },
    "jpal_ri": {
        "name": "J-PAL Research Initiatives",
        "url": "https://www.povertyactionlab.org/initiative/governance-initiative",
        "org": "J-PAL",
        "known_deadlines": [
            {
                "label": "Initiative funding call",
                "month": 3,
                "recurrence": "biannual",
                "second_month": 9,
                "last_verified": "2026-05-28",
            },
        ],
    },
    "weiss_fund": {
        "name": "Weiss Fund for Research in Development Economics",
        "url": "https://weissfund.uchicago.edu/",
        "org": "Weiss Fund",
        "known_deadlines": [
            {
                "label": "Annual research grant call",
                "month": 12,
                "recurrence": "annual",
                "last_verified": "2026-05-28",
            },
        ],
    },
    "usc_econ": {
        "name": "USC Dornsife Economics Research & Grants",
        "url": "https://dornsife.usc.edu/economics/",
        "org": "USC",
        # No public recurring research-grant deadline; track as rolling
        "known_deadlines": [],
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


def _next_occurrence(month: int, today: date) -> str:
    """Return the next 15th-of-month ISO date on or after today for a month.

    Uses the 15th as a generic mid-month placeholder. If the 15th of the given
    month has already passed this year, roll to next year.

    Args:
        month: Target month 1-12.
        today: Reference date (the current date).

    Returns:
        ISO date string "YYYY-MM-15" for the next occurrence.
    """
    candidate = date(today.year, month, 15)
    if candidate < today:
        candidate = date(today.year + 1, month, 15)
    return candidate.isoformat()


def curated_deadline_dates(entry: dict, today: date) -> list[str]:
    """Project a curated recurrence entry into upcoming ISO deadline dates.

    Args:
        entry: A ``known_deadlines`` dict with month, recurrence, and
            optionally second_month.
        today: Reference date (the current date).

    Returns:
        Sorted list of unique upcoming ISO date strings (one for an annual
        call, up to two for a biannual call).
    """
    months = [entry["month"]]
    if entry.get("recurrence") == "biannual" and entry.get("second_month"):
        months.append(entry["second_month"])
    dates = {_next_occurrence(m, today) for m in months}
    return sorted(dates)


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

    def _curated_records(
        self, key: str, source: dict, description: str, rolling_fallback: bool = True
    ) -> list[dict]:
        """Build deadline records from a source's curated known_deadlines.

        Projects each curated recurrence into upcoming dated records. When a
        source has no curated calls and rolling_fallback is True, returns a
        single rolling record (deadline_date='') so a confirmed-open call still
        surfaces. On a fetch/decode failure the caller passes rolling_fallback=
        False: we emit only real curated data, never fabricate a rolling
        deadline for a page we could not even load.

        Args:
            key: Source key, used only for logging.
            source: Source dict with name, url, org, known_deadlines.
            description: Description text to attach to each record.
            rolling_fallback: Emit a dateless rolling record when there are no
                curated calls (default True; pass False on fetch failure).

        Returns:
            List of deadline dicts ready for upsert_deadline().
        """
        name = source["name"]
        url = source["url"]
        org = source["org"]
        score = self._relevance_score(org)
        today = datetime.now(timezone.utc).date()

        records: list[dict] = []
        for entry in source.get("known_deadlines", []):
            for iso in curated_deadline_dates(entry, today):
                records.append(
                    {
                        "type": "funding",
                        "name": f"{name} ({entry['label']})",
                        "organization": org,
                        "deadline_date": iso,
                        "event_date": None,
                        "url": url,
                        "description": description,
                        "relevance_score": score,
                    }
                )

        # No curated calls: surface as a single rolling (dateless) opportunity,
        # unless the caller suppressed it (fetch failed — nothing real to assert)
        if not records and rolling_fallback:
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

    def _scrape_source(self, key: str, source: dict) -> list[dict]:
        """Fetch and parse a single funding source page.

        Live scraping is an override on top of the curated registry. When the
        page yields a real parsed date it is used; otherwise the source's
        curated known_deadlines (or a rolling record) are emitted. A fetch or
        decode failure emits a health-marker record so /status can surface a
        persistently-failing source rather than treating it as "no calls".

        Args:
            key: Source key (e.g. "nsf_ses"), used only for logging.
            source: Dict with name, url, org, known_deadlines keys.

        Returns:
            List of deadline dicts ready for upsert_deadline().
        """
        name = source["name"]
        url = source["url"]
        org = source["org"]
        score = self._relevance_score(org)

        try:
            raw = self.fetch_url(url, timeout=45)
        except Exception as exc:
            self.stats["errors"] = int(self.stats["errors"]) + 1
            print(f"[funding] fetch failed for {key} ({url}): {exc}", file=sys.stderr)
            # Emit the curated registry anyway (it does not depend on the page),
            # and mark the source unhealthy so /status can see the failure
            self.stats.setdefault("failed_sources", [])
            self.stats["failed_sources"].append(key)
            return self._curated_records(
                key, source, "FETCH FAILED (using curated deadlines)", rolling_fallback=False
            )

        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            self.stats["errors"] = int(self.stats["errors"]) + 1
            print(f"[funding] decode error for {key}: {exc}", file=sys.stderr)
            self.stats.setdefault("failed_sources", [])
            self.stats["failed_sources"].append(key)
            return self._curated_records(
                key, source, "DECODE FAILED (using curated deadlines)", rolling_fallback=False
            )

        dates = _extract_deadline_dates(html)
        description = _extract_description(html, name)

        print(
            f"[funding] {key}: found {len(dates)} deadline date(s)",
            file=sys.stderr,
        )

        if not dates:
            # Scraping yielded nothing (JS-rendered page): fall back to the
            # curated known_deadlines registry as the primary data source
            return self._curated_records(key, source, description)

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

            run_status = "partial_success" if int(self.stats["errors"]) > 0 else "success"
            log_sensor_end(
                run_id,
                run_status,
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
            "status": (
                "error"
                if "error_message" in self.stats
                else "partial_success" if int(self.stats["errors"]) > 0 else "success"
            ),
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
