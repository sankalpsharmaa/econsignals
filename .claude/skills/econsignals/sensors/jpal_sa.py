"""J-PAL South Asia sensor for EconSignals.

Monitors evaluations and publications from J-PAL South Asia at
povertyactionlab.org.  The site is a JavaScript-heavy React application,
so this sensor uses two approaches:

Primary: JSON API endpoint that backs the evaluations listing React page.
Fallback: HTML parsing of the evaluations and publications listing pages.

Both approaches are wrapped defensively -- if the site structure changes or
the network is unavailable, collect() returns an empty list rather than
raising.

Usage:
    python sensors/jpal_sa.py

State:
    Persisted under the "jpal_sa" key in watches/india/state.json.
    Tracks last successful fetch date to avoid re-ingesting old items.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urljoin

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_SENSORS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SENSORS_DIR.parent
sys.path.insert(0, str(_SKILL_ROOT))

from sensors._base import BaseSensor, PROJ_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.povertyactionlab.org"
_EVALUATIONS_URL = f"{_BASE_URL}/evaluations"
_PUBLICATIONS_URL = f"{_BASE_URL}/publications"

# Candidate JSON API endpoints observed on the J-PAL React frontend.
# The site uses Gatsby / GraphQL under the hood; these paths may work.
_API_CANDIDATES: list[str] = [
    f"{_BASE_URL}/page-data/evaluations/page-data.json",
    f"{_BASE_URL}/api/evaluations",
    f"{_BASE_URL}/api/evaluations?region=south-asia",
]

_SOUTH_ASIA_KEYWORDS = {"south asia", "south-asia", "india", "bangladesh", "pakistan",
                        "sri lanka", "nepal", "bhutan", "maldives", "afghanistan"}

_BASE_KEYWORDS = ["J-PAL", "RCT", "India", "South Asia"]

_DEFAULT_LOOKBACK_DAYS = 90

_STATE_PATH = PROJ_ROOT / "watches" / "india" / "state.json"

# Sector / theme tags that J-PAL uses on evaluation pages
_SECTOR_PATTERN = re.compile(
    r'(?:sector|theme|tag)[^"]*"[^"]*"([^"]{2,60})"',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load watches/india/state.json.

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
    """Atomically write state to watches/india/state.json.

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
# HTML helpers
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace.

    Args:
        html: Raw HTML string.

    Returns:
        Plain text with collapsed whitespace.

    Examples:
        >>> _strip_html("<p>Hello <b>world</b></p>")
        'Hello world'
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


