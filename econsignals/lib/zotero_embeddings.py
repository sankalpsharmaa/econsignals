"""Embedding-based Zotero personalization (zotero-arxiv-daily style).

Uses Ollama (nomic-embed-text or similar) to compute semantic similarity between
candidate papers and your Zotero library. Newer Zotero items are weighted more.

Corpus embeddings are cached to disk; recomputed only when Zotero content changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import urllib.request
from pathlib import Path

_EMBEDDING_AVAILABLE = False
try:
    import numpy as np

    _EMBEDDING_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]

_DEFAULT_MODEL = "nomic-embed-text"  # Ollama embedding model
_OLLAMA_BASE = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Cache path: data/zotero_embedding_cache.pkl (same dir as econsignals.db)
_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "zotero_embedding_cache.pkl"

_log = logging.getLogger(__name__)


def _ollama_embed(text: str, model: str) -> list[float] | None:
    """Call Ollama /api/embeddings. Returns embedding vector or None on failure."""
    try:
        req = urllib.request.Request(
            f"{_OLLAMA_BASE.rstrip('/')}/api/embeddings",
            data=json.dumps({"model": model, "prompt": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            return out.get("embedding")
    except Exception as e:
        _log.debug("Ollama embed failed for %r: %s", text[:50], e)
        return None


def _embed_batch(texts: list[str], model: str) -> list[list[float]]:
    """Embed texts via Ollama (one request per text)."""
    out: list[list[float]] = []
    for t in texts:
        emb = _ollama_embed(t or "(empty)", model)
        if emb is None:
            return []  # fail fast
        out.append(emb)
    return out


def _corpus_hash(corpus_texts: list[str]) -> str:
    """Content hash of corpus; changes when any item is added, removed, or edited."""
    blob = "\n".join(corpus_texts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _load_corpus_cache(corpus_hash: str, model_name: str) -> list[list[float]] | None:
    """Load cached corpus embeddings if hash and model match."""
    if not _CACHE_PATH.exists():
        return None
    try:
        with open(_CACHE_PATH, "rb") as f:
            data = pickle.load(f)
        if data.get("corpus_hash") != corpus_hash or data.get("model") != model_name:
            return None
        emb = data.get("embeddings")
        if emb is None:
            return None
        # Normalize to list of lists (may be numpy from older cache)
        if hasattr(emb, "tolist"):
            return emb.tolist()
        return list(emb)
    except (pickle.PickleError, OSError, KeyError) as e:
        _log.debug("Corpus cache load failed: %s", e)
        return None


def _save_corpus_cache(corpus_hash: str, model_name: str, embeddings: list[list[float]]) -> None:
    """Save corpus embeddings to disk."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "wb") as f:
            pickle.dump(
                {"corpus_hash": corpus_hash, "model": model_name, "embeddings": embeddings},
                f,
            )
        _log.info("Saved Zotero corpus embeddings to %s (%d items)", _CACHE_PATH, len(embeddings))
    except OSError as e:
        _log.warning("Failed to save corpus cache: %s", e)


def _cosine_similarity(a: list[list[float]], b: list[list[float]]) -> "np.ndarray":
    """Compute (len(a), len(b)) cosine similarity matrix."""
    A = np.array(a, dtype=np.float64)
    B = np.array(b, dtype=np.float64)
    An = A / (np.linalg.norm(A, axis=1, keepdims=True) + 1e-12)
    Bn = B / (np.linalg.norm(B, axis=1, keepdims=True) + 1e-12)
    return np.dot(An, Bn.T)


def compute_zotero_embedding_scores(
    candidates: list[dict],
    corpus: list[dict],
    *,
    model_name: str = _DEFAULT_MODEL,
    text_key: str = "text",
    scale: float = 10.0,
) -> list[float]:
    """Compute Zotero affinity score for each candidate via weighted average similarity.

    Uses Ollama embeddings. Follows zotero-arxiv-daily: encode title+abstract,
    compute cosine similarity to corpus, weight by recency (newer = higher).

    Args:
        candidates: List of dicts with title, abstract.
        corpus: From load_zotero_corpus(); must have "text".
        model_name: Ollama model (e.g. nomic-embed-text).
        text_key: Key in corpus dict for text to embed.
        scale: Multiply final score by this (default 10).

    Returns:
        List of float scores, one per candidate. Zeros if deps missing or Ollama down.
    """
    if not _EMBEDDING_AVAILABLE or np is None:
        return [0.0] * len(candidates)

    if not corpus:
        return [0.0] * len(candidates)

    candidate_texts: list[str] = []
    for c in candidates:
        title = str(c.get("title") or "")
        abstract = str(c.get("abstract") or "")
        text = f"{title} {abstract}".strip()
        candidate_texts.append(text if text else "(no text)")

    corpus_texts = [str(c.get(text_key) or "").strip() for c in corpus]
    corpus_texts = [t for t in corpus_texts if t]
    if not corpus_texts:
        return [0.0] * len(candidates)

    # Corpus embeddings: use cache if Zotero unchanged
    corpus_hash = _corpus_hash(corpus_texts)
    enc_corpus = _load_corpus_cache(corpus_hash, model_name)
    if enc_corpus is None:
        enc_corpus = _embed_batch(corpus_texts, model_name)
        if not enc_corpus:
            return [0.0] * len(candidates)
        _save_corpus_cache(corpus_hash, model_name, enc_corpus)
    else:
        _log.debug("Using cached Zotero corpus embeddings (%d items)", len(enc_corpus))

    enc_candidates = _embed_batch(candidate_texts, model_name)
    if not enc_candidates:
        _log.warning("Ollama embedding failed — is %s running with model %s?", _OLLAMA_BASE, model_name)
        return [0.0] * len(candidates)

    # Recency weights: newer items (earlier in sorted corpus) get higher weight
    n_corpus = len(corpus_texts)
    indices = np.arange(n_corpus, dtype=np.float64)
    time_decay = 1.0 / (1.0 + np.log10(indices + 1.0))
    time_decay = time_decay / time_decay.sum()

    sim = _cosine_similarity(enc_candidates, enc_corpus)
    scores = (sim * time_decay).sum(axis=1) * scale
    return [float(s) for s in scores]
