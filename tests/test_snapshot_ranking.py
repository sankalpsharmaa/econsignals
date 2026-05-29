"""Tests for the percentile rank-space feed combination and novelty wiring.

Covers ITEM #7: per-batch percentile combination of relevance / Zotero / learned
channels, novelty duplicate collapse on built feed dicts, and the rationale
field defaulting to None when no LLM backend is configured. All pure-function,
no network and no live DB (the snapshot helpers under test take lists in and
return lists out).
"""

from __future__ import annotations

import pytest

from econsignals.lib import relevance as R
from econsignals.lib import snapshot as S


@pytest.fixture(autouse=True)
def _no_llm_backend(monkeypatch):
    """Keep every test in this file network-free.

    rationale_batch only calls the Anthropic API when ANTHROPIC_API_KEY is set;
    clearing it for all tests means even the collapse/suppress cases that route
    through _apply_novelty_and_rationale never attempt a network call, whatever
    the runner's environment holds.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Percentile combination: scale-free, equal-weight, order-sensible
# ---------------------------------------------------------------------------

def test_percentile_combination_is_scale_free_across_channels():
    # Channel A and B disagree on scale wildly but agree on the WINNER.
    # In raw-sum space the large-magnitude channel would dominate; in rank space
    # each channel contributes equally, so the jointly-best paper wins.
    relevance = [0.10, 0.20, 0.90]          # paper 2 best, small spread
    learned = [1000.0, 2000.0, 9000.0]      # paper 2 best, huge magnitude
    combined = R.combine_percentile_ranks({"relevance": relevance, "learned": learned})
    assert combined[2] == max(combined)     # agreed winner ranks top
    # Magnitude does not leak in: result is a mean of two percentile vectors,
    # both bounded in (0, 1], so no entry exceeds 1.0.
    assert all(0.0 < c <= 1.0 for c in combined)


def test_single_channel_is_monotone_in_raw_score():
    # With one channel present, the combined score is a monotone transform of the
    # raw score: argsort is preserved. This is what keeps the default feed order
    # identical to get_top_papers.
    relevance = [0.3, 0.9, 0.1, 0.6]
    combined = R.combine_percentile_ranks({"relevance": relevance})
    raw_order = sorted(range(4), key=lambda i: relevance[i])
    comb_order = sorted(range(4), key=lambda i: combined[i])
    assert raw_order == comb_order


def test_absent_channel_does_not_shift_ranking():
    relevance = [0.2, 0.8, 0.5]
    with_none = R.combine_percentile_ranks({"relevance": relevance, "learned": None})
    alone = R.combine_percentile_ranks({"relevance": relevance})
    assert with_none == alone


def test_ties_get_equal_percentile():
    # Equal raw values must receive equal percentiles, or a stable sort could not
    # preserve input order on ties.
    combined = R.combine_percentile_ranks({"relevance": [0.5, 0.5, 0.9]})
    assert combined[0] == combined[1]
    assert combined[2] > combined[0]


# ---------------------------------------------------------------------------
# _personalize: default (relevance-only) order is preserved
# ---------------------------------------------------------------------------

def test_personalize_preserves_relevance_order_when_backends_off(monkeypatch):
    # ECONSIGNALS_NO_ZOTERO disables the Zotero channel; no learned model exists
    # in the test env, so only the relevance channel is present. The stable sort
    # must reproduce the get_top_papers (relevance DESC) order exactly.
    monkeypatch.setenv("ECONSIGNALS_NO_ZOTERO", "1")
    pool = [
        {"id": 1, "relevance_score": 0.9, "title": "A"},
        {"id": 2, "relevance_score": 0.7, "title": "B"},
        {"id": 3, "relevance_score": 0.7, "title": "C"},  # tie with B, after it
        {"id": 4, "relevance_score": 0.3, "title": "D"},
    ]
    out, personalized = S._personalize([dict(p) for p in pool])
    assert [p["id"] for p in out] == [1, 2, 3, 4]
    assert personalized is False
    assert all(p["_zotero"] is None for p in out)
    # _final is monotone non-increasing down the sorted list.
    finals = [p["_final"] for p in out]
    assert finals == sorted(finals, reverse=True)


def test_personalize_calls_learned_ranker_without_training(monkeypatch):
    # The default feed build must never train a model as a side effect; it may
    # only use one that already exists. Assert _personalize passes
    # train_if_missing=False to the learned ranker.
    monkeypatch.setenv("ECONSIGNALS_NO_ZOTERO", "1")
    captured = {}

    def fake_rank(candidates, *, train_if_missing=True, **kw):
        captured["train_if_missing"] = train_if_missing
        return None

    monkeypatch.setattr("econsignals.lib.learned_ranker.rank_papers", fake_rank)
    S._personalize([{"id": 1, "relevance_score": 0.5, "title": "A"}])
    assert captured["train_if_missing"] is False


def test_personalize_learned_channel_reorders_batch(monkeypatch):
    # When a learned model exists, its channel enters the equal-weight percentile
    # combination and can outrank the relevance order. Here the learned ranker
    # strongly prefers the relevance-worst paper, which then climbs.
    monkeypatch.setenv("ECONSIGNALS_NO_ZOTERO", "1")
    pool = [
        {"id": 1, "relevance_score": 0.9, "title": "A"},
        {"id": 2, "relevance_score": 0.6, "title": "B"},
        {"id": 3, "relevance_score": 0.3, "title": "C"},
    ]
    # Learned ranker rates paper 3 best and paper 2 worst. Relevance percentiles
    # are [1.0, 0.667, 0.333] for papers [1, 2, 3]; learned percentiles are
    # [0.667, 0.333, 1.0]; the equal-weight means are 0.833, 0.5, 0.667. So the
    # learned channel lifts paper 3 above paper 2 even though paper 2 has the
    # higher relevance score.
    learned = {1: 0.5, 2: 0.0, 3: 1.0}
    monkeypatch.setattr(
        "econsignals.lib.learned_ranker.rank_papers",
        lambda candidates, **kw: [learned[c["id"]] for c in candidates],
    )
    out, _ = S._personalize([dict(p) for p in pool])
    assert [p["id"] for p in out] == [1, 3, 2]
    finals = {p["id"]: p["_final"] for p in out}
    assert finals[3] > finals[2]  # learned channel overrode relevance order


# ---------------------------------------------------------------------------
# Novelty: a preprint/published duplicate collapses; order preserved
# ---------------------------------------------------------------------------

def _built(idx: int, **kw) -> dict:
    """Minimal built-feed dict (the shape build_snapshot produces)."""
    base = {
        "id": idx,
        "title": kw.get("title", f"Paper {idx}"),
        "authors": kw.get("authors", []),
        "abstract": kw.get("abstract", ""),
        "doi": kw.get("doi"),
        "source": kw.get("source"),
        "venue": None,
        "published_at": kw.get("published_at"),
        "score": kw.get("score", 0.5),
        "zotero": None,
        "jel": [],
        "topics": [],
        "india": False,
    }
    return base


def test_novelty_collapses_preprint_and_published():
    # Same title + overlapping authors, different sources (NBER preprint vs the
    # published journal version). Should collapse to ONE record, the higher-
    # prestige source kept, with the other source noted in also_in.
    title = "Land Misallocation and Agricultural Productivity in India"
    authors = ["Asha Rao", "Vikram Mehta"]
    built = [
        _built(1, title=title, authors=authors, source="nber",
               doi="10.3386/w99999", abstract="A long abstract " * 5),
        _built(2, title=title, authors=authors, source="openalex",
               doi="10.1016/j.jdeveco.2026.1", abstract="short"),
        _built(3, title="Unrelated Urban Zoning Paper", authors=["Other Person"],
               source="crossref"),
    ]
    out = S._apply_novelty_and_rationale(built, seen_keys=set())
    titles = [p["title"] for p in out]
    assert titles.count(title) == 1          # duplicate collapsed
    assert len(out) == 2                      # the unrelated paper survives
    survivor = next(p for p in out if p["title"] == title)
    assert survivor["source"] == "nber"      # higher-prestige canonical kept
    assert "openalex" in survivor.get("also_in", [])
    # Input order is preserved: the collapsed work sits at its first position.
    assert titles[0] == title


def test_novelty_suppresses_seen_zotero_title():
    from econsignals.lib.normalize import normalize_title

    seen_title = "A Paper Already In My Zotero Library"
    built = [
        _built(1, title=seen_title),
        _built(2, title="A Fresh Unseen Paper"),
    ]
    seen = {normalize_title(seen_title)}
    out = S._apply_novelty_and_rationale(built, seen_keys=seen)
    assert [p["title"] for p in out] == ["A Fresh Unseen Paper"]


# ---------------------------------------------------------------------------
# Rationale: why_it_matters is None by default (no LLM backend)
# ---------------------------------------------------------------------------

def test_rationale_field_is_none_by_default(monkeypatch):
    # With no ANTHROPIC_API_KEY, rationale_batch is a no-op, so every item carries
    # why_it_matters == None and the key is always present (stable schema).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    built = [_built(1, title="X"), _built(2, title="Y")]
    out = S._apply_novelty_and_rationale(built, seen_keys=set())
    assert len(out) == 2
    for p in out:
        assert "why_it_matters" in p
        assert p["why_it_matters"] is None
