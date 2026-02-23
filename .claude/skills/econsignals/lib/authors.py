"""Author tracking and entity resolution for EconSignals.

Handles identity resolution across sources (OpenAlex, Semantic Scholar, ORCID),
author tracking management, co-authorship network traversal, and auto-discovery
of frequently-cited untracked authors.

Auto-discovered author states (auto_discovered column):
    0  = never discovered (default)
    1  = pending: surfaced, awaiting user decision
   -1  = dismissed: user rejected, will not be re-surfaced
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
PROJ_ROOT = SKILL_ROOT.parent.parent.parent

from .db import get_db, upsert_author, get_tracked_authors  # noqa: E402
from .normalize import normalize_author_name  # noqa: E402


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _merge_into_author(author_id: int, author_data: dict, conn: sqlite3.Connection) -> None:
    """Update an existing author row with non-null fields from author_data."""
    updatable = (
        "openalex_id",
        "semantic_scholar_id",
        "orcid",
        "affiliation",
        "country",
        "bluesky_handle",
        "twitter_handle",
    )
    sets = []
    values: list[Any] = []
    for field in updatable:
        value = author_data.get(field)
        if value is not None:
            sets.append(f"{field} = COALESCE({field}, ?)")
            values.append(value)

    if not sets:
        return

    values.append(author_id)
    conn.execute(
        f"UPDATE authors SET {', '.join(sets)} WHERE id = ?",
        values,
    )


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def resolve_author_identity(author_data: dict, db: sqlite3.Connection | None = None) -> int:
    """Match an incoming author record to an existing author in the DB.

    Resolution cascade (highest confidence first):
    1. openalex_id exact match
    2. semantic_scholar_id exact match
    3. orcid exact match
    4. name_normalized + affiliation exact match
    5. name_normalized match, only when unique across all rows
    6. Create new author

    On any match the non-null fields from author_data are merged into the
    existing record via COALESCE (existing values are never overwritten).

    Args:
        author_data: Dict with any of: name, openalex_id, semantic_scholar_id,
                     orcid, affiliation, country, bluesky_handle, twitter_handle.
        db: Optional open connection. If None, get_db() is called.

    Returns:
        author_id of the matched or newly created author.
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        # Steps 1-3: exact ID matches
        id_fields = (
            ("openalex_id", author_data.get("openalex_id")),
            ("semantic_scholar_id", author_data.get("semantic_scholar_id")),
            ("orcid", author_data.get("orcid")),
        )
        for column, value in id_fields:
            if not value:
                continue
            row = conn.execute(
                f"SELECT id FROM authors WHERE {column} = ?", (value,)
            ).fetchone()
            if row:
                author_id = row["id"]
                with conn:
                    _merge_into_author(author_id, author_data, conn)
                return author_id

        # Step 4: name_normalized + affiliation
        name = author_data.get("name", "")
        name_norm = normalize_author_name(name) if name else ""
        affiliation = author_data.get("affiliation")

        if name_norm and affiliation is not None:
            row = conn.execute(
                "SELECT id FROM authors WHERE name_normalized = ? AND affiliation = ?",
                (name_norm, affiliation),
            ).fetchone()
            if row:
                author_id = row["id"]
                with conn:
                    _merge_into_author(author_id, author_data, conn)
                return author_id

        # Step 5: name_normalized match, only when unique
        if name_norm:
            rows = conn.execute(
                "SELECT id FROM authors WHERE name_normalized = ?", (name_norm,)
            ).fetchall()
            if len(rows) == 1:
                author_id = rows[0]["id"]
                with conn:
                    _merge_into_author(author_id, author_data, conn)
                return author_id

        # Step 6: create new
        if not name:
            raise ValueError("author_data must include 'name' to create a new author record")

        author_id = upsert_author(
            name=name,
            name_normalized=name_norm,
            openalex_id=author_data.get("openalex_id"),
            semantic_scholar_id=author_data.get("semantic_scholar_id"),
            orcid=author_data.get("orcid"),
            affiliation=affiliation,
            country=author_data.get("country"),
            bluesky_handle=author_data.get("bluesky_handle"),
            twitter_handle=author_data.get("twitter_handle"),
        )
        return author_id

    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# Tracking management
# ---------------------------------------------------------------------------


