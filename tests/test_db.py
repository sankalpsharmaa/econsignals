"""Unit tests for econsignals.lib.db."""

import pytest

from econsignals.lib.db import (
    find_paper_by_doi,
    find_paper_by_normalized_title,
    get_db,
    insert_paper,
    insert_paper_source,
    link_paper_author,
    upsert_author,
)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Clear papers, paper_sources, authors, paper_authors before each test."""
    conn = get_db()
    conn.execute("DELETE FROM paper_authors")
    conn.execute("DELETE FROM paper_sources")
    conn.execute("DELETE FROM authors")
    conn.execute("DELETE FROM papers")
    conn.commit()
    conn.close()
    yield


class TestInsertPaper:
    """Tests for insert_paper()."""

    def test_insert_new_paper(self):
        pid = insert_paper(
            {
                "canonical_id": "abc123",
                "title": "Test Paper",
                "title_normalized": "test paper",
            }
        )
        assert pid > 0

    def test_insert_or_ignore_duplicate_canonical_id(self):
        data = {"canonical_id": "dup1", "title": "First", "title_normalized": "first"}
        pid1 = insert_paper(data)
        pid2 = insert_paper(data)
        assert pid1 == pid2

    def test_serializes_jel_codes_and_keywords(self):
        import json

        pid = insert_paper(
            {
                "canonical_id": "j1",
                "title": "JEL Test",
                "title_normalized": "jel test",
                "jel_codes": ["O1", "R1"],
                "keywords": ["urban", "housing"],
            }
        )
        assert pid > 0
        conn = get_db()
        row = conn.execute("SELECT jel_codes, keywords FROM papers WHERE id = ?", (pid,)).fetchone()
        conn.close()
        assert json.loads(row["jel_codes"]) == ["O1", "R1"]
        assert json.loads(row["keywords"]) == ["urban", "housing"]


class TestFindPaperByDoi:
    """Tests for find_paper_by_doi()."""

    def test_finds_existing(self):
        insert_paper({
            "canonical_id": "f1",
            "title": "DOI Paper",
            "title_normalized": "doi paper",
            "doi": "10.1234/test",
        })

        found = find_paper_by_doi("10.1234/test")
        assert found is not None
        assert found["doi"] == "10.1234/test"
        assert found["title"] == "DOI Paper"

    def test_returns_none_for_missing(self):
        assert find_paper_by_doi("10.9999/nonexistent") is None


class TestFindPaperByNormalizedTitle:
    """Tests for find_paper_by_normalized_title()."""

    def test_finds_exact_match(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO papers (canonical_id, title, title_normalized) VALUES (?, ?, ?)",
            ("t1", "Inflation and Wages", "inflation and wages"),
        )
        conn.commit()
        conn.close()

        results = find_paper_by_normalized_title("inflation and wages")
        assert len(results) == 1
        assert results[0]["title"] == "Inflation and Wages"

    def test_returns_empty_for_missing(self):
        assert find_paper_by_normalized_title("nonexistent title norm") == []


class TestUpsertAuthor:
    """Tests for upsert_author() and link_paper_author()."""

    def test_upsert_creates_new(self):
        aid = upsert_author("John Smith", "john smith")
        assert aid > 0

    def test_upsert_returns_same_id_on_duplicate(self):
        aid1 = upsert_author("Jane Doe", "jane doe")
        aid2 = upsert_author("Jane Doe", "jane doe")
        assert aid1 == aid2

    def test_link_paper_author(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO papers (canonical_id, title, title_normalized) VALUES (?, ?, ?)",
            ("p1", "Paper", "paper"),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()

        aid = upsert_author("Author One", "author one")
        link_paper_author(paper_id=pid, author_id=aid, position=0)

        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM paper_authors WHERE paper_id = ?", (pid,)
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["author_id"] == aid
        assert rows[0]["position"] == 0


class TestInsertPaperSource:
    """Tests for insert_paper_source()."""

    def test_inserts_source(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO papers (canonical_id, title, title_normalized) VALUES (?, ?, ?)",
            ("s1", "Source Paper", "source paper"),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()

        result = insert_paper_source(
            paper_id=pid,
            source="openalex",
            source_id="W123",
            source_url="https://openalex.org/W123",
        )
        assert result > 0

    def test_ignores_duplicate_source_id(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO papers (canonical_id, title, title_normalized) VALUES (?, ?, ?)",
            ("s2", "Dup Source", "dup source"),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()

        r1 = insert_paper_source(pid, "nber", "wp123", None)
        r2 = insert_paper_source(pid, "nber", "wp123", None)
        assert r1 > 0
        assert r2 == 0  # duplicate returns 0
