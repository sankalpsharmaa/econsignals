"""Parse-only tests for the Semantic Scholar recommendations sensor.

No network: every test feeds the pure helpers an embedded JSON sample captured
from the live POST endpoint
(https://api.semanticscholar.org/recommendations/v1/papers/) so the
record-shape mapping is pinned without hitting the API.
"""

from __future__ import annotations

import json

from econsignals.sensors.semantic_scholar import (
    _infer_paper_type,
    _parse_recommendations,
    _seed_ids_from_papers,
    _year_to_iso,
)

# A captured response body shape from the live POST endpoint, trimmed to a few
# records that exercise the edge cases: a record with a DOI, one with only a
# CorpusId (no DOI), one missing paperId (must skip), one missing title (must
# skip), and one with a null abstract.
_SAMPLE = json.dumps(
    {
        "recommendedPapers": [
            {
                "paperId": "d28cdae0f2f67ed9302766047e9a26aa6bc9b9e0",
                "externalIds": {"DOI": "10.1257/aer.20231234", "CorpusId": 1},
                "url": "https://www.semanticscholar.org/paper/d28cdae0",
                "title": "Globalization and the China Shock: A Reassessment",
                "year": 2026,
                "authors": [
                    {"authorId": "111", "name": "Jane Doe"},
                    {"authorId": "222", "name": "John Smith"},
                ],
                "abstract": "We reassess the labor-market effects of trade.",
            },
            {
                "paperId": "110c20a5d1d74070918e81eb530fc7565eb8c7c0",
                "externalIds": {"CorpusId": 287115611},
                "url": "https://www.semanticscholar.org/paper/110c20a5",
                "title": "Journal of Development Economics",
                "year": None,
                "authors": [],
                "abstract": None,
            },
            {
                "paperId": "",
                "externalIds": {"DOI": "10.0000/skipme"},
                "title": "No paperId should be dropped",
                "year": 2025,
                "authors": [],
                "abstract": "x",
            },
            {
                "paperId": "aaabbbcccddd",
                "externalIds": {"DOI": "10.0000/notitle"},
                "title": "   ",
                "year": 2024,
                "authors": [{"authorId": "9", "name": "  "}],
                "abstract": "y",
            },
        ]
    }
)


# ---------------------------------------------------------------------------
# Year -> ISO mapping
# ---------------------------------------------------------------------------

def test_year_maps_to_jan_first():
    assert _year_to_iso(2026) == "2026-01-01"
    assert _year_to_iso("1999") == "1999-01-01"


def test_year_none_or_implausible_is_none():
    assert _year_to_iso(None) is None
    assert _year_to_iso("not-a-year") is None
    assert _year_to_iso(1200) is None
    assert _year_to_iso(2200) is None


# ---------------------------------------------------------------------------
# Recommendation parsing
# ---------------------------------------------------------------------------

def test_parse_keeps_only_usable_records():
    papers = _parse_recommendations(json.loads(_SAMPLE))
    # Four input records: one has no paperId, one has a blank title -> 2 kept.
    assert len(papers) == 2
    ids = {p["source_id"] for p in papers}
    assert "" not in ids
    assert "aaabbbcccddd" not in ids


def test_parse_maps_first_record_fields():
    papers = _parse_recommendations(json.loads(_SAMPLE))
    first = next(
        p for p in papers if p["source_id"] == "d28cdae0f2f67ed9302766047e9a26aa6bc9b9e0"
    )
    assert first["title"].startswith("Globalization and the China Shock")
    assert first["doi"] == "10.1257/aer.20231234"
    assert first["published_at"] == "2026-01-01"
    assert first["authors"] == ["Jane Doe", "John Smith"]
    assert first["paper_type"] == "journal_article"
    assert first["abstract"] == "We reassess the labor-market effects of trade."
    # source_id is the S2 paperId, used for cross-run dedup.
    assert first["source_id"] == "d28cdae0f2f67ed9302766047e9a26aa6bc9b9e0"
    assert first["raw_metadata"]["source"] == "semantic_scholar"


def test_parse_handles_missing_doi_and_null_abstract():
    papers = _parse_recommendations(json.loads(_SAMPLE))
    only_corpus = next(
        p for p in papers if p["source_id"] == "110c20a5d1d74070918e81eb530fc7565eb8c7c0"
    )
    # externalIds had only a CorpusId -> doi is None, not a crash.
    assert only_corpus["doi"] is None
    assert only_corpus["abstract"] is None
    assert only_corpus["published_at"] is None
    assert only_corpus["authors"] == []


def test_parse_empty_payload_returns_empty():
    assert _parse_recommendations({}) == []
    assert _parse_recommendations({"recommendedPapers": []}) == []
    assert _parse_recommendations({"recommendedPapers": None}) == []


# ---------------------------------------------------------------------------
# paper_type inference
# ---------------------------------------------------------------------------

def test_paper_type_arxiv_without_doi_is_working_paper():
    assert _infer_paper_type({"ArXiv": "2604.15825"}) == "working_paper"


def test_paper_type_with_doi_is_journal_article():
    assert _infer_paper_type({"DOI": "10.1/x", "ArXiv": "2604.1"}) == "journal_article"
    assert _infer_paper_type({"DOI": "10.1/x"}) == "journal_article"
    assert _infer_paper_type({"CorpusId": 1}) == "journal_article"
    assert _infer_paper_type({}) == "journal_article"


# ---------------------------------------------------------------------------
# Seed building
# ---------------------------------------------------------------------------

def test_seed_ids_prefix_dedup_and_cap():
    papers = [
        {"doi": "10.1/a"},
        {"doi": "10.1/b"},
        {"doi": "10.1/a"},  # duplicate
        {"doi": None},      # no doi
        {"doi": "  "},      # blank
        {"doi": "10.1/c"},
    ]
    seeds = _seed_ids_from_papers(papers, max_seeds=2)
    # Capped at 2, prefixed, order preserved, duplicate dropped.
    assert seeds == ["DOI:10.1/a", "DOI:10.1/b"]


def test_seed_ids_empty_when_no_dois():
    assert _seed_ids_from_papers([{"doi": None}, {"title": "x"}], max_seeds=10) == []
    assert _seed_ids_from_papers([], max_seeds=10) == []
