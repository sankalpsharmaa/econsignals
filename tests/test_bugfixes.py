"""Tests for the four bug fixes identified in the repository.

Bug 1: _applicable_threshold used wrong comparison direction.
Bug 2: score_author_proximity co-author query checked same paper.
Bug 3: Dedup author-overlap fallback never matched (no authors column in papers).
Bug 4: generate_daily_brief leaked DB connection.
"""

import pytest

from econsignals.lib.db import (
    get_db,
    insert_paper,
    link_paper_author,
    upsert_author,
)
from econsignals.lib.normalize import normalize_author_name, normalize_title


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_tables():
    """Clear relevant tables before each test."""
    conn = get_db()
    conn.execute("DELETE FROM paper_authors")
    conn.execute("DELETE FROM paper_sources")
    conn.execute("DELETE FROM social_items")
    conn.execute("DELETE FROM authors")
    conn.execute("DELETE FROM papers")
    conn.commit()
    conn.close()
    yield


# ---------------------------------------------------------------------------
# Bug 1: _applicable_threshold
# ---------------------------------------------------------------------------

class TestApplicableThreshold:
    """Tests for the fixed _applicable_threshold logic."""

    def test_exact_threshold_boundary(self):
        from econsignals.lenses.deadline_alert import _applicable_threshold

        assert _applicable_threshold(60) == 60
        assert _applicable_threshold(30) == 30
        assert _applicable_threshold(14) == 14
        assert _applicable_threshold(7) == 7
        assert _applicable_threshold(3) == 3
        assert _applicable_threshold(1) == 1

    def test_between_thresholds(self):
        from econsignals.lenses.deadline_alert import _applicable_threshold

        # 28 days: within the 30-day window
        assert _applicable_threshold(28) == 30

        # 5 days: within the 7-day window
        assert _applicable_threshold(5) == 7

        # 10 days: within the 14-day window
        assert _applicable_threshold(10) == 14

        # 2 days: within the 3-day window
        assert _applicable_threshold(2) == 3

    def test_day_of_deadline(self):
        from econsignals.lenses.deadline_alert import _applicable_threshold

        # 0 days remaining: should get the 1-day threshold (FINAL alert)
        assert _applicable_threshold(0) == 1

    def test_beyond_all_thresholds(self):
        from econsignals.lenses.deadline_alert import _applicable_threshold

        # 90 days away: beyond all alert windows
        assert _applicable_threshold(90) is None
        assert _applicable_threshold(100) is None


# ---------------------------------------------------------------------------
# Bug 2: score_author_proximity co-author query
# ---------------------------------------------------------------------------

class TestScoreAuthorProximityCoauthor:
    """Tests for the fixed co-author-of-tracked scoring path."""

    def test_coauthor_of_tracked_scores_half(self):
        """An unknown author who co-authored another paper with a tracked
        author should score 0.5 on the current paper."""
        from econsignals.lib.relevance import score_author_proximity

        # Create a tracked author
        tracked_id = upsert_author("Tracked Author", "tracked author", is_tracked=1)

        # Create an unknown author
        unknown_id = upsert_author("Unknown Author", "unknown author")

        # Paper A: tracked + unknown co-authored together
        pid_a = insert_paper({
            "canonical_id": "coauth_a",
            "title": "Paper A",
            "title_normalized": "paper a",
        })
        link_paper_author(pid_a, tracked_id, 0)
        link_paper_author(pid_a, unknown_id, 1)

        # Paper B: only the unknown author (the paper we're scoring)
        pid_b = insert_paper({
            "canonical_id": "coauth_b",
            "title": "Paper B",
            "title_normalized": "paper b",
        })
        link_paper_author(pid_b, unknown_id, 0)

        db = get_db()
        try:
            score = score_author_proximity(pid_b, db)
        finally:
            db.close()

        # The unknown author on paper B has co-authored paper A with a tracked
        # author, so paper B should get the co-author-of-tracked score (0.5)
        assert score == 0.5

    def test_no_coauthor_link_scores_zero(self):
        """An unknown author with no co-authorship to tracked authors scores 0."""
        from econsignals.lib.relevance import score_author_proximity

        # Create an unknown author
        unknown_id = upsert_author("Lone Author", "lone author")

        pid = insert_paper({
            "canonical_id": "lone_paper",
            "title": "Lone Paper",
            "title_normalized": "lone paper",
        })
        link_paper_author(pid, unknown_id, 0)

        db = get_db()
        try:
            score = score_author_proximity(pid, db)
        finally:
            db.close()

        assert score == 0.0

    def test_tracked_author_scores_one(self):
        """A paper with a tracked author directly should score 1.0."""
        from econsignals.lib.relevance import score_author_proximity

        tracked_id = upsert_author("Direct Tracked", "direct tracked", is_tracked=1)

        pid = insert_paper({
            "canonical_id": "tracked_paper",
            "title": "Tracked Paper",
            "title_normalized": "tracked paper",
        })
        link_paper_author(pid, tracked_id, 0)

        db = get_db()
        try:
            score = score_author_proximity(pid, db)
        finally:
            db.close()

        assert score == 1.0


# ---------------------------------------------------------------------------
# Bug 3: Dedup author-overlap fallback
# ---------------------------------------------------------------------------

class TestDedupAuthorOverlapFallback:
    """Tests for the fixed author-overlap fallback in find_existing_paper."""

    def test_author_overlap_finds_duplicate(self):
        """Step 4 of dedup cascade should match papers with shared authors
        and similar (but not identical) titles."""
        from econsignals.lib.dedup import find_existing_paper

        # Insert a paper with authors via the full pipeline
        pid = insert_paper({
            "canonical_id": "overlap_test_1",
            "title": "Economic Growth and Development in South Asia",
            "title_normalized": normalize_title(
                "Economic Growth and Development in South Asia"
            ),
        })

        # Link two authors
        a1 = upsert_author("John Smith", normalize_author_name("John Smith"))
        a2 = upsert_author("Alice Zhao", normalize_author_name("Alice Zhao"))
        link_paper_author(pid, a1, 0)
        link_paper_author(pid, a2, 1)

        # Now search for a paper with a slightly different title but same
        # authors — should be caught by step 4 (author overlap)
        result = find_existing_paper(
            title="Economic Growth and Development in South Asia: New Evidence",
            authors=["John Smith", "Alice Zhao"],
            doi=None,
        )

        assert result == pid

    def test_author_overlap_no_match_different_authors(self):
        """Step 4 should NOT match when authors don't overlap."""
        from econsignals.lib.dedup import find_existing_paper

        pid = insert_paper({
            "canonical_id": "no_overlap_test",
            "title": "Economic Growth and Development in South Asia",
            "title_normalized": normalize_title(
                "Economic Growth and Development in South Asia"
            ),
        })

        a1 = upsert_author("Bob Jones", normalize_author_name("Bob Jones"))
        a2 = upsert_author("Carol White", normalize_author_name("Carol White"))
        link_paper_author(pid, a1, 0)
        link_paper_author(pid, a2, 1)

        # Different authors, similar title
        result = find_existing_paper(
            title="Economic Growth and Development in South Asia: New Evidence",
            authors=["John Smith", "Alice Zhao"],
            doi=None,
        )

        assert result is None
