"""Tests for econsignals.lib.zotero_embeddings.

Unit tests use mocked Ollama. Integration tests require Ollama running
and your Zotero database.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from econsignals.lib.zotero_embeddings import (
    _corpus_hash,
    _cosine_similarity,
    _embed_batch,
    _load_corpus_cache,
    _ollama_embed,
    _save_corpus_cache,
    compute_zotero_embedding_scores,
)

# Skip integration tests if Ollama unreachable
_OLLAMA_AVAILABLE = False
try:
    import urllib.request

    req = urllib.request.Request(
        f"{os.environ.get('OLLAMA_HOST', 'http://localhost:11434')}/api/tags",
        method="GET",
    )
    urllib.request.urlopen(req, timeout=2)
    _OLLAMA_AVAILABLE = True
except Exception:
    pass

# Skip if Zotero DB missing
from econsignals.lib.zotero_profile import load_zotero_corpus, zotero_db_path

_ZOTERO_AVAILABLE = zotero_db_path() is not None


class TestCorpusCache:
    """Tests for corpus embedding cache."""

    def test_corpus_hash_deterministic(self):
        texts = ["Paper A about housing.", "Paper B about development."]
        h1 = _corpus_hash(texts)
        h2 = _corpus_hash(texts)
        assert h1 == h2

    def test_corpus_hash_changes_with_content(self):
        h1 = _corpus_hash(["Paper A.", "Paper B."])
        h2 = _corpus_hash(["Paper A.", "Paper B.", "Paper C."])
        h3 = _corpus_hash(["Paper A.", "Paper B edited."])
        assert h1 != h2
        assert h1 != h3

    def test_cache_save_and_load(self, tmp_path):
        texts = ["Economics and policy."]
        h = _corpus_hash(texts)
        emb = [[0.1, 0.2, 0.3]]  # fake embedding

        with patch("econsignals.lib.zotero_embeddings._CACHE_PATH", tmp_path / "cache.pkl"):
            _save_corpus_cache(h, "nomic-embed-text", emb)
            loaded = _load_corpus_cache(h, "nomic-embed-text")
        assert loaded == emb

    def test_cache_miss_on_hash_mismatch(self, tmp_path):
        with patch("econsignals.lib.zotero_embeddings._CACHE_PATH", tmp_path / "cache.pkl"):
            _save_corpus_cache("abc123", "nomic-embed-text", [[1.0, 2.0]])
            loaded = _load_corpus_cache("different_hash", "nomic-embed-text")
        assert loaded is None


class TestCosineSimilarity:
    """Tests for _cosine_similarity (pure math, no Ollama)."""

    def test_identical_vectors_similarity_one(self):

        v = [[1.0, 0.0, 0.0]]
        sim = _cosine_similarity(v, v)
        assert sim.shape == (1, 1)
        assert abs(float(sim[0, 0]) - 1.0) < 1e-6

    def test_orthogonal_vectors_similarity_zero(self):
        v1 = [[1.0, 0.0, 0.0]]
        v2 = [[0.0, 1.0, 0.0]]
        sim = _cosine_similarity(v1, v2)
        assert abs(float(sim[0, 0])) < 1e-6

    def test_matrix_shape(self):
        a = [[1.0, 0.0], [0.0, 1.0]]  # 2 vectors
        b = [[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]]  # 3 vectors
        sim = _cosine_similarity(a, b)
        assert sim.shape == (2, 3)


class TestComputeZoteroEmbeddingScoresUnit:
    """Unit tests with mocked Ollama."""

    def test_empty_corpus_returns_zeros(self):
        candidates = [{"title": "Test", "abstract": "Abstract"}]
        scores = compute_zotero_embedding_scores(candidates, [])
        assert scores == [0.0]

    def test_empty_candidates_returns_empty(self):
        corpus = [{"text": "Some paper about economics and development policy."}]
        scores = compute_zotero_embedding_scores([], corpus)
        assert scores == []

    def test_similar_text_scores_higher_than_unrelated(self):
        """Fake embedder with keyword overlap: similar topic scores higher than unrelated."""
        corpus = [
            {"text": "Urban housing supply and land use regulation in developing countries."},
            {"text": "Random unrelated topic about cooking recipes and gardening."},
        ]
        similar = [{"title": "Housing", "abstract": "Land use regulation and urban development."}]
        unrelated = [{"title": "Gardening", "abstract": "How to grow tomatoes in your backyard."}]

        with patch(
            "econsignals.lib.zotero_embeddings._embed_batch",
            side_effect=lambda texts, model: _fake_embeddings_keyword_overlap(texts),
        ):
            scores_similar = compute_zotero_embedding_scores(similar, corpus)
            scores_unrelated = compute_zotero_embedding_scores(unrelated, corpus)

        assert len(scores_similar) == 1
        assert len(scores_unrelated) == 1
        assert scores_similar[0] > scores_unrelated[0]

    def test_identical_text_scores_highest(self):
        """Exact corpus match should get max similarity to that item."""
        text = "Development economics and randomized controlled trials in India."
        corpus = [{"text": text}]
        candidate = [{"title": "", "abstract": text}]

        with patch(
            "econsignals.lib.zotero_embeddings._embed_batch",
            side_effect=lambda texts, model: _fake_embeddings_keyword_overlap(texts),
        ):
            scores = compute_zotero_embedding_scores(candidate, corpus)

        assert len(scores) == 1
        # Identical text → identical embedding → cosine sim = 1 → score = 10
        assert scores[0] > 9.0

    def test_scale_applied(self):
        """Custom scale multiplies the score."""
        corpus = [{"text": "Economics and development policy in urban areas."}]
        candidate = [{"title": "Economics", "abstract": "Development policy urban."}]

        with patch(
            "econsignals.lib.zotero_embeddings._embed_batch",
            side_effect=lambda texts, model: _fake_embeddings_keyword_overlap(texts),
        ):
            s10 = compute_zotero_embedding_scores(candidate, corpus, scale=10.0)
            s100 = compute_zotero_embedding_scores(candidate, corpus, scale=100.0)

        assert s100[0] == pytest.approx(10 * s10[0], rel=0.01)


def _fake_embeddings_keyword_overlap(texts: list[str]) -> list[list[float]]:
    """Fake embeddings: bag-of-words over a small vocab. Overlapping words → similar vectors."""
    import re

    import numpy as np

    vocab = [
        "urban", "housing", "land", "regulation", "development", "economics",
        "policy", "india", "gardening", "tomatoes", "cooking", "recipes",
        "random", "unrelated",
    ]
    word_to_idx = {w: i for i, w in enumerate(vocab)}
    dim = len(vocab)

    def text_to_vec(t: str) -> list[float]:
        words = re.findall(r"[a-z]+", t.lower())
        vec = np.zeros(dim, dtype=np.float64)
        for w in words:
            if w in word_to_idx:
                vec[word_to_idx[w]] += 1.0
        n = np.linalg.norm(vec)
        if n < 1e-12:
            vec[0] = 1.0
        else:
            vec = vec / n
        return vec.tolist()

    return [text_to_vec(t) for t in texts]


@pytest.mark.skipif(not _OLLAMA_AVAILABLE, reason="Ollama not running")
class TestOllamaIntegration:
    """Integration tests with real Ollama (no Zotero required)."""

    def test_embed_batch_returns_vectors(self):
        """_embed_batch returns one embedding per input text via Ollama."""
        texts = [
            "Development economics and poverty reduction in India.",
            "Land use regulation and housing supply in cities.",
        ]
        result = _embed_batch(texts, "nomic-embed-text")
        assert len(result) == len(texts)
        for emb in result:
            assert isinstance(emb, list)
            assert len(emb) > 100
            assert all(isinstance(x, (int, float)) for x in emb[:5])

    def test_compute_scores_with_synthetic_corpus(self):
        """Full pipeline: synthetic corpus, real Ollama, non-zero scores."""
        corpus = [
            {"text": "Urban housing supply and land use regulation in developing countries."},
            {"text": "Randomized controlled trials in development economics."},
        ]
        candidates = [
            {"title": "Housing Policy", "abstract": "Land use and urban development in India."},
        ]
        scores = compute_zotero_embedding_scores(candidates, corpus)
        assert len(scores) == 1
        assert scores[0] > 0
        assert scores[0] < 20.0  # scale=10, max sim ~1 → reasonable upper bound

    def test_compute_scores_similar_higher_than_unrelated(self):
        """Similar candidate scores higher than unrelated via real embeddings."""
        corpus = [
            {"text": "Machine learning for causal inference in economics."},
            {"text": "Regression discontinuity designs and policy evaluation."},
        ]
        similar = [{"title": "Causal ML", "abstract": "Machine learning methods for treatment effects."}]
        unrelated = [{"title": "Gardening Tips", "abstract": "How to grow tomatoes and herbs."}]
        s_sim = compute_zotero_embedding_scores(similar, corpus)
        s_unr = compute_zotero_embedding_scores(unrelated, corpus)
        assert s_sim[0] > s_unr[0], f"Similar ({s_sim[0]:.3f}) should exceed unrelated ({s_unr[0]:.3f})"

    def test_cache_used_on_second_run(self, tmp_path):
        """Corpus embeddings are cached; second run uses cache (no extra Ollama calls)."""
        corpus = [{"text": "Test corpus for cache verification."}]
        candidates = [{"title": "Test", "abstract": "Cache test."}]

        with patch("econsignals.lib.zotero_embeddings._CACHE_PATH", tmp_path / "zotero_cache.pkl"):
            scores1 = compute_zotero_embedding_scores(candidates, corpus)
            scores2 = compute_zotero_embedding_scores(candidates, corpus)
        assert scores1[0] > 0
        assert scores2[0] == pytest.approx(scores1[0], rel=0.01)


@pytest.mark.skipif(not _OLLAMA_AVAILABLE, reason="Ollama not running")
@pytest.mark.skipif(not _ZOTERO_AVAILABLE, reason="Zotero DB not found")
class TestOllamaWithRealZotero:
    """Integration tests: Ollama + your actual Zotero database."""

    def test_ollama_returns_embeddings(self):
        """Ollama /api/embeddings returns a vector."""
        emb = _ollama_embed("Development economics and poverty reduction.", "nomic-embed-text")
        assert emb is not None
        assert isinstance(emb, list)
        assert len(emb) > 100  # nomic-embed-text is 768-dim
        assert all(isinstance(x, (int, float)) for x in emb[:5])

    def test_similar_to_zotero_scores_higher_than_unrelated(self):
        """A paper similar to your Zotero library scores higher than random text."""
        corpus = load_zotero_corpus(max_items=50)
        if len(corpus) < 5:
            pytest.skip("Zotero corpus too small")

        # Candidate similar to economics/development (likely in your library)
        similar = [
            {
                "title": "Land Use Regulation and Housing Affordability",
                "abstract": "We study the impact of zoning on housing supply in Indian cities.",
            }
        ]
        # Unrelated candidate
        unrelated = [
            {
                "title": "Quantum Entanglement in Photonic Systems",
                "abstract": "We present a novel approach to quantum computing using superconducting qubits.",
            }
        ]

        scores_similar = compute_zotero_embedding_scores(similar, corpus)
        scores_unrelated = compute_zotero_embedding_scores(unrelated, corpus)

        assert len(scores_similar) == 1
        assert len(scores_unrelated) == 1
        assert scores_similar[0] > scores_unrelated[0], (
            f"Similar paper ({scores_similar[0]:.3f}) should score higher than "
            f"unrelated ({scores_unrelated[0]:.3f})"
        )

    def test_exact_corpus_match_scores_high(self):
        """Using text from your Zotero as candidate should score very high."""
        corpus = load_zotero_corpus(max_items=20)
        if not corpus:
            pytest.skip("Zotero corpus empty")

        # Use first corpus item's text as candidate (should be near-perfect match)
        first = corpus[0]
        candidate = [{"title": first["title"], "abstract": first.get("abstract", "")}]

        scores = compute_zotero_embedding_scores(candidate, corpus)
        assert len(scores) == 1
        assert scores[0] > 5.0, f"Exact match should score high, got {scores[0]:.3f}"

    def test_recency_affects_ranking(self):
        """Newer corpus items contribute more (by design). Same similarity, different order → different score."""
        # Use a small corpus; swap order of two items and verify score changes
        corpus = load_zotero_corpus(max_items=10)
        if len(corpus) < 2:
            pytest.skip("Need at least 2 corpus items")

        candidate = [{"title": corpus[0]["title"], "abstract": corpus[0].get("abstract", "")}]
        scores_orig = compute_zotero_embedding_scores(candidate, corpus)

        # Reverse corpus (oldest first) — same items, different weights
        corpus_reversed = list(reversed(corpus))
        scores_rev = compute_zotero_embedding_scores(candidate, corpus_reversed)

        # When candidate matches first item: orig has it weighted high, rev has it weighted low
        assert scores_orig[0] != scores_rev[0]
