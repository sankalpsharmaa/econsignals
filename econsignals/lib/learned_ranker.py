"""Learned personal ranker (Scholar Inbox / arxiv-sanity style).

Fits a logistic boundary in embedding space that separates what the user reads
from what they ignore, then scores fresh candidates by their distance from that
boundary. This is the supervised counterpart to the unsupervised top-k Zotero
similarity in ``zotero_embeddings.py``: instead of "near my library = good", it
learns "papers like the ones I keep AND thumbs-up, unlike the ones I thumb-down
or never engage with".

Training signal
    Positives  = the user's Zotero library embeddings
                 + thumbs-up rows from data/feedback.jsonl
    Negatives  = thumbs-down rows from data/feedback.jsonl
                 + a random sample of recent candidate papers (presumed-ignored)

Classifier
    sklearn LogisticRegression when sklearn imports; otherwise a numpy-only
    nearest-centroid classifier (signed cosine-to-centroid gap squashed through a
    logistic), since the repo only guarantees numpy. Embeddings are L2-normalised
    so the logistic operates on cosine geometry in both branches.

Degradation contract
    Every public entry point degrades to None / zeros when embeddings (numpy or
    Ollama) or training signal are unavailable, so a caller can keep its existing
    behaviour unchanged. Nothing here is wired into snapshot.py; the integrator
    wires it.

Persistence
    The trained model is pickled to data/learned_ranker.pkl.

Usage:
    from econsignals.lib.learned_ranker import rank_papers
    scores = rank_papers(candidates)   # list[float] in [0,1], or None
"""

from __future__ import annotations

import json
import logging
import pickle
import random
from dataclasses import dataclass, field
from pathlib import Path

from econsignals.lib.zotero_embeddings import (
    _DEFAULT_MODEL,
    _corpus_hash,
    _embed_batch,
    _load_corpus_cache,
)
from econsignals.lib.zotero_profile import load_zotero_corpus

# numpy is the only guaranteed numeric dependency; without it no ranker can
# train or score, so every entry point degrades to None.
_NUMPY_AVAILABLE = False
try:
    import numpy as np

    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

# Persisted model path: data/learned_ranker.pkl (same dir as econsignals.db).
_MODEL_PATH = Path(__file__).resolve().parents[2] / "data" / "learned_ranker.pkl"

# Feedback audit trail written by econsignals/web/app.py; each line is
# {"paper_id": int, "interesting": bool, "at": iso}.
_FEEDBACK_LOG = Path(__file__).resolve().parents[2] / "data" / "feedback.jsonl"

# Cap the count of presumed-ignored candidate negatives so the Zotero positives
# are not swamped when the candidate pool is large.
_MAX_RANDOM_NEGATIVES = 200

# Minimum labelled examples per class required to train a classifier at all.
_MIN_PER_CLASS = 3

# Fixed seed so a given feedback + candidate set trains a reproducible model.
_RANDOM_SEED = 13


# ---------------------------------------------------------------------------
# Model container
# ---------------------------------------------------------------------------


@dataclass
class RankerModel:
    """A trained ranker plus the metadata needed to score and persist it.

    Attributes:
        kind: "logistic" (sklearn) or "centroid" (numpy fallback).
        dim: Embedding dimensionality the model was fit on; scoring rejects
            candidate embeddings of a different width.
        model_name: Ollama embedding model used for training texts.
        sklearn_model: Fitted sklearn estimator (None for the centroid kind).
        pos_centroid: L2-normalised positive centroid (centroid kind only).
        neg_centroid: L2-normalised negative centroid (centroid kind only).
        scale: Logistic temperature for the centroid kind.
        n_pos: Positive training count (diagnostics).
        n_neg: Negative training count (diagnostics).
    """

    kind: str
    dim: int
    model_name: str
    sklearn_model: object | None = None
    pos_centroid: "np.ndarray | None" = None
    neg_centroid: "np.ndarray | None" = None
    scale: float = 8.0
    n_pos: int = 0
    n_neg: int = 0
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Embedding helpers (pure numeric; no network)
# ---------------------------------------------------------------------------


