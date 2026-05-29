"""Tests for econsignals.lib.specter2_embeddings.

The SPECTER2 backend is default-OFF and gated behind heavy deps
(transformers/adapters/torch). These tests pin the selection logic without any
network call or model download:

- the backend is disabled by default (no env flag set);
- backend_enabled() is the conjunction of the env-flag read and the import check;
- _flag_selected() reads ECONSIGNALS_EMBED_BACKEND directly, so it is testable
  even where the heavy deps are absent;
- embed_texts() short-circuits to None when disabled.

Any test that needs transformers installed is guarded with
``pytest.importorskip("transformers")`` so CI without the optional dependency
still passes, and the real model is mocked so nothing is ever downloaded.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from econsignals.lib import specter2_embeddings as S2


# ---------------------------------------------------------------------------
# Default: backend is OFF when the env flag is unset
# ---------------------------------------------------------------------------


def test_backend_disabled_by_default(monkeypatch):
    monkeypatch.delenv(S2._ENV_FLAG, raising=False)
    assert S2.backend_enabled() is False
    assert S2._flag_selected() is False


# ---------------------------------------------------------------------------
# Flag read: _flag_selected reads ECONSIGNALS_EMBED_BACKEND (no deps needed)
# ---------------------------------------------------------------------------


def test_flag_selected_reads_env(monkeypatch):
    monkeypatch.setenv(S2._ENV_FLAG, "specter2")
    assert S2._flag_selected() is True


def test_flag_selected_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv(S2._ENV_FLAG, "  SPECTER2  ")
    assert S2._flag_selected() is True


def test_flag_selected_other_backend_is_false(monkeypatch):
    monkeypatch.setenv(S2._ENV_FLAG, "ollama")
    assert S2._flag_selected() is False


# ---------------------------------------------------------------------------
# backend_enabled() = flag AND deps; both must hold
# ---------------------------------------------------------------------------


def test_backend_enabled_requires_flag(monkeypatch):
    # Deps present but flag unset -> disabled.
    monkeypatch.delenv(S2._ENV_FLAG, raising=False)
    with patch.object(S2, "_deps_available", return_value=True):
        assert S2.backend_enabled() is False


def test_backend_enabled_requires_deps(monkeypatch):
    # Flag set but deps absent -> disabled.
    monkeypatch.setenv(S2._ENV_FLAG, "specter2")
    with patch.object(S2, "_deps_available", return_value=False):
        assert S2.backend_enabled() is False


def test_backend_enabled_when_flag_and_deps(monkeypatch):
    monkeypatch.setenv(S2._ENV_FLAG, "specter2")
    with patch.object(S2, "_deps_available", return_value=True):
        assert S2.backend_enabled() is True


# ---------------------------------------------------------------------------
# embed_texts short-circuits to None when the backend is off
# ---------------------------------------------------------------------------


def test_embed_texts_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv(S2._ENV_FLAG, raising=False)
    assert S2.embed_texts(["any text"]) is None


def test_embed_texts_empty_input_when_enabled(monkeypatch):
    # Enabled but empty input -> [], without ever loading a model.
    monkeypatch.setenv(S2._ENV_FLAG, "specter2")
    with patch.object(S2, "_deps_available", return_value=True):
        with patch.object(S2, "_load_model") as mock_load:
            assert S2.embed_texts([]) == []
            mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# embed_texts happy path: loader is mocked, so no model download occurs
# ---------------------------------------------------------------------------


def test_embed_texts_returns_vectors_with_mocked_model(monkeypatch):
    # embed_texts imports torch at inference time; this test crosses that import
    # (only the model + tokenizer are mocked), so skip it where torch is absent.
    pytest.importorskip("torch")
    monkeypatch.setenv(S2._ENV_FLAG, "specter2")

    # Stand in for a torch tensor whose CLS slice yields list-of-lists.
    fake_cls = MagicMock()
    fake_cls.cpu.return_value.tolist.return_value = [[0.1, 0.2], [0.3, 0.4]]
    fake_hidden = MagicMock()
    fake_hidden.__getitem__.return_value = fake_cls  # output.last_hidden_state[:, 0, :]
    fake_output = MagicMock()
    fake_output.last_hidden_state = fake_hidden

    fake_model = MagicMock(return_value=fake_output)
    fake_tokenizer = MagicMock(return_value={"input_ids": MagicMock()})

    with patch.object(S2, "_deps_available", return_value=True):
        with patch.object(S2, "_load_model", return_value=(fake_tokenizer, fake_model)):
            vecs = S2.embed_texts(["paper one", "paper two"])

    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_texts_returns_none_on_load_failure(monkeypatch):
    monkeypatch.setenv(S2._ENV_FLAG, "specter2")
    with patch.object(S2, "_deps_available", return_value=True):
        with patch.object(S2, "_load_model", return_value=None):
            assert S2.embed_texts(["text"]) is None


# ---------------------------------------------------------------------------
# Dep-present path: only runs where transformers is installed; model still mocked
# ---------------------------------------------------------------------------


def test_backend_enabled_true_with_real_deps(monkeypatch):
    pytest.importorskip("transformers")
    pytest.importorskip("adapters")
    pytest.importorskip("torch")
    monkeypatch.setenv(S2._ENV_FLAG, "specter2")
    # No mock of _deps_available: this exercises the real find_spec check.
    assert S2.backend_enabled() is True