def track_author(name: str, db: sqlite3.Connection | None = None, **kwargs: Any) -> int:
    """Set is_tracked=1 for an author, creating the record if it does not exist.

    Args:
        name: Display name of the author.
        db: Optional open connection.
        **kwargs: Additional author fields: affiliation, openalex_id,
                  semantic_scholar_id, orcid, bluesky_handle, twitter_handle,
                  country.

    Returns:
        author_id.
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        name_norm = normalize_author_name(name)
        author_id = upsert_author(
            name=name,
            name_normalized=name_norm,
            is_tracked=1,
            **kwargs,
        )
        # upsert may not flip is_tracked if the conflict path left it unchanged;
        # ensure it is explicitly set.
        with conn:
            conn.execute(
                "UPDATE authors SET is_tracked = 1 WHERE id = ?", (author_id,)
            )
        return author_id
    finally:
        if close_after:
            conn.close()


def untrack_author(author_id: int, db: sqlite3.Connection | None = None) -> None:
    """Set is_tracked=0 for the given author.

    Args:
        author_id: Primary key of the author to untrack.
        db: Optional open connection.
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        with conn:
            conn.execute(
                "UPDATE authors SET is_tracked = 0 WHERE id = ?", (author_id,)
            )
    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def auto_discover_authors(
    min_papers: int = 3,
    min_relevance: float = 0.3,
    days: int = 90,
) -> list[dict]:
    """Find authors who appear frequently in relevant recent papers but are untracked.

    Scans paper_authors joined against papers where relevance_score > min_relevance
    and first_seen_at is within the last `days` days. Authors with at least
    min_papers qualifying papers who are neither tracked nor already discovered
    are marked auto_discovered=1 and returned.

    Args:
        min_papers: Minimum qualifying paper count to surface an author.
        min_relevance: Minimum paper relevance_score threshold.
        days: Look-back window in calendar days.

    Returns:
        List of author dicts for newly discovered authors (auto_discovered just
        set to 1). Empty list if none found.
    """
    conn = get_db()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        """
        SELECT pa.author_id, COUNT(pa.paper_id) AS paper_count
        FROM paper_authors pa
        JOIN papers p ON p.id = pa.paper_id
        WHERE p.relevance_score > ?
          AND p.first_seen_at > ?
        GROUP BY pa.author_id
        HAVING paper_count >= ?
        """,
        (min_relevance, cutoff, min_papers),
    ).fetchall()

    if not rows:
        conn.close()
        return []

    candidate_ids = [r["author_id"] for r in rows]
    count_map = {r["author_id"]: r["paper_count"] for r in rows}

    # Filter: not tracked, not already discovered (pending or dismissed)
    placeholders = ",".join("?" * len(candidate_ids))
    author_rows = conn.execute(
        f"""
        SELECT * FROM authors
        WHERE id IN ({placeholders})
          AND is_tracked = 0
          AND auto_discovered = 0
        """,
        candidate_ids,
    ).fetchall()

    if not author_rows:
        conn.close()
        return []

    ids_to_update = [r["id"] for r in author_rows]
    update_placeholders = ",".join("?" * len(ids_to_update))
    with conn:
        conn.execute(
            f"UPDATE authors SET auto_discovered = 1 WHERE id IN ({update_placeholders})",
            ids_to_update,
        )

    discovered = []
    for row in author_rows:
        d = dict(row)
        d["paper_count"] = count_map.get(d["id"], 0)
        discovered.append(d)

    conn.close()
    return discovered