def _l2_normalize(matrix: "np.ndarray") -> "np.ndarray":
    """Return row-wise L2-normalised copy of `matrix` (rows -> unit vectors)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / (norms + 1e-12)


def _candidate_text(paper: dict) -> str:
    """Build the embedding text for a candidate: title + abstract."""
    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    text = f"{title} {abstract}".strip()
    return text if text else "(no text)"


def _embed_texts(texts: list[str], model_name: str) -> "np.ndarray | None":
    """Embed texts via Ollama and return an (n, dim) float64 matrix, or None.

    Returns None when numpy is missing, the input is empty, or Ollama fails for
    any text (mirrors `_embed_batch`'s fail-fast contract).
    """
    if not _NUMPY_AVAILABLE or np is None or not texts:
        return None
    vectors = _embed_batch(texts, model_name)
    if not vectors:
        return None
    return np.asarray(vectors, dtype=np.float64)


# ---------------------------------------------------------------------------
# Training-signal assembly
# ---------------------------------------------------------------------------


def _load_feedback() -> list[dict]:
    """Read data/feedback.jsonl into a list of {paper_id, interesting} dicts.

    Skips malformed lines. Returns an empty list when the log is absent.
    """
    if not _FEEDBACK_LOG.exists():
        return []
    rows: list[dict] = []
    with _FEEDBACK_LOG.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = rec.get("paper_id")
            if pid is None or "interesting" not in rec:
                continue
            rows.append({"paper_id": int(pid), "interesting": bool(rec["interesting"])})
    return rows


def _feedback_texts() -> tuple[list[str], list[str]]:
    """Resolve thumbs-up / thumbs-down feedback paper_ids to embedding texts.

    A feedback row references a paper by its database id; this looks each paper
    up (last vote wins per paper) and returns (positive_texts, negative_texts).
    Unresolvable ids (deleted papers) are skipped. Imports the DB layer lazily so
    importing this module never forces a DB connection.
    """
    rows = _load_feedback()
    if not rows:
        return [], []

    # Last vote per paper wins, matching how a user changes their mind.
    latest: dict[int, bool] = {}
    for r in rows:
        latest[r["paper_id"]] = r["interesting"]

    from econsignals.lib.db import get_paper_with_sources

    pos_texts: list[str] = []
    neg_texts: list[str] = []
    for paper_id, interesting in latest.items():
        try:
            paper = get_paper_with_sources(paper_id)
        except KeyError:
            continue
        text = _candidate_text(paper)
        (pos_texts if interesting else neg_texts).append(text)
    return pos_texts, neg_texts


def _corpus_embeddings(model_name: str) -> "np.ndarray | None":
    """Return the user's Zotero library embeddings as an (n, dim) matrix, or None.

    Reuses the on-disk corpus cache from `zotero_embeddings` when its hash and
    model match, so training does not re-embed the whole library; otherwise it
    embeds the corpus fresh via Ollama. Returns None when the corpus is empty or
    embeddings are unavailable.
    """
    if not _NUMPY_AVAILABLE or np is None:
        return None
    corpus = load_zotero_corpus()
    corpus_texts = [str(c.get("text") or "").strip() for c in corpus]
    corpus_texts = [t for t in corpus_texts if t]
    if not corpus_texts:
        return None

    cached = _load_corpus_cache(_corpus_hash(corpus_texts), model_name)
    if cached:
        return np.asarray(cached, dtype=np.float64)
    return _embed_texts(corpus_texts, model_name)


# ---------------------------------------------------------------------------
# Classifier fitting
# ---------------------------------------------------------------------------


def _fit_logistic(X: "np.ndarray", y: "np.ndarray") -> object | None:
    """Fit sklearn LogisticRegression on (X, y); return the estimator or None.

    Imports sklearn lazily so the module never hard-depends on it. Returns None
    when sklearn is absent, signalling the caller to use the numpy fallback.
    """
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None
    # Balanced weights guard against the positive-heavy Zotero corpus dominating
    # a small set of feedback negatives; liblinear is robust for small n.
    clf = LogisticRegression(
        class_weight="balanced",
        max_iter=1000,
        solver="liblinear",
        random_state=_RANDOM_SEED,
    )
    clf.fit(X, y)
    return clf


def _fit_centroid(X: "np.ndarray", y: "np.ndarray") -> tuple["np.ndarray", "np.ndarray"]:
    """Compute L2-normalised positive and negative centroids (numpy fallback)."""
    pos = X[y == 1]
    neg = X[y == 0]
    pos_centroid = _l2_normalize(pos.mean(axis=0, keepdims=True))[0]
    neg_centroid = _l2_normalize(neg.mean(axis=0, keepdims=True))[0]
    return pos_centroid, neg_centroid


def train_ranker(
    candidates: list[dict] | None = None,
    *,
    model_name: str = _DEFAULT_MODEL,
    persist: bool = True,
    random_state: int = _RANDOM_SEED,
) -> RankerModel | None:
    """Train the personal ranker from Zotero + feedback signal.

    Positives  = Zotero library embeddings + thumbs-up feedback papers.
    Negatives  = thumbs-down feedback papers + a random sample of recent
                 `candidates` (presumed-ignored), capped at _MAX_RANDOM_NEGATIVES.

    Trains sklearn LogisticRegression when sklearn imports, else a numpy
    nearest-centroid classifier. The fitted model is pickled to
    data/learned_ranker.pkl when `persist` is True.

    Args:
        candidates: Recent candidate papers (each with title/abstract) used to
            draw presumed-ignored negatives. May be None/empty; then negatives
            come only from thumbs-down feedback.
        model_name: Ollama embedding model.
        persist: Whether to write the model to disk.
        random_state: Seed for the negative subsample.

    Returns:
        A RankerModel, or None when numpy/Ollama/signal are insufficient
        (no embeddings, or fewer than _MIN_PER_CLASS examples in either class).
    """
    if not _NUMPY_AVAILABLE or np is None:
        return None

    # Positives: Zotero corpus (+ thumbs-up feedback).
    pos_corpus = _corpus_embeddings(model_name)
    fb_pos_texts, fb_neg_texts = _feedback_texts()
    fb_pos = _embed_texts(fb_pos_texts, model_name) if fb_pos_texts else None

    pos_parts = [m for m in (pos_corpus, fb_pos) if m is not None and len(m)]
    if not pos_parts:
        _log.debug("learned_ranker: no positive embeddings available")
        return None
    X_pos = np.vstack(pos_parts)

    # Negatives: thumbs-down feedback + a random sample of recent candidates.
    fb_neg = _embed_texts(fb_neg_texts, model_name) if fb_neg_texts else None

    rng = random.Random(random_state)
    cand_texts: list[str] = []
    if candidates:
        cand_texts = [_candidate_text(c) for c in candidates]
        if len(cand_texts) > _MAX_RANDOM_NEGATIVES:
            cand_texts = rng.sample(cand_texts, _MAX_RANDOM_NEGATIVES)
    rand_neg = _embed_texts(cand_texts, model_name) if cand_texts else None

    neg_parts = [m for m in (fb_neg, rand_neg) if m is not None and len(m)]
    if not neg_parts:
        _log.debug("learned_ranker: no negative embeddings available")
        return None
    X_neg = np.vstack(neg_parts)

    if len(X_pos) < _MIN_PER_CLASS or len(X_neg) < _MIN_PER_CLASS:
        _log.debug(
            "learned_ranker: too few examples (pos=%d, neg=%d)", len(X_pos), len(X_neg)
        )
        return None

    if X_pos.shape[1] != X_neg.shape[1]:
        _log.warning(
            "learned_ranker: embedding dim mismatch pos=%d neg=%d",
            X_pos.shape[1],
            X_neg.shape[1],
        )
        return None

    X = _l2_normalize(np.vstack([X_pos, X_neg]))
    y = np.concatenate([np.ones(len(X_pos)), np.zeros(len(X_neg))]).astype(int)
    dim = X.shape[1]

    sk_model = _fit_logistic(X, y)
    if sk_model is not None:
        model = RankerModel(
            kind="logistic",
            dim=dim,
            model_name=model_name,
            sklearn_model=sk_model,
            n_pos=int(len(X_pos)),
            n_neg=int(len(X_neg)),
        )
    else:
        pos_centroid, neg_centroid = _fit_centroid(X, y)
        model = RankerModel(
            kind="centroid",
            dim=dim,
            model_name=model_name,
            pos_centroid=pos_centroid,
            neg_centroid=neg_centroid,
            n_pos=int(len(X_pos)),
            n_neg=int(len(X_neg)),
        )

    if persist:
        _save_model(model)
    return model


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _centroid_scores(emb: "np.ndarray", model: RankerModel) -> "np.ndarray":
    """Score rows of `emb` with the centroid model -> probabilities in [0,1].

    Score is the logistic of `scale * (cos(x, pos) - cos(x, neg))`, so a
    candidate nearer the positive centroid than the negative centroid scores
    above 0.5.
    """
    Xn = _l2_normalize(emb)
    pos_sim = Xn @ model.pos_centroid
    neg_sim = Xn @ model.neg_centroid
    gap = model.scale * (pos_sim - neg_sim)
    return 1.0 / (1.0 + np.exp(-gap))


def score_candidates(cand_emb: "np.ndarray | list", model: RankerModel | None) -> list[float]:
    """Return P(interesting) in [0,1] for each candidate embedding.

    Args:
        cand_emb: (n, dim) array (or list of vectors) of candidate embeddings.
        model: A trained RankerModel, or None.

    Returns:
        A list of floats in [0,1]. Returns all-zeros when the model is None,
        numpy is unavailable, the input is empty, or the embedding width does
        not match the model (graceful degradation, never an exception).
    """
    if not _NUMPY_AVAILABLE or np is None or model is None:
        return [0.0] * (len(cand_emb) if cand_emb is not None else 0)

    emb = np.asarray(cand_emb, dtype=np.float64)
    if emb.ndim != 2 or emb.shape[0] == 0:
        return [0.0] * (emb.shape[0] if emb.ndim == 2 else 0)
    if emb.shape[1] != model.dim:
        _log.warning(
            "learned_ranker: candidate dim %d != model dim %d", emb.shape[1], model.dim
        )
        return [0.0] * emb.shape[0]

    if model.kind == "logistic" and model.sklearn_model is not None:
        Xn = _l2_normalize(emb)
        proba = model.sklearn_model.predict_proba(Xn)
        # Column index of the positive class (label 1) in the fitted estimator.
        classes = list(getattr(model.sklearn_model, "classes_", [0, 1]))
        pos_idx = classes.index(1) if 1 in classes else proba.shape[1] - 1
        scores = proba[:, pos_idx]
    else:
        scores = _centroid_scores(emb, model)

    return [float(s) for s in scores]


def rank_papers(
    candidates: list[dict],
    *,
    model: RankerModel | None = None,
    model_name: str = _DEFAULT_MODEL,
    train_if_missing: bool = True,
) -> list[float] | None:
    """Score `candidates` with the learned ranker, returning [0,1] per paper.

    Embeds each candidate's title+abstract via Ollama and applies the model.
    Loads the persisted model when none is passed; trains one from current
    Zotero + feedback signal (using these candidates as presumed-ignored
    negatives) when none exists and `train_if_missing` is True.

    Degrades to None — so the caller keeps its current ordering — whenever
    numpy/Ollama/training-signal are unavailable or no usable model can be
    obtained. Never raises on a missing dependency.

    Args:
        candidates: Papers to score (each with title/abstract).
        model: A pre-trained model; if None, loads/trains one.
        model_name: Ollama embedding model.
        train_if_missing: Train a fresh model when no persisted one is found.

    Returns:
        List of float scores in [0,1] (one per candidate), or None when the
        ranker cannot run.
    """
    if not _NUMPY_AVAILABLE or np is None or not candidates:
        return None

    if model is None:
        model = load_model()
    if model is None and train_if_missing:
        model = train_ranker(candidates, model_name=model_name)
    if model is None:
        return None

    cand_emb = _embed_texts([_candidate_text(c) for c in candidates], model.model_name)
    if cand_emb is None:
        return None

    scores = score_candidates(cand_emb, model)
    if not scores or len(scores) != len(candidates):
        return None
    return scores


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _save_model(model: RankerModel) -> None:
    """Pickle `model` to data/learned_ranker.pkl.

    Pickle is safe here: the file is written and read only by this module under
    the project's own data/ directory (same pattern as the zotero_embeddings
    corpus cache); it is never loaded from an untrusted source.
    """
    try:
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _MODEL_PATH.open("wb") as fh:
            pickle.dump(model, fh)
        _log.info(
            "learned_ranker: saved %s model (pos=%d, neg=%d) to %s",
            model.kind,
            model.n_pos,
            model.n_neg,
            _MODEL_PATH,
        )
    except (OSError, pickle.PickleError) as exc:
        _log.warning("learned_ranker: failed to save model: %s", exc)


def load_model() -> RankerModel | None:
    """Load the persisted ranker from data/learned_ranker.pkl, or None.

    Returns None when the file is absent, unreadable, or not a RankerModel
    (graceful degradation, never an exception).
    """
    if not _MODEL_PATH.exists():
        return None
    try:
        with _MODEL_PATH.open("rb") as fh:
            model = pickle.load(fh)
    except (OSError, pickle.PickleError, AttributeError, EOFError) as exc:
        _log.debug("learned_ranker: failed to load model: %s", exc)
        return None
    return model if isinstance(model, RankerModel) else None
