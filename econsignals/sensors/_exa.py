"""Shared Exa API helper utilities for sensors.

Supports research paper search, tweet search, and domain-filtered queries.
Includes disk cache to avoid redundant API calls across runs.
Uses only Python stdlib networking primitives.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from econsignals.sensors._base import _SSL_CTX, PROJ_ROOT

_EXA_SEARCH_URL = "https://api.exa.ai/search"
_MAX_RESULTS = 25
_MAX_CHARACTERS = 1200

_CACHE_DIR = PROJ_ROOT / "data" / "exa_cache"
_CACHE_TTL_HOURS = 12


def _cache_key(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _cache_get(key: str) -> list[dict] | None:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
        ts = data.get("ts", 0)
        if (time.time() - ts) > _CACHE_TTL_HOURS * 3600:
            return None
        results = data.get("results")
        return results if isinstance(results, list) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _cache_put(key: str, results: list[dict]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "results": results}
        (_CACHE_DIR / f"{key}.json").write_text(json.dumps(payload), "utf-8")
    except OSError:
        pass


def exa_search(
    query: str,
    *,
    num_results: int = 10,
    max_characters: int = _MAX_CHARACTERS,
    category: str = "research paper",
    include_domains: list[str] | None = None,
    start_published_date: str | None = None,
    timeout: int = 30,
    log_prefix: str | None = None,
) -> list[dict]:
    """Run an Exa search query and return raw result objects.

    Results are cached to disk for 12 hours to avoid redundant API calls.
    Returns an empty list when EXA_API_KEY is missing or any error occurs.
    """
    api_key = (os.environ.get("EXA_API_KEY") or "").strip()
    if not api_key:
        if log_prefix:
            print(f"{log_prefix} EXA_API_KEY not set; skipping", file=sys.stderr)
        return []

    query = (query or "").strip()
    if not query:
        return []

    try:
        requested_results = max(1, min(_MAX_RESULTS, int(num_results)))
    except (TypeError, ValueError):
        requested_results = 10
    try:
        requested_chars = max(1, min(_MAX_CHARACTERS, int(max_characters)))
    except (TypeError, ValueError):
        requested_chars = _MAX_CHARACTERS

    payload: dict = {
        "query": query,
        "type": "fast",
        "num_results": requested_results,
        "category": category,
        "contents": {
            "highlights": {
                "max_characters": requested_chars,
            }
        },
    }

    if include_domains:
        payload["include_domains"] = include_domains
    if start_published_date:
        payload["start_published_date"] = start_published_date

    # Check cache first
    ckey = _cache_key(payload)
    cached = _cache_get(ckey)
    if cached is not None:
        if log_prefix:
            print(f"{log_prefix} cache hit ({len(cached)} results)", file=sys.stderr)
        return cached

    data = json.dumps(payload).encode("utf-8")
    req = Request(
        _EXA_SEARCH_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": api_key,
            "User-Agent": "EconSignals/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            raw = resp.read()
    except HTTPError as exc:
        if log_prefix:
            print(f"{log_prefix} Exa API HTTP {exc.code}", file=sys.stderr)
        return []
    except (URLError, TimeoutError, ValueError) as exc:
        if log_prefix:
            print(f"{log_prefix} Exa request failed: {exc}", file=sys.stderr)
        return []

    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        if log_prefix:
            print(f"{log_prefix} Exa response was not valid JSON", file=sys.stderr)
        return []

    if not isinstance(parsed, dict):
        return []

    results = parsed.get("results")
    if not isinstance(results, list):
        nested = parsed.get("data")
        if isinstance(nested, dict):
            results = nested.get("results")
    if not isinstance(results, list):
        return []

    out = [r for r in results if isinstance(r, dict)]

    _cache_put(ckey, out)
    return out
