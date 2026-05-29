"""SPECTER2 embedding backend (default OFF, opt-in via env flag).

SPECTER2 (allenai/specter2_base + the "proximity" adapter) is a scientific-paper
embedding model purpose-built for citation/similarity tasks, so it discriminates
related work better than a general-purpose text embedder. It is NOT the default
because it needs heavy dependencies (transformers + adapters + torch) and is slow
on CPU. It is selected only when the caller sets ``ECONSIGNALS_EMBED_BACKEND=specter2``
AND those dependencies import successfully; otherwise the existing Ollama
nomic-embed-text path (econsignals.lib.zotero_embeddings) handles embedding.

Install (NOT in requirements.txt; opt-in only)::

    pip install transformers adapters torch

Selection contract (mirrors the graceful-fallback pattern in zotero_embeddings):

    from econsignals.lib import specter2_embeddings as S2
    if S2.backend_enabled():
        vecs = S2.embed_texts(texts)   # list[list[float]] or None on failure
    if not S2.backend_enabled() or vecs is None:
        ... existing Ollama path ...

``embed_texts`` embeds each string as-is with CLS-token ([:, 0, :]) pooling, the
model-card-documented representation. Callers that want the recommended
title<sep>abstract formatting should join upstream before calling; the existing
zotero_embeddings caller already passes a joined "title abstract" string.

The model and adapter are loaded lazily on the first ``embed_texts`` call and
cached for the process. ``backend_enabled()`` never triggers a download: it only
reads the env flag and checks importability via importlib.util.find_spec.
"""

from __future__ import annotations

import importlib.util
import logging
import os

_ENV_FLAG = "ECONSIGNALS_EMBED_BACKEND"
_BACKEND_NAME = "specter2"

# Model-card identifiers (https://huggingface.co/allenai/specter2). The base
# encoder plus the "proximity" adapter, which is the recommended setup for
# nearest-neighbour / similarity retrieval.
_BASE_MODEL = "allenai/specter2_base"
_ADAPTER = "allenai/specter2"

# Heavy deps the backend needs; checked by name without importing them so the
# enablement probe stays cheap and side-effect-free.
_REQUIRED_DEPS = ("transformers", "adapters", "torch")

_log = logging.getLogger(__name__)

# Process-level cache of the loaded (tokenizer, model) pair, populated on the
# first successful embed_texts call.
_MODEL_CACHE: tuple | None = None


def _flag_selected() -> bool:
    """Return True iff the env flag requests the SPECTER2 backend.

    Reads ``ECONSIGNALS_EMBED_BACKEND`` only; does not touch dependencies. Kept
    separate from the import check so the selection logic is testable without the
    heavy deps installed.

    Returns:
        True when ``ECONSIGNALS_EMBED_BACKEND`` equals "specter2"
        (case-insensitive, surrounding whitespace ignored).
    """
    return os.environ.get(_ENV_FLAG, "").strip().lower() == _BACKEND_NAME


def _deps_available() -> bool:
    """Report whether transformers, adapters, and torch are importable.

    Uses importlib.util.find_spec so no heavy module is actually imported (and no
    model is downloaded) just to answer the question.

    Returns:
        True when every required dependency can be located on the import path.
    """
    try:
        return all(importlib.util.find_spec(name) is not None for name in _REQUIRED_DEPS)
    except (ImportError, ValueError):
        # find_spec raises on a namespace-package edge case or a broken parent
        # package; treat either as "not available" so the caller falls back.
        return False


def backend_enabled() -> bool:
    """Return True iff the SPECTER2 backend should be used.

    The backend is enabled only when the env flag selects it AND the heavy
    dependencies are importable. Never downloads a model.

    Returns:
        True when ``ECONSIGNALS_EMBED_BACKEND=specter2`` and transformers,
        adapters, and torch are all importable; False otherwise.
    """
    return _flag_selected() and _deps_available()


def _load_model() -> tuple | None:
    """Lazily load and cache the SPECTER2 tokenizer and adapter model.

    Mirrors the allenai/specter2 model-card snippet: load the base encoder via
    the standalone ``adapters`` package's AutoAdapterModel, then attach and
    activate the "proximity" adapter. The heavy imports happen here, not at module
    import time. The first call may download model weights.

    Returns:
        A (tokenizer, model) tuple, or None if loading fails (caught so the caller
        can fall back to the Ollama path).
    """
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE

    try:
        from adapters import AutoAdapterModel
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(_BASE_MODEL)
        model = AutoAdapterModel.from_pretrained(_BASE_MODEL)
        model.load_adapter(_ADAPTER, source="hf", load_as="specter2", set_active=True)
        model.eval()
    except Exception as exc:
        # Import error, download failure, or adapter mismatch: log and fall back.
        _log.warning("SPECTER2 model load failed (%s); falling back to default backend", exc)
        return None

    _MODEL_CACHE = (tokenizer, model)
    return _MODEL_CACHE


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed texts with SPECTER2, or return None so the caller falls back.

    Returns None (rather than raising) whenever the backend is disabled or any
    step fails, so the caller can drop back to the Ollama nomic-embed-text path.
    Each text is embedded as-is using CLS-token pooling (output.last_hidden_state
    at position 0), the representation documented on the model card.

    Args:
        texts: Strings to embed. Each is typically a joined "title abstract".

    Returns:
        A list of float vectors (one per input), or None when the backend is off,
        the model cannot load, or inference fails. An empty input yields [].
    """
    if not backend_enabled():
        return None
    if not texts:
        return []

    loaded = _load_model()
    if loaded is None:
        return None
    tokenizer, model = loaded

    try:
        import torch

        inputs = tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            return_tensors="pt",
            return_token_type_ids=False,
            max_length=512,
        )
        with torch.no_grad():
            output = model(**inputs)
        # CLS-token embedding: first position of the final hidden state.
        cls = output.last_hidden_state[:, 0, :]
        return cls.cpu().tolist()
    except Exception as exc:
        _log.warning("SPECTER2 inference failed (%s); falling back to default backend", exc)
        return None
