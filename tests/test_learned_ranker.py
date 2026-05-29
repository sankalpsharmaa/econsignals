"""Unit tests for the learned personal ranker.

No network: Ollama embedding calls are monkeypatched to deterministic synthetic
vectors, so the tests exercise the training/scoring/persistence logic and the
degradation contract without touching Ollama, the DB, or Zotero.

Two clusters in embedding space stand in for "papers like my library / thumbs-up"
(positives) and "papers I ignore / thumbs-down" (negatives). A correct ranker
must score held-out positives above held-out negatives.
"""

from __future__ import annotations

import numpy as np
import pytest

from econsignals.lib import learned_ranker as LR


# ---------------------------------------------------------------------------
# Synthetic embedding fixtures
# ---------------------------------------------------------------------------

_DIM = 16


def _cluster(center_axis: int, n: int, *, dim: int = _DIM, seed: int = 0) -> np.ndarray:
    """Return `n` noisy unit-ish vectors centred on one coordinate axis.

    Positives sit near axis 0, negatives near axis 1, so the two classes are
    linearly separable but not identical.
    """
    rng = np.random.default_rng(seed)
    base = np.zeros(dim)
    base[center_axis] = 1.0
    pts = base + 0.15 * rng.standard_normal((n, dim))
    return pts


def _papers(prefix: str, n: int) -> list[dict]:
    """Build `n` candidate dicts with distinct titles (text is unused: embeds
    are monkeypatched by index, so order is what matters)."""
    return [{"title": f"{prefix} paper {i}", "abstract": ""} for i in range(n)]


@pytest.fixture
def synthetic_signal(monkeypatch):
    """Wire the ranker's embedding/corpus/feedback inputs to synthetic clusters.

    Positives (Zotero corpus): cluster on axis 0.
    Negatives (random candidates): cluster on axis 1.
    Candidate texts are embedded by mapping their title prefix to the matching
    cluster, so held-out positives/negatives land where the model expects.
    """
    pos_corpus = _cluster(0, 20, seed=1)

    # Zotero corpus embeddings = positive cluster.
    monkeypatch.setattr(LR, "_corpus_embeddings", lambda model_name: pos_corpus)
    # No feedback in this base fixture.
    monkeypatch.setattr(LR, "_feedback_texts", lambda: ([], []))

    # Embed candidate texts: prefix "POS" -> axis-0 cluster, else axis-1 cluster.
    def fake_embed(texts: list[str], model_name: str):
        if not texts:
            return None
        rng = np.random.default_rng(99)
        out = np.zeros((len(texts), _DIM))
        for i, t in enumerate(texts):
            axis = 0 if str(t).startswith("POS") else 1
            base = np.zeros(_DIM)
            base[axis] = 1.0
            out[i] = base + 0.12 * rng.standard_normal(_DIM)
        return out

    monkeypatch.setattr(LR, "_embed_texts", fake_embed)
    return pos_corpus


# ---------------------------------------------------------------------------
# Core: a trained ranker separates positives from negatives
# ---------------------------------------------------------------------------


def test_trained_ranker_ranks_positives_above_negatives(synthetic_signal, tmp_path, monkeypatch):
    """Held-out positives must outscore held-out negatives after training."""
    monkeypatch.setattr(LR, "_MODEL_PATH", tmp_path / "ranker.pkl")

    # Train: positives from Zotero corpus (fixture); negatives from candidate
    # pool (prefix "NEG" -> axis-1 cluster via the fake embedder).
    train_candidates = _papers("NEG", 20)
    model = LR.train_ranker(train_candidates, persist=True)
    assert model is not None
    assert model.n_pos >= LR._MIN_PER_CLASS
    assert model.n_neg >= LR._MIN_PER_CLASS

    # Held-out evaluation set, disjoint titles from training.
    held_out = _papers("POS_eval", 8) + _papers("NEG_eval", 8)
    scores = LR.rank_papers(held_out, model=model)
    assert scores is not None
    assert len(scores) == len(held_out)
    assert all(0.0 <= s <= 1.0 for s in scores)

    pos_scores = scores[:8]
    neg_scores = scores[8:]
    assert min(pos_scores) > max(neg_scores), (
        f"positives {pos_scores} should all beat negatives {neg_scores}"
    )


def test_centroid_fallback_separates_without_sklearn(synthetic_signal, tmp_path, monkeypatch):
    """When sklearn is unavailable, the numpy centroid model still separates."""
    monkeypatch.setattr(LR, "_MODEL_PATH", tmp_path / "ranker.pkl")
    # Force the sklearn branch to report "not installed".
    monkeypatch.setattr(LR, "_fit_logistic", lambda X, y: None)

    model = LR.train_ranker(_papers("NEG", 20), persist=False)
    assert model is not None
    assert model.kind == "centroid"
    assert model.pos_centroid is not None and model.neg_centroid is not None

    held_out = _papers("POS_eval", 6) + _papers("NEG_eval", 6)
    scores = LR.rank_papers(held_out, model=model)
    assert scores is not None
    assert min(scores[:6]) > max(scores[6:])


