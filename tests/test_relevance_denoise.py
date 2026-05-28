"""Regression tests for the relevance-scoring and denoising fixes.

The audit found zero direct tests for the scorer and the social/deadline
filters — the exact code that made the tool untrustworthy. These pin the
behaviour of the pure-function paths (no DB needed).
"""

from __future__ import annotations

from econsignals.lib import relevance as R
from econsignals.lib import snapshot as S
from econsignals.lib.normalize import _strip_accents, normalize_author_name


# ---------------------------------------------------------------------------
# Keyword tiering: distinctive topics outweigh broad ones
# ---------------------------------------------------------------------------

def test_core_topics_outscore_secondary():
    kw = {"housing", "land use", "slum", "health", "education", "inequality"}
    core = R.score_keywords("Slum housing and land use reform", "", kw)
    secondary = R.score_keywords("Health, education and inequality outcomes", "", kw)
    assert core == 1.0  # three core topics saturate
    assert secondary < core  # three secondary topics do not
    assert 0.0 < secondary <= 0.75


def test_keyword_phrase_not_shattered_into_generic_words():
    kw = {"public goods", "land use"}
    # "public markets" must NOT match the phrase "public goods"
    assert R.score_keywords("Public markets and finance", "", kw) == 0.0


# ---------------------------------------------------------------------------
# Recency: future dates are neutral, not freshest
# ---------------------------------------------------------------------------

def test_future_date_is_neutral_not_fresh():
    assert R.score_recency("2099-01-01") == 0.5  # forthcoming-issue stamp
    assert R.score_recency("1990-01-01") == 0.0  # ancient
    assert R.score_recency(None) == 0.5


# ---------------------------------------------------------------------------
# Accent handling: non-decomposable letters survive
# ---------------------------------------------------------------------------

def test_strip_accents_transliterates():
    assert _strip_accents("Tyskø") == "Tysko"
    assert _strip_accents("Łódź") == "Lodz"
    assert "tysko" in normalize_author_name("Erna Tyskø")


# ---------------------------------------------------------------------------
# Caption detection: Exa AI image-captions are recognised
# ---------------------------------------------------------------------------

def test_caption_detection():
    assert S._is_caption("The image prominently displays a journal cover")
    assert S._is_caption("The data visualization shows a contrast")
    assert S._is_caption("The tweet announces a call for papers")
    assert not S._is_caption("New experimental evidence on rural water in India")
    assert not S._is_caption("")


# ---------------------------------------------------------------------------
# Social denoising: econ-only
# ---------------------------------------------------------------------------

def test_econ_social_filter():
    kw = R.load_interest_keywords()
    assert S.is_econ_social({"content": "linked", "paper_id": 5}, kw)
    assert S.is_econ_social({"content": "see https://doi.org/10.1257/aer.123"}, kw)
    assert S.is_econ_social(
        {"content": "thread on jobs", "author_handle": "@x@econtwitter.net"}, kw
    )
    assert S.is_econ_social({"content": "New work on slum housing reform"}, kw)
    # firehose noise: no paper, no DOI, no econ handle, no core topic
    assert not S.is_econ_social(
        {"content": "How to care for a PICC line", "author_handle": "@wikihow@x"}, kw
    )
    assert not S.is_econ_social(
        {"content": "The image shows a scenic landscape"}, kw
    )


# ---------------------------------------------------------------------------
# Deadline denoising: drop Twitter caption junk, keep curated/dated
# ---------------------------------------------------------------------------

def test_deadline_filter():
    assert S._clean_deadline(
        {"name": "PEDL Exploratory Grants", "organization": "CEPR/PEDL", "deadline_date": ""}
    )
    assert S._clean_deadline(
        {"name": "NEUDC 2026", "organization": "NEUDC", "deadline_date": "2026-07-15"}
    )
    # rolling with Twitter/X org -> junk
    assert not S._clean_deadline(
        {"name": "The image highlights a grant", "organization": "Twitter/X", "deadline_date": ""}
    )
    # rolling with no organization -> junk
    assert not S._clean_deadline(
        {"name": "Some opportunity", "organization": None, "deadline_date": ""}
    )
