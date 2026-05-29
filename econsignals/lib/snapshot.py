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
from econsignals.lib.novelty import collapse_duplicates, suppress_seen
from econsignals.lib.rationale import rationale_batch
from econsignals.lib.relevance import (
    _CORE_TOPICS,
    _INDIA_PATTERNS,
    combine_percentile_ranks,
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
    "repec_nep", "nber", "arxiv", "worldbank", "semantic_scholar",
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

# Feed re-ranking: relevance, Zotero-library similarity, and the learned ranker
# are combined in per-batch percentile space (see _personalize). _CANDIDATE_POOL
# papers are scored; novelty then collapses/suppresses before the top _MAX_PAPERS
# are kept.
_CANDIDATE_POOL = 600
# Zotero top-k similarity saturates near 7.0 for anything econ-ish and drops to
# ~2.5 for off-topic work, so normalize on a FIXED scale (min-max would collapse
# the homogeneous top). This demotes the off-topic tail while letting venue
# prestige (in relevance_score) rank the on-topic frontier.
_ZSIM_LO, _ZSIM_HI = 3.0, 7.0

# Number of top feed items to summarise with a "why it matters" rationale. The
# call is a no-op (returns None) unless an LLM backend is configured, so this
# only bounds spend when one IS configured.
_RATIONALE_TOP_N = 25


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


def _specter2_zotero_scores(cands: list[dict], corpus: list[dict]) -> list[float] | None:
    """Score candidates by SPECTER2 top-k similarity to the Zotero corpus.

    Consulted only when ECONSIGNALS_EMBED_BACKEND=specter2 (and the heavy deps
    import); otherwise returns None so the caller uses the default Ollama path.
    Mirrors compute_zotero_embedding_scores' top-k pooling and floor rescaling by
    reusing that module's tuned constants, so the returned scores sit on the same
    0–_MAX_BOOST*scale band the Ollama path produces.

    Returns:
        Pre-scaled scores aligned to `cands`, or None when the backend is off or
        any embedding step fails.
    """
    from econsignals.lib import specter2_embeddings as s2

    if not s2.backend_enabled():
        return None

    from econsignals.lib.zotero_embeddings import (
        _MAX_BOOST,
        _SIM_FLOOR,
        _SIM_WIDTH,
        _TOP_K,
        _cosine_similarity,
    )

    try:
        import numpy as np
    except ImportError:
        return None

    cand_texts = [f"{c.get('title') or ''} {c.get('abstract') or ''}".strip() or "(no text)" for c in cands]
    corpus_texts = [str(c.get("text") or "").strip() for c in corpus]
    corpus_texts = [t for t in corpus_texts if t]
    if not corpus_texts:
        return None

    enc_corpus = s2.embed_texts(corpus_texts)
    enc_cands = s2.embed_texts(cand_texts)
    if not enc_corpus or not enc_cands:
        return None

    sim = _cosine_similarity(enc_cands, enc_corpus)
    k = min(_TOP_K, sim.shape[1])
    topk = np.sort(sim, axis=1)[:, -k:]
    pooled = topk.mean(axis=1)
    rel = np.clip((pooled - _SIM_FLOOR) / _SIM_WIDTH, 0.0, _MAX_BOOST)
    # Scale 10x to match compute_zotero_embedding_scores' default `scale`.
    return [float(s) for s in rel * 10.0]


def _zotero_norm_scores(papers: list[dict]) -> list[float] | None:
    """Return normalized [0,1] Zotero-similarity scores aligned to `papers`, or None.

    Embeds candidates against the user's Zotero library and rescales onto the
    fixed _ZSIM_LO.._ZSIM_HI band. Uses the SPECTER2 backend when selected, else
    the default Ollama path (compute_zotero_embedding_scores). Returns None when
    personalization is disabled, the corpus is empty, or scoring fails — the
    caller then ranks on relevance alone.
    """
    if os.environ.get("ECONSIGNALS_NO_ZOTERO"):
        return None
    try:
        corpus = load_zotero_corpus()
        if not corpus:
            return None
        cands = [
            {"title": p.get("title") or "", "abstract": p.get("abstract") or ""}
            for p in papers
        ]
        scores = _specter2_zotero_scores(cands, corpus)
        if scores is None:
            scores = compute_zotero_embedding_scores(cands, corpus)
    except Exception as exc:  # Ollama down, model missing, corpus unreadable
        print(f"[snapshot] Zotero personalization skipped: {exc}", file=sys.stderr)
        return None

    if not scores or len(scores) != len(papers) or max(scores) <= 0:
        return None

    span = _ZSIM_HI - _ZSIM_LO
    return [max(0.0, min(1.0, (s - _ZSIM_LO) / span)) for s in scores]


def _personalize(papers: list[dict]) -> tuple[list[dict], bool]:
    """Re-rank candidates by combining the available signals in percentile space.

    Three channels rank the feed: the base relevance (quality) score, Zotero
    library similarity (the strongest taste signal), and — when a model exists —
    the learned personal ranker. They are combined in per-batch PERCENTILE space
    with equal weight (relevance.combine_percentile_ranks), so no channel's raw
    scale dominates and daily cohorts stay comparable. Each paper gets `_zotero`
    (0-1 raw similarity, or None) and `_final` (the combined percentile).

    When every optional backend is off (the default), only the relevance channel
    is present; its percentile rank is a monotone transform of relevance_score,
    so the stable sort reproduces the get_top_papers order exactly. Returns
    personalized=True only when the Zotero channel contributed.
    """
    n = len(papers)
    relevance = [float(p.get("relevance_score") or 0.0) for p in papers]

    zotero = _zotero_norm_scores(papers) if papers else None
    for p, z in zip(papers, zotero or [None] * n):
        p["_zotero"] = z

    # Learned ranker is the supervised channel: it contributes ONLY when a model
    # has already been trained and persisted. train_if_missing=False keeps the
    # default feed build side-effect-free (no implicit training, no pkl write, no
    # extra Ollama pass); training is an explicit opt-in the integrator wires.
    try:
        from econsignals.lib.learned_ranker import rank_papers

        learned = rank_papers(papers, train_if_missing=False) if papers else None
    except Exception as exc:  # numpy/Ollama/model issues degrade to no channel
        print(f"[snapshot] learned ranker skipped: {exc}", file=sys.stderr)
        learned = None
    if learned is not None and len(learned) != n:
        learned = None

    combined = combine_percentile_ranks(
        {"relevance": relevance, "zotero": zotero, "learned": learned}
    )
    for p, c in zip(papers, combined or relevance):
        p["_final"] = c

    # Stable sort preserves the relevance-only (default) order, since equal
    # percentiles keep their input positions.
    papers.sort(key=lambda p: p["_final"], reverse=True)
    return papers, zotero is not None


def _zotero_seen_titles() -> set[str]:
    """Return normalized titles of the user's Zotero library, for suppression.

    A paper already in the library is something the user has seen, so it is
    dropped from the feed. The Zotero corpus carries no DOI, so suppression keys
    on normalized titles only. Returns an empty set when the library is
    unavailable, which makes suppress_seen a no-op (preserving default behavior).
    """
    from econsignals.lib.normalize import normalize_title

    try:
        corpus = load_zotero_corpus()
    except Exception as exc:  # Zotero DB locked/absent/unreadable
        print(f"[snapshot] Zotero seen-titles skipped: {exc}", file=sys.stderr)
        return set()
    keys = {normalize_title(c.get("title") or "") for c in corpus}
    return {k for k in keys if k}


def _apply_novelty_and_rationale(
    built: list[dict],
    seen_keys: set[str],
) -> list[dict]:
    """Collapse duplicates, suppress already-seen work, truncate, add rationale.

    Operates on the built display dicts (authors as list[str], single `source`),
    the shape novelty.py expects. Steps, in order:

    1. collapse_duplicates  — merge preprint/published and cross-source copies,
       attaching `also_in` (the other sources the work appeared in).
    2. suppress_seen        — drop papers whose normalized title is in seen_keys
       (the Zotero library). Empty seen_keys is a no-op.
    3. truncate to _MAX_PAPERS.
    4. attach `why_it_matters` to the kept items — None by default (no LLM
       backend), so the dashboard schema is stable either way.

    Pure with respect to the DB; the only side effect is rationale_batch's
    optional network call, which is skipped when no API key is set.
    """
    collapsed = collapse_duplicates(built)
    kept = suppress_seen(collapsed, seen_keys)[:_MAX_PAPERS]

    # why_it_matters: present on every item (None when the backend is off).
    for paper in kept:
        paper["why_it_matters"] = None
    top = kept[:_RATIONALE_TOP_N]
    rationales = rationale_batch(top)
    if rationales:
        from econsignals.lib.rationale import _paper_key

        for paper in top:
            paper["why_it_matters"] = rationales.get(_paper_key(paper))
    return kept


def build_snapshot() -> dict:
    """Assemble the dashboard snapshot dict from the current database."""
    interest_kw = load_interest_keywords()
    now = datetime.now(timezone.utc)

    # Papers: take a quality-ranked candidate pool, then re-rank by combining
    # relevance, Zotero similarity, and the learned ranker in percentile space.
    papers_raw = get_top_papers(limit=_CANDIDATE_POOL, min_score=0.0)
    papers_raw, personalized = _personalize(papers_raw)

    # Build display dicts for the WHOLE ranked pool (caption-filtered only).
    # Novelty runs on these built dicts before truncation, so collapsing
    # duplicates and suppressing already-seen work does not shrink the feed
    # below _MAX_PAPERS.
    built = []
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
        built.append({
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

    papers = _apply_novelty_and_rationale(built, _zotero_seen_titles())

    # Deadlines: dated within 270 days (grant cycles are planned months ahead)
    # + curated rolling, denoised.
    deadlines_raw = get_upcoming_deadlines(days=270, include_rolling=True)
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
            "amount": (d.get("amount") or "").strip() or None,
            "eligibility": (d.get("eligibility") or "").strip() or None,
            "relevance": round(float(d.get("relevance_score") or 0), 3),
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
