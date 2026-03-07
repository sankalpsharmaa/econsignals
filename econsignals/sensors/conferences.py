"""Conference CFP (Call for Papers) sensor for EconSignals.

Tracks submission deadlines and event dates for major economics conferences.

For each conference the sensor attempts HTML scraping of the conference
website to extract CFP text, deadline dates, and event dates. When the
site offers no parseable dates the sensor falls back to generating a
plausible deadline from the conference's typical submission month.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from html import unescape
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors _base.py convention)
# ---------------------------------------------------------------------------

from econsignals.sensors._base import BaseSensor

# ---------------------------------------------------------------------------
# Conference registry
# ---------------------------------------------------------------------------

CONFERENCES: dict[str, dict] = {
    "neudc": {
        "name": "Northeast Universities Development Consortium",
        "url": "https://neudc.org",
        "org": "NEUDC",
        "typical_deadline_month": 7,   # July
        "typical_event_month": 11,     # November
        "relevance": 1.0,
    },
    "pacdev": {
        "name": "Pacific Conference for Development Economics",
        "url": "https://pacdev.ucdavis.edu/",
        "org": "PacDev",
        "typical_deadline_month": 11,
        "typical_event_month": 3,
        "relevance": 1.0,
    },
    "bread": {
        "name": "Bureau for Research and Economic Analysis of Development",
        "url": "https://www.ibread.org/conferences",
        "org": "BREAD",
        "typical_deadline_month": None,
        "typical_event_month": None,
        "relevance": 1.0,
    },
    "aea": {
        "name": "American Economic Association Annual Meeting",
        "url": "https://www.aeaweb.org/conference/",
        "org": "AEA",
        "typical_deadline_month": 3,
        "typical_event_month": 1,
        "relevance": 0.8,
    },
    "uea": {
        "name": "Urban Economics Association",
        "url": "https://urbaneconomics.org/meetings/",
        "org": "UEA",
        "typical_deadline_month": 2,
        "typical_event_month": 10,
        "relevance": 1.0,
    },
    "assa": {
        "name": "Allied Social Science Associations",
        "url": "https://www.aeaweb.org/conference/",
        "org": "ASSA",
        "typical_deadline_month": 3,
        "typical_event_month": 1,
        "relevance": 0.7,
    },
    "nber_si": {
        "name": "NBER Summer Institute",
        "url": "https://www.nber.org/conferences/summer-institute",
        "org": "NBER",
        "typical_deadline_month": 2,
        "typical_event_month": 7,
        "relevance": 0.9,
    },
}

# ---------------------------------------------------------------------------
# Regex patterns for date extraction
# ---------------------------------------------------------------------------

# Patterns that signal a CFP section heading
_RE_CFP_HEADING = re.compile(
    r"call\s+for\s+papers|submission\s+deadline|paper\s+submission"
    r"|abstract\s+submission|deadline\s+for\s+submission",
    re.IGNORECASE,
)

# Deadline signal phrases preceding a date
_RE_DEADLINE_PHRASE = re.compile(
    r"(?:submission|abstract|paper|proposal|deadline)[^\n]{0,60}?(\b\w+\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}\s+\w+\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b)",
    re.IGNORECASE,
)

# Conference / event date signal phrases
_RE_EVENT_PHRASE = re.compile(
    r"(?:conference|meeting|workshop|event|held|take\s+place)[^\n]{0,80}?"
    r"(\b\w+\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,?\s+\d{4}\b"
    r"|\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s+\w+\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b)",
    re.IGNORECASE,
)

# Bare date patterns (for opportunistic search in CFP snippets)
_RE_DATE_BARE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"
    r"|\b(\w+ \d{1,2},? \d{4})\b"
    r"|\b(\d{1,2} \w+ \d{4})\b",
    re.IGNORECASE,
)

# Formats to try when parsing a found date string
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)

# ---------------------------------------------------------------------------
# HTML / text helpers
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace.

    Args:
        html: Raw HTML string.

    Returns:
        Plain text with normalised whitespace.

    Examples:
        >>> _strip_html("<p>Hello &amp; <b>world</b></p>")
        'Hello & world'
    """
    if not html:
        return ""
    html = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date_string(raw: str) -> Optional[str]:
    """Attempt to parse a raw date string into an ISO date.

    Args:
        raw: A raw date string such as "July 15, 2025" or "2025-07-15".

    Returns:
        ISO date string "YYYY-MM-DD", or None if parsing fails.

    Examples:
        >>> _parse_date_string("July 15, 2025")
        '2025-07-15'
        >>> _parse_date_string("nonsense")
    """
    raw = raw.strip().rstrip(".,;")
    # Strip trailing range component like "15-17" -> keep first date
    raw = re.sub(r"(\d{1,2})\s*[-–]\s*\d{1,2}(,?\s+\d{4})", r"\1\2", raw)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw[: len(fmt) + 6], fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    return None


