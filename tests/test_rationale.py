"""Tests for the flag-gated, grounded rationale generator.

All paths are exercised WITHOUT a network call: the no-op default, the cache
round-trip, the cache-hit short-circuit (which bypasses the API), and the
grounding content of the built request payload. The cache path is redirected to
a temp file so tests never touch data/rationale_cache.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from econsignals.lib import rationale as R


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the on-disk rationale cache to a temp file."""
    cache_path = tmp_path / "rationale_cache.json"
    monkeypatch.setattr(R, "_CACHE_PATH", cache_path)
    return cache_path


@pytest.fixture
def sample_paper() -> dict:
    return {
        "title": "Property rights, land use, and informal housing in urban India",
        "abstract": "We study how titling reforms reshape slum land markets.",
        "doi": "10.1257/AER.20231234",
    }


# ---------------------------------------------------------------------------
# Default no-op: no key -> None, no network, no cache write
# ---------------------------------------------------------------------------


def test_returns_none_without_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_cache: Path, sample_paper: dict
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert R.rationale_for(sample_paper) is None
    # No key must never reach the API or write the cache.
    assert not tmp_cache.exists()


def test_batch_returns_none_without_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_cache: Path, sample_paper: dict
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = R.rationale_batch([sample_paper])
    key = R._paper_key(sample_paper)
    assert out == {key: None}
    assert not tmp_cache.exists()


# ---------------------------------------------------------------------------
# Cache round-trip and cache-hit short-circuit (no network)
# ---------------------------------------------------------------------------


def test_cache_round_trips(tmp_cache: Path, sample_paper: dict):
    key = R._paper_key(sample_paper)
    R._save_cache({key: "Touches your land-titling and slum work."})
    assert tmp_cache.exists()
    loaded = R._load_cache()
    assert loaded[key] == "Touches your land-titling and slum work."


def test_cache_hit_skips_network(
    monkeypatch: pytest.MonkeyPatch, tmp_cache: Path, sample_paper: dict
):
    # With a key set, a pre-seeded cache entry must be returned without any call.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("ECONSIGNALS_RATIONALE", "1")

    def _boom(*_args, **_kwargs):
        raise AssertionError("network must not be called on a cache hit")

    monkeypatch.setattr(R, "_call_anthropic", _boom)

    key = R._paper_key(sample_paper)
    R._save_cache({key: "Directly overlaps your urban land-use agenda."})
    assert R.rationale_for(sample_paper) == "Directly overlaps your urban land-use agenda."


def test_empty_cached_verdict_surfaces_as_none(
    monkeypatch: pytest.MonkeyPatch, tmp_cache: Path, sample_paper: dict
):
    # An empty cached verdict (no real overlap) is surfaced as None, not "".
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("ECONSIGNALS_RATIONALE", "1")
    monkeypatch.setattr(
        R, "_call_anthropic", lambda *_a, **_k: pytest.fail("no network on cache hit")
    )
    R._save_cache({R._paper_key(sample_paper): ""})
    assert R.rationale_for(sample_paper) is None


def test_empty_model_reply_is_cached_and_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_cache: Path, sample_paper: dict
):
    # A genuine empty model reply caches "" (so it is paid for once) and -> None.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("ECONSIGNALS_RATIONALE", "1")
    monkeypatch.setattr(R, "_call_anthropic", lambda *_a, **_k: "")
    assert R.rationale_for(sample_paper, matched_terms=[]) is None
    assert R._load_cache()[R._paper_key(sample_paper)] == ""


def test_transport_failure_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_cache: Path, sample_paper: dict
):
    # A None (transport failure) must NOT be cached, so a later run can retry.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("ECONSIGNALS_RATIONALE", "1")
    monkeypatch.setattr(R, "_call_anthropic", lambda *_a, **_k: None)
    assert R.rationale_for(sample_paper, matched_terms=["land use"]) is None
    assert R._load_cache() == {}


# ---------------------------------------------------------------------------
# Request builder: grounding content is present, no network
# ---------------------------------------------------------------------------


def test_payload_includes_grounding_terms_and_neighbors(sample_paper: dict):
    payload = R.build_request_payload(
        sample_paper,
        matched_terms=["property rights", "land use", "slum"],
        neighbors=["Titling and tenure security in Indian cities"],
    )
    assert payload["model"] == "claude-haiku-4-5"
    assert payload["max_tokens"] > 0
    # System prompt carries the anti-hallucination contract.
    assert "empty string" in payload["system"]

    content = payload["messages"][0]["content"]
    assert payload["messages"][0]["role"] == "user"
    # Matched interest terms are injected for grounding.
    assert "property rights" in content
    assert "land use" in content
    assert "slum" in content
    # Neighbour titles are injected for grounding.
    assert "Titling and tenure security in Indian cities" in content
    # The paper itself is present.
    assert sample_paper["title"] in content


def test_payload_marks_absent_grounding(sample_paper: dict):
    payload = R.build_request_payload(sample_paper, matched_terms=[], neighbors=None)
    content = payload["messages"][0]["content"]
    assert "(none matched)" in content
    assert "(none)" in content


def test_matched_interest_terms_filters_to_real_overlap():
    paper = {"title": "Housing, land use, and slum upgrading", "abstract": ""}
    kw = {"housing", "land use", "slum", "air pollution", "corruption"}
    matched = R.matched_interest_terms(paper, interest_kw=kw)
    assert set(matched) == {"housing", "land use", "slum"}
    assert "air pollution" not in matched


# ---------------------------------------------------------------------------
# Response text extraction
# ---------------------------------------------------------------------------


def test_extract_text_joins_text_blocks():
    response = {
        "content": [
            {"type": "text", "text": "Overlaps your "},
            {"type": "tool_use", "id": "x"},
            {"type": "text", "text": "urban land work."},
        ]
    }
    assert R._extract_text(response) == "Overlaps your urban land work."


def test_extract_text_empty_when_no_blocks():
    assert R._extract_text({"content": []}) == ""
    assert R._extract_text({}) == ""


# ---------------------------------------------------------------------------
# Paper key: DOI preferred, deterministic fallback
# ---------------------------------------------------------------------------


def test_paper_key_prefers_doi_then_id_then_title():
    assert R._paper_key({"doi": "10.1/X", "title": "t"}) == "doi:10.1/x"
    assert R._paper_key({"source_id": "abc", "title": "t"}) == "id:abc"
    k = R._paper_key({"title": "Same Title"})
    assert k.startswith("title:")
    # Stable across calls for the same title.
    assert k == R._paper_key({"title": "Same Title"})


def test_corrupt_cache_load_is_tolerated(tmp_cache: Path):
    tmp_cache.write_text("{not valid json", encoding="utf-8")
    assert R._load_cache() == {}
    # A non-dict JSON value also yields an empty cache.
    tmp_cache.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert R._load_cache() == {}
