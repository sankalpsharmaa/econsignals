"""Build the static dashboard snapshot (feed.json).

The web dashboard is a static SPA that cannot query SQLite directly, so this
module serializes the current database into one JSON file the SPA fetches. It
also applies presentation-layer denoising — econ-only social posts, and dropping
Twitter/Exa AI image-caption junk from deadlines — so the dashboard never
surfaces the noise that made the email newsletter untrustworthy.

The same filters are importable by the newsletter lens. Filtering here (rather
than deleting rows) is reversible and leaves the audit trail intact.

CLI:  python -m econsignals.lib.snapshot [output_path]
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from html import unescape as _html_unescape
from pathlib import Path

from econsignals.lib.db import (
    PROJ_ROOT,
    get_last_sensor_run,
    get_recent_social_items,
    get_top_papers,
    get_upcoming_deadlines,
)
from econsignals.lib.relevance import (
    _CORE_TOPICS,
    _INDIA_PATTERNS,
    load_interest_keywords,
)
from econsignals.lib.zotero_embeddings import compute_zotero_embedding_scores
from econsignals.lib.zotero_profile import load_zotero_corpus

def unescape(text: str) -> str:
    """Decode HTML entities, applied twice since some sources double-encode
    ("&amp;amp;" -> "&amp;" -> "&"). Idempotent once fully decoded.
    """
    return _html_unescape(_html_unescape(text))


# Default output: served by Vite during dev and copied into the build for Pages.
DEFAULT_OUTPUT: Path = PROJ_ROOT / "webapp" / "public" / "feed.json"

_SENSORS = [
    "openalex", "crossref", "iza", "bread", "imf",
    "mastodon", "bluesky", "twitter_bridge", "funding", "conferences",
]

# AI image/page captions Exa returns instead of real post text. These leaked
# into social_items and deadline names ("The image prominently displays...",
# "The tweet announces...", "The data visualization shows...").
_CAPTION_RE = re.compile(
    r"^\s*(the|this|an?)\s+"
    r"(image|photo|picture|graphic|graph|chart|visual\b|visualization|"
    r"infographic|screenshot|tweet|post|figure|map|table|data visualization|"
    r"accompanying)",
    re.IGNORECASE,
)

# Instances and handles known to carry economics-research content.
_ECON_INSTANCES = ("@econtwitter.net", "econtwitter.net")
_ECON_HANDLES = frozenset({
    "vox_dev", "nberpubs", "marketdesignbot", "voxeu", "cepr_org", "nberecon",
    "jpal", "poverty_action", "the_igc", "worldbank", "iza_bonn", "bread_dev",
})

_MAX_PAPERS = 250
_MAX_SOCIAL = 60
_ABSTRACT_CHARS = 320

# Zotero re-ranking: similarity to the user's own library is the strongest taste
# signal, so the feed is ranked by a blend of it and the base relevance (quality)
# score. _CANDIDATE_POOL papers are embedded; the top _MAX_PAPERS are kept.
_CANDIDATE_POOL = 600
_ZOTERO_WEIGHT = 0.45
# Zotero top-k similarity saturates near 7.0 for anything econ-ish and drops to
# ~2.5 for off-topic work, so normalize on a FIXED scale (min-max would collapse
# the homogeneous top). This demotes the off-topic tail while letting venue
# prestige (in relevance_score) rank the on-topic frontier.
_ZSIM_LO, _ZSIM_HI = 3.0, 7.0


def _is_caption(text: str | None) -> bool:
    """True if text looks like an Exa AI image-caption rather than real content."""
    if not text:
        return False
    return bool(_CAPTION_RE.match(text))


def _is_econ_handle(handle: str | None) -> bool:
    """True if the handle is on a known economics instance or allowlist."""
    if not handle:
        return False
    low = handle.lower()
    if any(inst in low for inst in _ECON_INSTANCES):
        return True
    stem = low.lstrip("@").split("@", 1)[0]
    return stem in _ECON_HANDLES


def _matched_topics(text: str, interest_kw: set[str]) -> list[str]:
    """Return the interest phrases that appear (whole-word) in the text."""
    low = text.lower()
    return sorted(
        kw for kw in interest_kw
        if re.search(rf"\b{re.escape(kw)}\b", low)
    )


def is_econ_social(item: dict, interest_kw: set[str]) -> bool:
    """Keep a social post only if it is plausibly about economics research.

    Reject AI image-captions outright, then accept when it links a paper, cites a
    DOI, comes from a known econ handle/instance, or matches a core interest
    topic. This cuts the ~88% firehose noise (weather bots, general news,
    unrelated politics) the audit flagged while keeping econ-Mastodon/Bluesky.
    """
    content = item.get("content") or ""
    if _is_caption(content):
        return False
    if item.get("paper_id"):
        return True
    if "doi.org/" in content.lower() or re.search(r"10\.\d{4,}/", content):
        return True
    if _is_econ_handle(item.get("author_handle")):
        return True
    # require a distinctive (core) topic match, not a single generic word
    return any(t in _CORE_TOPICS for t in _matched_topics(content, interest_kw))


def _clean_deadline(d: dict) -> bool:
    """Keep real deadlines; drop AI-caption junk and undated tweet 'deadlines'.

    A dated future deadline is always kept. A rolling (undated) deadline is kept
    only when it comes from a curated funder — i.e. it has a real organization
    that is not the "Twitter/X" tag the bridge stamps on scraped captions.
    """
    name = d.get("name") or ""
    if _is_caption(name) or len(name.strip()) < 4:
        return False

    has_date = bool((d.get("deadline_date") or "").strip())
    if has_date:
        return True

    org = (d.get("organization") or "").strip()
    return bool(org) and org.lower() not in ("twitter/x", "twitter", "x")


def _days_left(deadline_date: str | None) -> int | None:
    if not deadline_date:
        return None
    try:
        due = datetime.strptime(deadline_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (due - date.today()).days


def _urgency(days: int | None) -> str:
    if days is None:
        return "rolling"
    if days <= 3:
        return "final"
    if days <= 14:
        return "urgent"
    if days <= 45:
        return "soon"
    return "upcoming"


def _personalize(papers: list[dict]) -> tuple[list[dict], bool]:
    """Re-rank candidates by similarity to the user's Zotero library.

    Library similarity is the strongest signal of what the user actually reads,
    so the feed is sorted by a blend of normalized Zotero similarity and the base
    relevance (quality) score. Each paper gets `_zotero` (0-1) and `_final` (the
    blend). Falls back to relevance order, returning personalized=False, when
    Ollama or the Zotero corpus is unavailable or scoring fails.
    """
    for p in papers:
        p["_zotero"] = None
        p["_final"] = p.get("relevance_score") or 0.0

    if os.environ.get("ECONSIGNALS_NO_ZOTERO"):
        return papers, False
    try:
        corpus = load_zotero_corpus()
        if not corpus:
            return papers, False
        cands = [
            {"title": p.get("title") or "", "abstract": p.get("abstract") or ""}
            for p in papers
        ]
        scores = compute_zotero_embedding_scores(cands, corpus)
    except Exception as exc:  # Ollama down, model missing, corpus unreadable
        print(f"[snapshot] Zotero personalization skipped: {exc}", file=sys.stderr)
        return papers, False

    if not scores or len(scores) != len(papers) or max(scores) <= 0:
        return papers, False

    span = _ZSIM_HI - _ZSIM_LO
    for p, s in zip(papers, scores):
        z = max(0.0, min(1.0, (s - _ZSIM_LO) / span))
        p["_zotero"] = z
        p["_final"] = _ZOTERO_WEIGHT * z + (1 - _ZOTERO_WEIGHT) * (p.get("relevance_score") or 0.0)
    papers.sort(key=lambda p: p["_final"], reverse=True)
    return papers, True


def build_snapshot() -> dict:
    """Assemble the dashboard snapshot dict from the current database."""
    interest_kw = load_interest_keywords()
    now = datetime.now(timezone.utc)

    # Papers: take a quality-ranked candidate pool, then re-rank by similarity to
    # the user's Zotero library (their actual taste), then denoise.
    papers_raw = get_top_papers(limit=_CANDIDATE_POOL, min_score=0.0)
    papers_raw, personalized = _personalize(papers_raw)
    papers = []
    for p in papers_raw:
        # Source titles/abstracts often carry HTML entities ("&amp;", "&lt;");
        # decode them so the dashboard shows real characters.
        title = unescape(p.get("title") or "")
        abstract = unescape(p.get("abstract") or "")
        if _is_caption(title):
            continue
        text = f"{title} {abstract}"
        topics = _matched_topics(text, interest_kw)
        is_india = bool(_INDIA_PATTERNS.search(text))
        abstract_short = abstract[:_ABSTRACT_CHARS].rstrip()
        if abstract and len(abstract) > _ABSTRACT_CHARS:
            abstract_short += "…"
        papers.append({
            "id": p["id"],
            "title": title,
            "authors": [unescape(a["name"]) for a in p.get("authors", [])][:8],
            "abstract": abstract_short,
            "doi": p.get("doi"),
            "url": p.get("url") or (f"https://doi.org/{p['doi']}" if p.get("doi") else None),
            "source": p.get("primary_source"),
            "venue": unescape(p["primary_venue"]) if p.get("primary_venue") else None,
            "published_at": (p.get("published_at") or "")[:10] or None,
            "score": round(p.get("_final") if p.get("_final") is not None else (p.get("relevance_score") or 0.0), 3),
            "zotero": round(p["_zotero"], 3) if p.get("_zotero") is not None else None,
            "jel": p.get("jel_codes") or [],
            "topics": topics,
            "india": is_india,
        })
        if len(papers) >= _MAX_PAPERS:
            break

    # Deadlines: dated within 120 days + curated rolling, denoised
    deadlines_raw = get_upcoming_deadlines(days=120, include_rolling=True)
    deadlines = []
    for d in deadlines_raw:
        if not _clean_deadline(d):
            continue
        dl_date = (d.get("deadline_date") or "").strip() or None
        days = _days_left(dl_date)
        deadlines.append({
            "name": unescape(d.get("name") or ""),
            "type": d.get("type"),
            "organization": d.get("organization"),
            "due": dl_date,
            "days_left": days,
            "rolling": dl_date is None,
            "url": d.get("url"),
            "description": unescape((d.get("description") or "")[:240]),
            "urgency": _urgency(days),
        })

    # Social: econ-only, denoised, most recent first
    social_raw = get_recent_social_items(days=100000, limit=600)
    social = []
    for s in social_raw:
        if not is_econ_social(s, interest_kw):
            continue
        social.append({
            "handle": (s.get("author_handle") or "").lstrip("@") or None,
            "source": s.get("source"),
            "content": unescape((s.get("content") or "")[:280]),
            "url": s.get("url"),
            "paper_id": s.get("paper_id"),
            "published_at": (s.get("published_at") or "")[:10] or None,
        })
        if len(social) >= _MAX_SOCIAL:
            break

    # Sensor health for the status view
    sensors = []
    for name in _SENSORS:
        run = get_last_sensor_run(name)
        sensors.append({
            "sensor": name,
            "status": run.get("status") if run else "never run",
            "last_run": run.get("finished_at") or run.get("started_at") if run else None,
            "items_found": run.get("items_found") if run else None,
        })

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": {
            "papers": len(papers),
            "deadlines": len(deadlines),
            "social": len(social),
            "personalized": personalized,
        },
        "papers": papers,
        "deadlines": deadlines,
        "social": social,
        "sensors": sensors,
    }


def write_snapshot(output_path: Path | None = None) -> Path:
    """Build the snapshot and write it to disk; return the path written."""
    out = output_path or DEFAULT_OUTPUT
    out.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot()
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    """Console-script entry point: write feed.json and print a status line."""
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    path = write_snapshot(target)
    data = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({"status": "ok", "path": str(path), "stats": data["stats"]}))


if __name__ == "__main__":
    main()