def _extract_cfp_snippet(text: str, window: int = 800) -> str:
    """Return the text surrounding the first CFP-related heading.

    Args:
        text: Plain text of the page.
        window: Number of characters to include after the match.

    Returns:
        Substring around the CFP heading, or the first 800 chars if not found.
    """
    m = _RE_CFP_HEADING.search(text)
    if m:
        start = max(0, m.start() - 80)
        return text[start : start + window]
    return text[:window]


def _find_deadline_date(text: str) -> Optional[str]:
    """Search text for a submission deadline date.

    Tries deadline-phrase patterns first, then bare dates inside a CFP
    snippet as a last resort.

    Args:
        text: Plain text of the conference page.

    Returns:
        ISO date string, or None if no date could be extracted.
    """
    for m in _RE_DEADLINE_PHRASE.finditer(text):
        parsed = _parse_date_string(m.group(1))
        if parsed:
            return parsed

    # Fallback: bare dates inside the CFP snippet
    snippet = _extract_cfp_snippet(text)
    for m in _RE_DATE_BARE.finditer(snippet):
        candidate = next(g for g in m.groups() if g)
        parsed = _parse_date_string(candidate)
        if parsed:
            return parsed

    return None


def _find_event_date(text: str) -> Optional[str]:
    """Search text for a conference event date.

    Args:
        text: Plain text of the conference page.

    Returns:
        ISO date string, or None if no date could be extracted.
    """
    for m in _RE_EVENT_PHRASE.finditer(text):
        parsed = _parse_date_string(m.group(1))
        if parsed:
            return parsed
    return None


def _find_cfp_url(html: str, base_url: str) -> Optional[str]:
    """Locate a 'call for papers' or 'submit' hyperlink in the page HTML.

    Args:
        html: Raw page HTML.
        base_url: Base URL for resolving relative hrefs.

    Returns:
        Absolute URL of the CFP/submission page, or None.
    """
    pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*'
        r"(?:call\s+for\s+papers|cfp|submit|submission|paper\s+submission)"
        r"\s*</a>",
        re.IGNORECASE,
    )
    m = pattern.search(html)
    if not m:
        return None
    href = m.group(1).strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        # Resolve against scheme+host
        host_m = re.match(r"(https?://[^/]+)", base_url)
        if host_m:
            return host_m.group(1) + href
    return None


# ---------------------------------------------------------------------------
# Fallback date generation
# ---------------------------------------------------------------------------


def _fallback_deadline(typical_month: int) -> str:
    """Generate a plausible deadline ISO date from a typical submission month.

    If the typical month is still in the future this year, use this year.
    Otherwise use next year (the deadline hasn't happened yet for the next
    cycle).

    Args:
        typical_month: Integer 1-12 representing the expected deadline month.

    Returns:
        ISO date string for the 15th of the target month and year.

    Examples:
        >>> import datetime
        >>> # Returns a YYYY-MM-15 string
        >>> bool(_fallback_deadline(7))
        True
    """
    today = date.today()
    year = today.year
    # Use the 15th as a generic mid-month placeholder
    candidate = date(year, typical_month, 15)
    if candidate < today:
        # Deadline already passed this year; generate for next year
        candidate = date(year + 1, typical_month, 15)
    return candidate.isoformat()


