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
import sys
import urllib.error
import urllib.request
from pathlib import Path

from econsignals.lib.zotero_profile import zotero_author_score, zotero_content_score

_EMBEDDING_AVAILABLE = False
try:
    import numpy as np

    _EMBEDDING_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]

_DEFAULT_MODEL = "nomic-embed-text"  # Ollama embedding model
_OLLAMA_BASE = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Top-k pooling: score a candidate by its k nearest Zotero items, not the
# whole-corpus mean. Averaging over a homogeneous 495-item econ library buries
# the signal under a high baseline cosine floor (~0.45); the discriminating
# similarity lives in the nearest items.
_TOP_K = 5

# The corpus is all-econ, so even off-topic papers sit on a similarity floor.
# Subtract that floor and rescale so the boost reflects relative, not absolute,
# similarity (empirically: off-topic ~0.54, on-topic ~0.74 top-k mean).
_SIM_FLOOR = 0.45
_SIM_WIDTH = 0.35
_MAX_BOOST = 0.7

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


def ollama_model_available(model: str = _DEFAULT_MODEL) -> bool:
    """Probe Ollama /api/tags and report whether `model` is installed.

    A bare model name (e.g. "nomic-embed-text") matches a "nomic-embed-text:latest"
    tag. Returns False if Ollama is unreachable so the caller can print a hint.
    """
    try:
        req = urllib.request.Request(
            f"{_OLLAMA_BASE.rstrip('/')}/api/tags",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log.debug("Ollama /api/tags probe failed: %s", e)
        return False
    # Match either the exact name or "<name>:<tag>"
    installed = {str(m.get("name") or "") for m in tags.get("models") or []}
    return any(name == model or name.startswith(f"{model}:") for name in installed)


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


def _lexical_scores(
    candidates: list[dict],
    profile: dict | None,
    scale: float,
) -> list[float]:
    """Lexical author+content fallback when embeddings are unavailable.

    Mirrors the newsletter's lexical branch (0.4 * author + 0.3 * content) and
    multiplies by `scale` so the caller's `score / scale` transform recovers a
    boost in the same 0–0.7 range as the embedding path.
    """
    if not profile or not profile.get("ok"):
        return [0.0] * len(candidates)

    out: list[float] = []
    for c in candidates:
        title = str(c.get("title") or "")
        abstract = str(c.get("abstract") or "")
        author_names = [
            (r.get("name") or "").strip()
            for r in (c.get("authors") or [])
            if isinstance(r, dict) and (r.get("name") or "").strip()
        ]
        a_score, _ = zotero_author_score(author_names, profile)
        c_score, _ = zotero_content_score(title, abstract, profile)
        boost = 0.4 * a_score + 0.3 * c_score
        out.append(min(boost, _MAX_BOOST) * scale)
    return out


def compute_zotero_embedding_scores(
    candidates: list[dict],
    corpus: list[dict],
    *,
    model_name: str = _DEFAULT_MODEL,
    text_key: str = "text",
    scale: float = 10.0,
    profile: dict | None = None,
) -> list[float]:
    """Compute a Zotero affinity score for each candidate via top-k similarity.

    Encodes title+abstract with Ollama, then scores each candidate by the mean
    cosine similarity to its k nearest Zotero items (NOT the whole-corpus mean,
    which a homogeneous econ library washes out). The per-item floor is removed
    and rescaled so the score reflects relative similarity. The returned value is
    pre-scaled by `scale` so the caller's `min(score / scale, 0.7)` recovers a
    0–0.7 boost.

    On any embedding failure (deps missing, Ollama down, model absent) this falls
    back to the lexical author+content profile when `profile` is supplied, so the
    personalization path degrades gracefully instead of zeroing every paper.

    Args:
        candidates: List of dicts with title, abstract (and optionally authors).
        corpus: From load_zotero_corpus(); must have "text".
        model_name: Ollama model (e.g. nomic-embed-text).
        text_key: Key in corpus dict for text to embed.
        scale: Pre-scale factor matching the caller's divisor (default 10).
        profile: Lexical Zotero profile from load_zotero_profile(); used as a
            fallback when embeddings are unavailable.

    Returns:
        List of float scores, one per candidate. Falls back to lexical (or zeros
        if no profile) when embeddings cannot be computed.
    """
    if not _EMBEDDING_AVAILABLE or np is None:
        return _lexical_scores(candidates, profile, scale)

    if not corpus:
        return _lexical_scores(candidates, profile, scale)

    candidate_texts: list[str] = []
    for c in candidates:
        title = str(c.get("title") or "")
        abstract = str(c.get("abstract") or "")
        text = f"{title} {abstract}".strip()
        candidate_texts.append(text if text else "(no text)")

    corpus_texts = [str(c.get(text_key) or "").strip() for c in corpus]
    corpus_texts = [t for t in corpus_texts if t]
    if not corpus_texts:
        return _lexical_scores(candidates, profile, scale)

    # Corpus embeddings: use cache if Zotero unchanged
    corpus_hash = _corpus_hash(corpus_texts)
    enc_corpus = _load_corpus_cache(corpus_hash, model_name)
    if enc_corpus is None:
        enc_corpus = _embed_batch(corpus_texts, model_name)
        if not enc_corpus:
            print(
                f"econsignals: Ollama embedding unavailable at {_OLLAMA_BASE} "
                f"(model {model_name}) — falling back to lexical Zotero match. "
                f"Run: ollama pull {model_name}",
                file=sys.stderr,
            )
            return _lexical_scores(candidates, profile, scale)
        _save_corpus_cache(corpus_hash, model_name, enc_corpus)
    else:
        _log.debug("Using cached Zotero corpus embeddings (%d items)", len(enc_corpus))

    enc_candidates = _embed_batch(candidate_texts, model_name)
    if not enc_candidates:
        _log.warning("Ollama embedding failed — is %s running with model %s?", _OLLAMA_BASE, model_name)
        print(
            f"econsignals: Ollama embedding unavailable at {_OLLAMA_BASE} "
            f"(model {model_name}) — falling back to lexical Zotero match. "
            f"Run: ollama pull {model_name}",
            file=sys.stderr,
        )
        return _lexical_scores(candidates, profile, scale)

    # Score each candidate by its top-k nearest Zotero items, then subtract the
    # corpus-wide similarity floor and rescale into a relative 0–_MAX_BOOST band.
    sim = _cosine_similarity(enc_candidates, enc_corpus)
    k = min(_TOP_K, sim.shape[1])
    topk = np.sort(sim, axis=1)[:, -k:]
    pooled = topk.mean(axis=1)
    rel = (pooled - _SIM_FLOOR) / _SIM_WIDTH
    rel = np.clip(rel, 0.0, _MAX_BOOST)
    scores = rel * scale
    return [float(s) for s in scores]