def test_feedback_thumbs_up_down_feed_training(tmp_path, monkeypatch):
    """Thumbs-up rows add positives and thumbs-down rows add negatives."""
    monkeypatch.setattr(LR, "_MODEL_PATH", tmp_path / "ranker.pkl")
    # No Zotero corpus: positives must come entirely from thumbs-up feedback.
    monkeypatch.setattr(LR, "_corpus_embeddings", lambda model_name: None)
    monkeypatch.setattr(
        LR,
        "_feedback_texts",
        lambda: (["POS up a", "POS up b", "POS up c", "POS up d"],
                 ["NEG dn a", "NEG dn b", "NEG dn c", "NEG dn d"]),
    )

    def fake_embed(texts, model_name):
        if not texts:
            return None
        out = np.zeros((len(texts), _DIM))
        for i, t in enumerate(texts):
            out[i, 0 if str(t).startswith("POS") else 1] = 1.0
        return out

    monkeypatch.setattr(LR, "_embed_texts", fake_embed)

    # Candidates contribute extra random negatives (axis 1).
    model = LR.train_ranker(_papers("NEG", 6), persist=False)
    assert model is not None
    assert model.n_pos == 4  # only the thumbs-up rows
    assert model.n_neg >= 4  # thumbs-down rows + candidate negatives


# ---------------------------------------------------------------------------
# Degradation contract: never raise, return None / zeros
# ---------------------------------------------------------------------------


def test_rank_papers_degrades_to_none_without_embeddings(monkeypatch):
    """rank_papers returns None when Ollama embedding is unavailable."""
    monkeypatch.setattr(LR, "_corpus_embeddings", lambda model_name: None)
    monkeypatch.setattr(LR, "_feedback_texts", lambda: ([], []))
    monkeypatch.setattr(LR, "_embed_texts", lambda texts, model_name: None)
    # No persisted model to fall back on.
    monkeypatch.setattr(LR, "load_model", lambda: None)

    assert LR.rank_papers(_papers("X", 5)) is None


def test_train_ranker_returns_none_without_signal(monkeypatch):
    """train_ranker returns None when neither corpus nor feedback exists."""
    monkeypatch.setattr(LR, "_corpus_embeddings", lambda model_name: None)
    monkeypatch.setattr(LR, "_feedback_texts", lambda: ([], []))
    monkeypatch.setattr(LR, "_embed_texts", lambda texts, model_name: None)

    assert LR.train_ranker(_papers("X", 5)) is None
    assert LR.train_ranker([]) is None


def test_score_candidates_zeros_on_none_model():
    """score_candidates returns all-zeros (not an error) for a None model."""
    emb = np.zeros((4, _DIM))
    scores = LR.score_candidates(emb, None)
    assert scores == [0.0, 0.0, 0.0, 0.0]


def test_score_candidates_zeros_on_dim_mismatch(synthetic_signal, tmp_path, monkeypatch):
    """A candidate embedding of the wrong width scores zeros, never raises."""
    monkeypatch.setattr(LR, "_MODEL_PATH", tmp_path / "ranker.pkl")
    model = LR.train_ranker(_papers("NEG", 12), persist=False)
    assert model is not None
    wrong = np.zeros((3, _DIM + 4))
    assert LR.score_candidates(wrong, model) == [0.0, 0.0, 0.0]


def test_rank_papers_empty_candidates_returns_none():
    """An empty candidate list degrades to None."""
    assert LR.rank_papers([]) is None


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_model_persists_and_reloads(synthetic_signal, tmp_path, monkeypatch):
    """A trained model saved to disk reloads and scores identically."""
    monkeypatch.setattr(LR, "_MODEL_PATH", tmp_path / "ranker.pkl")
    trained = LR.train_ranker(_papers("NEG", 16), persist=True)
    assert trained is not None
    assert (tmp_path / "ranker.pkl").exists()

    reloaded = LR.load_model()
    assert reloaded is not None
    assert reloaded.kind == trained.kind
    assert reloaded.dim == trained.dim

    held_out = _papers("POS_eval", 5) + _papers("NEG_eval", 5)
    a = LR.rank_papers(held_out, model=trained)
    b = LR.rank_papers(held_out, model=reloaded)
    assert a is not None and b is not None
    assert a == pytest.approx(b)