def _fallback_event(typical_month: int, deadline_year: int) -> str:
    """Generate a plausible event ISO date from a typical conference month.

    Args:
        typical_month: Integer 1-12 representing the expected conference month.
        deadline_year: Year used for the corresponding deadline (used as anchor).

    Returns:
        ISO date string for the 1st of the target month and year.
    """
    today = date.today()
    year = today.year
    candidate = date(year, typical_month, 1)
    if candidate < today:
        candidate = date(year + 1, typical_month, 1)
    return candidate.isoformat()


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class ConferencesSensor(BaseSensor):
    """Sensor that tracks CFP deadlines for major economics conferences.

    For each conference the sensor fetches the conference website, parses
    the HTML for CFP details (deadline dates, event dates, submission links,
    and descriptive text), and falls back to generating plausible dates from
    the configured typical months when live parsing yields nothing.

    Results are inserted into the ``deadlines`` table via ``upsert_deadline``.
    This sensor overrides ``run()`` because it writes to ``deadlines`` rather
    than ``papers``.

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category (deadlines).
        rate_limit: Polite rate limit (0.3 req/s) to avoid hammering small
                    conference sites.
    """

    name = "conferences"
    watch = "deadlines"
    rate_limit = 0.3

    # ------------------------------------------------------------------
    # Per-conference fetch + parse
    # ------------------------------------------------------------------

    def _fetch_text_and_html(self, url: str) -> tuple[Optional[str], Optional[str]]:
        """Fetch a URL and return (plain_text, raw_html).

        Args:
            url: URL to retrieve.

        Returns:
            Tuple of (plain_text, raw_html). Both are None on fetch error.
        """
        try:
            raw = self.fetch_url(url, timeout=30)
            html = raw.decode("utf-8", errors="replace")
            return _strip_html(html), html
        except Exception as exc:
            print(f"[conferences] failed to fetch {url!r}: {exc}", file=sys.stderr)
            return None, None

    def _parse_conference(self, key: str, conf: dict) -> Optional[dict]:
        """Fetch and parse a single conference page.

        Steps:
        1. Fetch the configured URL.
        2. Attempt to extract a CFP-specific sub-URL and refetch it.
        3. Parse deadline and event dates from the page text.
        4. Fall back to typical_deadline_month / typical_event_month when
           no dates are found in the HTML.
        5. Skip the conference entirely if neither live dates nor typical
           months are available.

        Args:
            key: Conference key (e.g. "neudc").
            conf: Conference configuration dict.

        Returns:
            A deadline dict ready for ``upsert_deadline``, or None if the
            conference should be skipped.
        """
        base_url: str = conf["url"]
        text, html = self._fetch_text_and_html(base_url)
        if text is None or html is None:
            self.stats["errors"] = int(self.stats["errors"]) + 1
            return None

        cfp_url: Optional[str] = None

        # Attempt to find a dedicated CFP sub-page
        if html:
            cfp_url = _find_cfp_url(html, base_url)
            if cfp_url and cfp_url != base_url:
                cfp_text, cfp_html = self._fetch_text_and_html(cfp_url)
                if cfp_text is None or cfp_html is None:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                elif cfp_text:
                    # Prefer the sub-page content for date extraction
                    text = cfp_text

        deadline_date: Optional[str] = _find_deadline_date(text) if text else None
        event_date: Optional[str] = _find_event_date(text) if text else None

        # Extract a short description from around the CFP heading
        description: Optional[str] = None
        if text:
            snippet = _extract_cfp_snippet(text, window=400)
            description = snippet[:400] if snippet else None

        # Fall back to typical months when parsing yields nothing
        used_fallback = False
        if deadline_date is None and conf.get("typical_deadline_month"):
            deadline_date = _fallback_deadline(conf["typical_deadline_month"])
            used_fallback = True

        if event_date is None and conf.get("typical_event_month"):
            # Anchor event year to the deadline year when available
            dl_year = int(deadline_date[:4]) if deadline_date else date.today().year
            event_date = _fallback_event(conf["typical_event_month"], dl_year)

        # If we have neither a live date nor a typical month, skip
        if deadline_date is None and event_date is None:
            print(
                f"[conferences] {key}: no dates found and no typical months configured;"
                f" skipping",
                file=sys.stderr,
            )
            return None

        if used_fallback:
            print(
                f"[conferences] {key}: used fallback deadline {deadline_date}",
                file=sys.stderr,
            )
        else:
            print(
                f"[conferences] {key}: deadline={deadline_date} event={event_date}",
                file=sys.stderr,
            )

        return {
            "type": "cfp",
            "name": conf["name"],
            "organization": conf["org"],
            "deadline_date": deadline_date,
            "event_date": event_date,
            "url": cfp_url or base_url,
            "description": description,
            "relevance_score": conf["relevance"],
        }

    # ------------------------------------------------------------------
    # collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Collect CFP deadline records for all configured conferences.

        For each conference: fetch the URL, parse dates, fall back to
        typical months, and return a deadline dict. Conferences that yield
        no usable date information are silently skipped.

        Returns:
            List of deadline dicts compatible with ``upsert_deadline``.
        """
        results: list[dict] = []
        for key, conf in CONFERENCES.items():
            print(f"[conferences] processing {key}: {conf['url']}", file=sys.stderr)
            try:
                record = self._parse_conference(key, conf)
            except Exception as exc:
                warnings.warn(f"[conferences] {key}: unexpected error: {exc}")
                self.stats["errors"] = int(self.stats["errors"]) + 1
                continue
            if record is not None:
                results.append(record)

        print(f"[conferences] collected {len(results)} deadline records", file=sys.stderr)
        return results

    # ------------------------------------------------------------------
    # run (override: writes to deadlines, not papers)
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute the sensor: collect CFP records and upsert into the DB.

        Overrides BaseSensor.run() because this sensor writes to the
        ``deadlines`` table via ``upsert_deadline`` rather than going
        through the paper deduplication pipeline.

        Returns:
            Stats dict with keys: sensor, watch, status, found, new, errors.
        """
        from econsignals.lib.db import log_sensor_start, log_sensor_end, upsert_deadline

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                try:
                    row_id = upsert_deadline(item)
                    if row_id:
                        self.stats["new"] = int(self.stats["new"]) + 1
                except Exception as exc:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(
                        f"  [conferences] error upserting deadline"
                        f" {item.get('name')!r}: {exc}",
                        file=sys.stderr,
                    )

            run_status = "partial_success" if int(self.stats["errors"]) > 0 else "success"
            log_sensor_end(run_id, run_status, self.stats["found"], self.stats["new"])
        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"Sensor {self.name} failed: {exc}", file=sys.stderr)

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
    sensor = ConferencesSensor()
    sensor.run()