def dismiss_auto_discovered(author_id: int, db: sqlite3.Connection | None = None) -> None:
    """Mark an auto-discovered author as dismissed (auto_discovered=-1).

    Dismissed authors will not be re-surfaced by auto_discover_authors.

    Args:
        author_id: Primary key of the author to dismiss.
        db: Optional open connection.
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        with conn:
            conn.execute(
                "UPDATE authors SET auto_discovered = -1 WHERE id = ?", (author_id,)
            )
    finally:
        if close_after:
            conn.close()


def get_pending_discoveries(db: sqlite3.Connection | None = None) -> list[dict]:
    """Return auto-discovered authors awaiting promotion or dismissal.

    An author is pending when auto_discovered=1 and is_tracked=0.

    Args:
        db: Optional open connection.

    Returns:
        List of author dicts, ordered by paper_count DESC.
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        rows = conn.execute(
            """
            SELECT * FROM authors
            WHERE auto_discovered = 1 AND is_tracked = 0
            ORDER BY paper_count DESC
            """
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# Author queries
# ---------------------------------------------------------------------------


def get_coauthors(author_id: int, db: sqlite3.Connection | None = None) -> list[dict]:
    """Find all co-authors of a given author via shared paper_authors rows.

    Args:
        author_id: Primary key of the focal author.
        db: Optional open connection.

    Returns:
        List of author dicts, each augmented with a 'shared_papers' key
        (count of papers coauthored with the focal author), ordered by
        shared_papers DESC.
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        rows = conn.execute(
            """
            SELECT a.*, COUNT(pa2.paper_id) AS shared_papers
            FROM paper_authors pa1
            JOIN paper_authors pa2 ON pa2.paper_id = pa1.paper_id
                                  AND pa2.author_id != pa1.author_id
            JOIN authors a ON a.id = pa2.author_id
            WHERE pa1.author_id = ?
            GROUP BY a.id
            ORDER BY shared_papers DESC
            """,
            (author_id,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        if close_after:
            conn.close()


def get_author_papers(
    author_id: int,
    db: sqlite3.Connection | None = None,
    limit: int = 20,
) -> list[dict]:
    """Get papers by an author, ordered by published_at DESC.

    Args:
        author_id: Primary key of the author.
        db: Optional open connection.
        limit: Maximum number of papers to return.

    Returns:
        List of paper dicts (all columns from the papers table).
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        rows = conn.execute(
            """
            SELECT p.*
            FROM papers p
            JOIN paper_authors pa ON pa.paper_id = p.id
            WHERE pa.author_id = ?
            ORDER BY p.published_at DESC
            LIMIT ?
            """,
            (author_id, limit),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# Stats maintenance
# ---------------------------------------------------------------------------


def update_author_stats(db: sqlite3.Connection | None = None) -> int:
    """Recompute paper_count and relevance_score for all authors.

    paper_count  = number of rows in paper_authors for the author
    relevance_score = average relevance_score of those papers (NULL if none)

    This is a bulk UPDATE and is safe to run after every sensor pass.

    Args:
        db: Optional open connection.

    Returns:
        Number of author rows updated.
    """
    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        with conn:
            conn.execute(
                """
                UPDATE authors
                SET paper_count = (
                        SELECT COUNT(*) FROM paper_authors pa
                        WHERE pa.author_id = authors.id
                    ),
                    relevance_score = (
                        SELECT COALESCE(AVG(p.relevance_score), 0)
                        FROM paper_authors pa
                        JOIN papers p ON p.id = pa.paper_id
                        WHERE pa.author_id = authors.id
                    )
                """
            )
        row = conn.execute("SELECT changes() AS n").fetchone()
        return row["n"] if row else 0
    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# Co-authorship network
# ---------------------------------------------------------------------------


def get_author_network(
    author_id: int,
    depth: int = 1,
    db: sqlite3.Connection | None = None,
) -> dict:
    """Build the co-authorship network rooted at a given author.

    depth=1 returns only direct co-authors.
    depth=2 also includes co-authors of those co-authors (excluding the center).

    Args:
        author_id: Primary key of the focal author.
        depth: Network radius (1 or 2).
        db: Optional open connection.

    Returns:
        Dict with keys:
            "center"      -- author dict for the focal author
            "connections" -- list of dicts, each with:
                             "author"        (author dict)
                             "shared_papers" (int)
                             "depth"         (int, 1 or 2)
    """
    if depth not in (1, 2):
        raise ValueError(f"depth must be 1 or 2, got {depth}")

    conn = db if db is not None else get_db()
    close_after = db is None

    try:
        center_row = conn.execute(
            "SELECT * FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
        if center_row is None:
            raise KeyError(f"No author with id={author_id}")

        center = dict(center_row)
        connections: list[dict] = []
        seen_ids: set[int] = {author_id}

        # Depth-1: direct co-authors
        depth1_rows = conn.execute(
            """
            SELECT a.*, COUNT(pa2.paper_id) AS shared_papers
            FROM paper_authors pa1
            JOIN paper_authors pa2 ON pa2.paper_id = pa1.paper_id
                                  AND pa2.author_id != pa1.author_id
            JOIN authors a ON a.id = pa2.author_id
            WHERE pa1.author_id = ?
            GROUP BY a.id
            ORDER BY shared_papers DESC
            """,
            (author_id,),
        ).fetchall()

        depth1_ids: list[int] = []
        for row in depth1_rows:
            d = dict(row)
            aid = d["id"]
            seen_ids.add(aid)
            depth1_ids.append(aid)
            connections.append({
                "author": {k: v for k, v in d.items() if k != "shared_papers"},
                "shared_papers": d["shared_papers"],
                "depth": 1,
            })

        if depth == 2 and depth1_ids:
            placeholders = ",".join("?" * len(depth1_ids))
            depth2_rows = conn.execute(
                f"""
                SELECT a.*, COUNT(pa2.paper_id) AS shared_papers
                FROM paper_authors pa1
                JOIN paper_authors pa2 ON pa2.paper_id = pa1.paper_id
                                      AND pa2.author_id != pa1.author_id
                JOIN authors a ON a.id = pa2.author_id
                WHERE pa1.author_id IN ({placeholders})
                  AND pa2.author_id NOT IN ({placeholders})
                GROUP BY a.id
                ORDER BY shared_papers DESC
                """,
                depth1_ids + depth1_ids,
            ).fetchall()

            # Exclude already-seen authors (center + depth-1)
            seen_placeholders = ",".join("?" * len(seen_ids))
            already_seen = {r["id"] for r in conn.execute(
                f"SELECT id FROM authors WHERE id IN ({seen_placeholders})",
                list(seen_ids),
            ).fetchall()}

            for row in depth2_rows:
                d = dict(row)
                aid = d["id"]
                if aid in already_seen:
                    continue
                already_seen.add(aid)
                seen_ids.add(aid)
                connections.append({
                    "author": {k: v for k, v in d.items() if k != "shared_papers"},
                    "shared_papers": d["shared_papers"],
                    "depth": 2,
                })

        return {"center": center, "connections": connections}

    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(SKILL_ROOT))
    from lib.db import init_db  # noqa: E402 (local import for __main__ only)

    init_db()

    discovered = auto_discover_authors()
    print(f"Auto-discovered {len(discovered)} authors:")
    for a in discovered:
        print(f"  {a['name']} ({a.get('affiliation', 'unknown')}) - {a.get('paper_count', 0)} papers")
