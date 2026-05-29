"""Unit tests for lib/novelty: preprint/published collapse and seen-suppression.

Pure-function tests, no DB and no network. Each case exercises a hard path:
the preprint+published collapse goes through the title+author route (different
DOIs), the distinct case shares authors so it guards the over-merge threshold,
and the suppression case includes a URL-form DOI to prove canonicalization.
"""

from __future__ import annotations

from econsignals.lib.novelty import (
    _normalize_doi,
    cluster_similar,
    collapse_duplicates,
    suppress_seen,
)
from econsignals.lib.normalize import normalize_title


# ---------------------------------------------------------------------------
# collapse_duplicates: preprint and published version collapse to one
# ---------------------------------------------------------------------------

def test_preprint_and_published_collapse_to_one():
    # Preprint: low-prestige source, no DOI, empty abstract, adds a subtitle.
    # Title Jaccard with the published version is ~0.78 (< 0.85), so this
    # exercises the title-Jaccard >= 0.70 + author-overlap route, NOT DOI-exact.
    preprint = {
        "title": "Land Misallocation and Agricultural Productivity in India: New Evidence",
        "authors": ["Aditi Sharma", "Rohan Gupta"],
        "doi": None,
        "abstract": "",
        "source": "openalex",
        "published_at": "2023-02",
    }
    # Published: high-prestige source, has a DIFFERENT DOI, full abstract.
    published = {
        "title": "Land Misallocation and Agricultural Productivity in India",
        "authors": ["Aditi Sharma", "Rohan Gupta"],
        "doi": "10.1257/aer.20211234",
        "abstract": "We document large misallocation of agricultural land in India.",
        "source": "nber",
        "published_at": "2023-08",
    }

    collapsed = collapse_duplicates([preprint, published])

    # Exactly one survivor, from the higher-prestige (nber) record.
    assert len(collapsed) == 1
    survivor = collapsed[0]
    assert survivor["source"] == "nber"
    assert survivor["doi"] == "10.1257/aer.20211234"

    # also_in carries the other member's source.
    assert survivor["also_in"] == ["openalex"]

    # A field absent on the canonical record is backfilled from the other:
    # the preprint's earlier date wins under merge_paper_metadata's rules.
    assert survivor["published_at"] == "2023-02"
    # The published abstract (the only non-empty one) survives.
    assert survivor["abstract"].startswith("We document")


# ---------------------------------------------------------------------------
# collapse_duplicates: distinct papers by the SAME authors do NOT merge
# ---------------------------------------------------------------------------

def test_distinct_papers_same_authors_do_not_merge():
    # Same two authors, clearly different titles (Jaccard well under 0.70).
    paper_a = {
        "title": "Property Rights and Tenancy Reform in West Bengal",
        "authors": ["Aditi Sharma", "Rohan Gupta"],
        "doi": "10.1111/aaa.0001",
        "source": "crossref",
    }
    paper_b = {
        "title": "Urban Transit Pricing and Commuting Welfare in Mumbai",
        "authors": ["Aditi Sharma", "Rohan Gupta"],
        "doi": "10.1111/bbb.0002",
        "source": "crossref",
    }

    collapsed = collapse_duplicates([paper_a, paper_b])

    # Both survive; neither absorbs the other.
    assert len(collapsed) == 2
    titles = {p["title"] for p in collapsed}
    assert titles == {paper_a["title"], paper_b["title"]}
    # Singletons get an empty also_in.
    assert all(p["also_in"] == [] for p in collapsed)


# ---------------------------------------------------------------------------
# collapse_duplicates: order preserved, distinct work between duplicates
# ---------------------------------------------------------------------------

def test_collapse_preserves_full_key_set():
    # Downstream rendering reads id/url/venue/score/topics, not just title/doi.
    rich = {
        "id": 7, "title": "Some Working Paper", "authors": ["X Author"],
        "doi": "10.1/x", "source": "nber", "url": "https://x", "venue": "AER",
        "score": 0.9, "topics": ["land"], "india": True,
    }
    dup = {"title": "Some Working Paper", "authors": ["X Author"], "source": "openalex"}

    survivor = collapse_duplicates([rich, dup])[0]

    # Every key on the canonical record survives the shallow copy, plus also_in.
    for k in ("id", "url", "venue", "score", "topics", "india"):
        assert survivor[k] == rich[k]
    assert survivor["also_in"] == ["openalex"]


def test_collapse_preserves_input_order():
    p0 = {"title": "Alpha Theory of Growth", "authors": ["X Author"], "source": "nber"}
    p1 = {"title": "Beta Models of Trade", "authors": ["Y Author"], "source": "iza"}
    p2 = {"title": "Alpha Theory of Growth", "authors": ["X Author"], "source": "openalex"}

    collapsed = collapse_duplicates([p0, p1, p2])

    # p0 and p2 collapse; the survivor stays at p0's leading position.
    assert len(collapsed) == 2
    assert collapsed[0]["title"] == "Alpha Theory of Growth"
    assert collapsed[0]["source"] == "nber"
    assert collapsed[0]["also_in"] == ["openalex"]
    assert collapsed[1]["title"] == "Beta Models of Trade"


# ---------------------------------------------------------------------------
# suppress_seen: drop by DOI and by normalized title; URL-form DOI normalizes
# ---------------------------------------------------------------------------

def test_suppress_seen_by_doi_and_title():
    seen_by_doi = {
        "title": "A Paper Already In Zotero",
        "doi": "https://doi.org/10.3386/W31705",  # URL form, mixed case
        "source": "nber",
    }
    seen_by_title = {
        "title": "Previously Surfaced Brief Item",
        "doi": None,
        "source": "openalex",
    }
    survivor = {
        "title": "A Brand New Working Paper",
        "doi": "10.9999/new",
        "source": "nber",
    }

    # seen_keys holds NORMALIZED keys: a bare lowercase DOI and a normalized title.
    seen_keys = {
        _normalize_doi("10.3386/w31705"),
        normalize_title("Previously Surfaced Brief Item"),
    }

    kept = suppress_seen([seen_by_doi, seen_by_title, survivor], seen_keys)

    assert len(kept) == 1
    assert kept[0]["title"] == "A Brand New Working Paper"


def test_suppress_seen_empty_key_does_not_suppress():
    # An empty-string key must never suppress an untitled / DOI-less paper.
    papers = [{"title": "", "doi": None, "source": "openalex"}]
    kept = suppress_seen(papers, {""})
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# cluster_similar: groups same-work papers without collapsing them
# ---------------------------------------------------------------------------

def test_cluster_similar_groups_same_work():
    p0 = {"title": "Gamma Effects in Labor Markets", "authors": ["Z Author"], "source": "nber"}
    p1 = {"title": "Gamma Effects in Labor Markets", "authors": ["Z Author"], "source": "openalex"}
    p2 = {"title": "Delta Welfare Analysis", "authors": ["Q Author"], "source": "iza"}

    clusters = cluster_similar([p0, p1, p2])

    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]
    # The two-member cluster holds both versions of the same work.
    two = next(c for c in clusters if len(c) == 2)
    assert {p["source"] for p in two} == {"nber", "openalex"}
