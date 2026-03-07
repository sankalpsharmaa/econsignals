"""Tests for econsignals.lib.zotero_profile."""

import pytest

from econsignals.lib.zotero_profile import load_zotero_corpus, zotero_db_path


class TestZoteroDbPath:
    """Tests for zotero_db_path()."""

    def test_returns_none_when_default_missing(self):
        # Use non-existent path to simulate missing Zotero
        result = zotero_db_path("/nonexistent/path/to/zotero.sqlite")
        assert result is None

    def test_returns_path_when_exists(self, tmp_path):
        db = tmp_path / "zotero.sqlite"
        db.touch()
        result = zotero_db_path(str(db))
        assert result is not None
        assert result == db


class TestLoadZoteroCorpus:
    """Tests for load_zotero_corpus()."""

    def test_returns_empty_when_db_missing(self):
        corpus = load_zotero_corpus("/nonexistent/zotero.sqlite")
        assert corpus == []

@pytest.mark.skipif(
    zotero_db_path() is None,
    reason="Zotero DB not found at ~/Zotero/zotero.sqlite",
)
class TestLoadZoteroCorpusWithRealDb:
    """Integration tests using your actual Zotero database."""

    def test_loads_corpus_with_expected_structure(self):
        corpus = load_zotero_corpus()
        if not corpus:
            pytest.skip("Zotero corpus empty (no items with title+abstract)")
        item = corpus[0]
        assert "title" in item
        assert "abstract" in item
        assert "text" in item
        assert "date_added" in item
        assert len(item["text"]) >= 20

    def test_corpus_sorted_by_date_descending(self):
        corpus = load_zotero_corpus(max_items=100)
        if len(corpus) < 2:
            pytest.skip("Need at least 2 corpus items")
        for i in range(len(corpus) - 1):
            assert corpus[i]["date_added"] >= corpus[i + 1]["date_added"]

    def test_corpus_respects_max_items(self):
        corpus_full = load_zotero_corpus(max_items=500)
        corpus_10 = load_zotero_corpus(max_items=10)
        if len(corpus_full) < 10:
            pytest.skip("Zotero has fewer than 10 items")
        assert len(corpus_10) <= 10
        assert len(corpus_10) <= len(corpus_full)