def _parse_date(raw: str | None) -> str | None:
    """Parse a date string to ISO YYYY-MM-DD.

    Args:
        raw: Raw date string.

    Returns:
        'YYYY-MM-DD' string, or None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%m/%d/%Y", "%B %Y", "%b %Y", "%Y"):
        try:
            parsed = datetime.strptime(raw[: len(fmt) + 5], fmt)
            return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    return None


def _slug_to_source_id(path_or_url: str) -> str:
    """Convert a URL path to a stable source_id string.

    Examples:
        >>> _slug_to_source_id("/evaluation/some-rct-name")
        'jpal:evaluation/some-rct-name'
        >>> _slug_to_source_id("https://www.povertyactionlab.org/evaluation/foo")
        'jpal:evaluation/foo'
    """
    path = path_or_url.strip()
    # Strip base URL if present
    for prefix in (f"{_BASE_URL}/", "https://povertyactionlab.org/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    path = path.lstrip("/")
    return f"jpal:{path}" if path else f"jpal:{hashlib.md5(path_or_url.encode()).hexdigest()[:12]}"


def _is_south_asia(text: str) -> bool:
    """Return True if any South Asia keyword appears in text.

    Args:
        text: Lowercased text to check.

    Returns:
        True if a South Asia region keyword is found.
    """
    lower = text.lower()
    return any(kw in lower for kw in _SOUTH_ASIA_KEYWORDS)


# ---------------------------------------------------------------------------
# JSON API parsing
# ---------------------------------------------------------------------------


def _extract_items_from_json(data: object) -> list[dict]:
    """Recursively search a parsed JSON structure for evaluation/publication items.

    J-PAL's Gatsby page-data JSON nests items deep inside a GraphQL result.
    This function walks the structure looking for lists of objects that have
    a ``title`` field and at least one of ``slug`` / ``url`` / ``path``.

    Args:
        data: Parsed JSON object (dict, list, or scalar).

    Returns:
        Flat list of candidate item dicts.
    """
    items: list[dict] = []
    if isinstance(data, list):
        for elem in data:
            if isinstance(elem, dict) and elem.get("title"):
                items.append(elem)
            else:
                items.extend(_extract_items_from_json(elem))
    elif isinstance(data, dict):
        # If this dict looks like a candidate record, collect it
        if data.get("title") and (data.get("slug") or data.get("url") or data.get("path")):
            items.append(data)
        else:
            for val in data.values():
                items.extend(_extract_items_from_json(val))
    return items


def _parse_json_item(item: dict, paper_type: str) -> dict | None:
    """Convert a raw J-PAL JSON item dict to the standard paper dict.

    Args:
        item: Raw dict from the JSON API / page-data response.
        paper_type: 'report' for evaluations, 'policy_brief' for publications.

    Returns:
        Standard paper dict or None if the item lacks a usable title.
    """
    title = (
        item.get("title") or item.get("name") or item.get("shortTitle") or ""
    ).strip()
    if not title:
        return None

    # Build URL
    slug = (item.get("slug") or item.get("path") or item.get("url") or "").strip()
    if slug.startswith("http"):
        url = slug
    elif slug:
        url = urljoin(_BASE_URL + "/", slug.lstrip("/"))
    else:
        url = None

    # Check region relevance
    region_text = " ".join([
        item.get("region", ""),
        item.get("regions", ""),
        str(item.get("countries", "")),
        item.get("location", ""),
        title,
    ])
    if not _is_south_asia(region_text):
        return None

    # Authors / PIs
    raw_pis = item.get("pis") or item.get("authors") or item.get("researchers") or []
    authors: list[str] = []
    for pi in raw_pis:
        if isinstance(pi, str):
            name = pi.strip()
        elif isinstance(pi, dict):
            name = (
                pi.get("name") or pi.get("displayName") or pi.get("fullName") or ""
            ).strip()
        else:
            name = ""
        if name:
            authors.append(name)

    # Abstract / summary
    abstract_raw = (
        item.get("abstract")
        or item.get("summary")
        or item.get("description")
        or item.get("shortDescription")
        or ""
    )
    abstract = _strip_html(str(abstract_raw)).strip() or None
    if abstract:
        abstract = abstract[:3000]

    # Date
    date_raw = (
        item.get("publicationDate")
        or item.get("date")
        or item.get("published")
        or item.get("firstPublished")
        or ""
    )
    published_at = _parse_date(str(date_raw)) if date_raw else None

    # Keywords: sector / theme tags + base keywords
    tags: list[str] = list(_BASE_KEYWORDS)
    for tag_field in ("sectors", "tags", "themes", "topics"):
        raw_tags = item.get(tag_field) or []
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    tags.append(t.strip())
                elif isinstance(t, dict):
                    tag_name = (t.get("name") or t.get("label") or "").strip()
                    if tag_name:
                        tags.append(tag_name)
        elif isinstance(raw_tags, str) and raw_tags.strip():
            tags.append(raw_tags.strip())

    # Deduplicate tags while preserving order
    seen_tags: set[str] = set()
    keywords: list[str] = []
    for tag in tags:
        if tag not in seen_tags:
            seen_tags.add(tag)
            keywords.append(tag)

    source_id = _slug_to_source_id(url or title)

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": None,
        "url": url,
        "published_at": published_at,
        "paper_type": paper_type,
        "jel_codes": [],
        "keywords": keywords,
        "source_id": source_id,
        "source_url": url,
        "raw_metadata": item,
    }


# ---------------------------------------------------------------------------
# HTML scraping helpers
# ---------------------------------------------------------------------------


def _extract_cards_from_html(html: str, base_url: str, paper_type: str) -> list[dict]:
    """Parse evaluation/publication cards from a J-PAL listing page HTML.

    J-PAL renders cards as ``<article>`` or ``<div>`` elements with a link,
    title, and optional summary.  This function is deliberately lenient --
    it collects whatever it can find rather than expecting a fixed structure.

    Args:
        html: Raw HTML string from the listing page.
        base_url: Base URL used to resolve relative hrefs.
        paper_type: 'report' for evaluations, 'policy_brief' for publications.

    Returns:
        List of parsed paper dicts (may be empty).
    """
    papers: list[dict] = []
    seen_urls: set[str] = set()

    # Look for JSON-LD structured data first (fastest path)
    for ld_match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            ld_data = json.loads(ld_match.group(1))
            items = _extract_items_from_json(ld_data)
            for item in items:
                parsed = _parse_json_item(item, paper_type)
                if parsed and parsed["url"] not in seen_urls:
                    seen_urls.add(parsed["url"] or "")
                    papers.append(parsed)
        except Exception:
            pass

    if papers:
        return papers

    # Fallback: look for inline __GATSBY_STATE__ or window.__INITIAL_DATA__ blobs
    for state_match in re.finditer(
        r'window\.__(?:GATSBY_STATE|INITIAL_DATA|PAGE_DATA)__\s*=\s*(\{.*?\});',
        html,
        re.DOTALL,
    ):
        try:
            state_data = json.loads(state_match.group(1))
            items = _extract_items_from_json(state_data)
            for item in items:
                parsed = _parse_json_item(item, paper_type)
                if parsed and parsed.get("url") not in seen_urls:
                    seen_urls.add(parsed["url"] or "")
                    papers.append(parsed)
        except Exception:
            pass

    if papers:
        return papers

    # Last resort: regex over <a> link + title + summary text patterns
    # Matches patterns like:
    #   <a href="/evaluation/..."><h2>Title</h2></a>
    #   or card-style <div> blocks
    card_pattern = re.compile(
        r'<a\s+[^>]*href=["\']([^"\']+(?:evaluation|publication)[^"\']*)["\'][^>]*>'
        r'(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in card_pattern.finditer(html):
        href = m.group(1).strip()
        inner = m.group(2)

        title = _strip_html(inner).strip()
        if not title or len(title) < 5 or len(title) > 500:
            continue

        # Resolve URL
        if href.startswith("http"):
            item_url = href
        else:
            item_url = urljoin(base_url + "/", href.lstrip("/"))

        if item_url in seen_urls:
            continue

        # Only include South Asia relevant items
        if not _is_south_asia(href + " " + title):
            continue

        seen_urls.add(item_url)
        source_id = _slug_to_source_id(item_url)

        papers.append({
            "title": title,
            "authors": [],
            "abstract": None,
            "doi": None,
            "url": item_url,
            "published_at": None,
            "paper_type": paper_type,
            "jel_codes": [],
            "keywords": list(_BASE_KEYWORDS),
            "source_id": source_id,
            "source_url": item_url,
            "raw_metadata": {"href": href, "source": "html_scrape"},
        })

    return papers


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class JPALSouthAsiaSensor(BaseSensor):
    """Monitor J-PAL South Asia evaluations and publications.

    J-PAL's website is a JavaScript-rendered React/Gatsby application.
    This sensor attempts three collection strategies in order:

    1. Fetch Gatsby page-data JSON (fast, structured, no JS required).
    2. Probe known API endpoint candidates for JSON responses.
    3. Fetch and regex-parse the HTML listing pages as a last resort.

    All strategies filter for South Asia region evaluations and apply
    region + methodological keywords (J-PAL, RCT, India, South Asia).

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category; 'india'.
        rate_limit: 0.5 requests/second (conservative for a small NGO site).
        EVALUATIONS_URL: Evaluations listing URL with South Asia region filter.
        PUBLICATIONS_URL: Publications listing URL.
    """

    name = "jpal_sa"
    watch = "india"
    rate_limit = 0.5

    EVALUATIONS_URL = f"{_EVALUATIONS_URL}?region=south-asia"
    PUBLICATIONS_URL = _PUBLICATIONS_URL

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _from_date(self) -> str:
        """Return ISO date string to use as the lower bound for publication dates.

        Reads jpal_sa.last_fetched from watches/india/state.json.
        Falls back to 90 days ago on first run.

        Returns:
            Date string in 'YYYY-MM-DD' format.
        """
        state = _load_state()
        sensor_state = state.get("sensors", {}).get("jpal_sa") or {}
        last_fetched = sensor_state.get("last_fetched")
        if last_fetched:
            try:
                datetime.fromisoformat(last_fetched)
                return last_fetched
            except ValueError:
                pass
        return (
            datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

    def _update_state(self) -> None:
        """Record today's date as the last successful jpal_sa fetch."""
        state = _load_state()
        sensors = state.setdefault("sensors", {})
        sensors.setdefault("jpal_sa", {})["last_fetched"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        try:
            _save_state(state)
        except Exception as exc:
            print(f"[jpal_sa] failed to save state: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # JSON API strategies
    # ------------------------------------------------------------------

    def _try_page_data_json(self, page_path: str) -> list[dict]:
        """Fetch a Gatsby page-data.json and extract items.

        Gatsby sites expose pre-rendered page data at:
            /page-data/<path>/page-data.json

        Args:
            page_path: Page path, e.g. 'evaluations'.

        Returns:
            List of raw item dicts extracted from the JSON blob.
        """
        url = f"{_BASE_URL}/page-data/{page_path.strip('/')}/page-data.json"
        try:
            data = self.fetch_json(url)
            items = _extract_items_from_json(data)
            print(
                f"[jpal_sa] page-data JSON ({page_path}): found {len(items)} candidate items",
                file=sys.stderr,
            )
            return items
        except Exception as exc:
            print(
                f"[jpal_sa] page-data JSON ({page_path}) unavailable: {exc}",
                file=sys.stderr,
            )
            return []

    def _try_api_endpoint(self, url: str) -> list[dict]:
        """Probe a candidate JSON API endpoint and extract items.

        Args:
            url: Full URL to try.

        Returns:
            List of raw item dicts, or empty list if the endpoint fails or
            returns non-JSON.
        """
        try:
            data = self.fetch_json(url)
            items = _extract_items_from_json(data)
            print(
                f"[jpal_sa] API endpoint {url}: found {len(items)} items",
                file=sys.stderr,
            )
            return items
        except Exception as exc:
            print(f"[jpal_sa] API endpoint {url} failed: {exc}", file=sys.stderr)
            return []

    def _collect_via_json(self) -> list[dict] | None:
        """Try all JSON-based collection strategies.

        Attempts Gatsby page-data and known API endpoints.  Returns None if
        nothing succeeded (signals the caller to fall back to HTML parsing).

        Returns:
            List of parsed paper dicts, or None if all JSON approaches failed.
        """
        all_raw: list[dict] = []

        # Gatsby page-data for evaluations (with South Asia filter in query)
        all_raw.extend(self._try_page_data_json("evaluations"))
        all_raw.extend(self._try_page_data_json("south-asia"))

        # Probe known API endpoints
        for endpoint in _API_CANDIDATES:
            all_raw.extend(self._try_api_endpoint(endpoint))

        if not all_raw:
            return None

        papers: list[dict] = []
        seen_ids: set[str] = set()

        for item in all_raw:
            # Guess paper_type from item content
            item_str = json.dumps(item).lower()
            paper_type = "policy_brief" if "publication" in item_str else "report"

            parsed = _parse_json_item(item, paper_type)
            if not parsed:
                continue
            sid = parsed["source_id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            papers.append(parsed)

        return papers if papers else None

    # ------------------------------------------------------------------
    # HTML scraping fallback
    # ------------------------------------------------------------------

    def _collect_via_html(self) -> list[dict]:
        """Collect items by scraping the evaluations and publications HTML pages.

        Fetches both listing pages and applies _extract_cards_from_html.
        Any individual page fetch failure is caught and logged; the other
        page is still attempted.

        Returns:
            List of parsed paper dicts. May be empty if both pages fail.
        """
        papers: list[dict] = []
        seen_ids: set[str] = set()

        pages_to_scrape: list[tuple[str, str]] = [
            (self.EVALUATIONS_URL, "report"),
            (self.PUBLICATIONS_URL, "policy_brief"),
        ]

        for url, paper_type in pages_to_scrape:
            print(f"[jpal_sa] scraping HTML: {url}", file=sys.stderr)
            try:
                html_bytes = self.fetch_url(url)
                html = html_bytes.decode("utf-8", errors="replace")
            except Exception as exc:
                warnings.warn(f"[jpal_sa] failed to fetch {url}: {exc}")
                continue

            cards = _extract_cards_from_html(html, _BASE_URL, paper_type)
            print(
                f"[jpal_sa] scraped {len(cards)} cards from {url}",
                file=sys.stderr,
            )

            for card in cards:
                sid = card["source_id"]
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    papers.append(card)

        return papers

    # ------------------------------------------------------------------
    # Main collect
    # ------------------------------------------------------------------

    def collect(self) -> list[dict]:
        """Collect J-PAL South Asia evaluations and publications.

        Strategy:
        1. Attempt JSON-based collection (Gatsby page-data + API probing).
        2. If JSON yields nothing, fall back to HTML scraping.
        3. Update state on (at least partial) success.

        Any failure in a sub-strategy is logged and the next strategy is
        tried.  If all strategies fail, returns an empty list.

        Returns:
            List of paper dicts conforming to BaseSensor.collect() contract.
        """
        from_date = self._from_date()
        print(
            f"[jpal_sa] collecting South Asia evaluations/publications "
            f"(lookback from {from_date})",
            file=sys.stderr,
        )

        papers: list[dict] | None = None

        # Primary: JSON strategies
        try:
            papers = self._collect_via_json()
        except Exception as exc:
            warnings.warn(f"[jpal_sa] JSON collection raised unexpectedly: {exc}")
            papers = None

        # Fallback: HTML scraping
        if papers is None:
            warnings.warn(
                "[jpal_sa] JSON collection yielded nothing, falling back to HTML scraping"
            )
            try:
                papers = self._collect_via_html()
            except Exception as exc:
                warnings.warn(f"[jpal_sa] HTML scraping also failed: {exc}")
                papers = []

        print(f"[jpal_sa] collected {len(papers)} items total", file=sys.stderr)

        if papers:
            self._update_state()

        return papers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(_SKILL_ROOT))
    from lib.db import init_db

    init_db()
    sensor = JPALSouthAsiaSensor()
    sensor.run()
