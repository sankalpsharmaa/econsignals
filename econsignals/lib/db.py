"""Database layer for EconSignals.

SQLite-backed persistence with WAL mode and foreign key enforcement.
Tables: papers, paper_sources, authors, paper_authors, social_items,
        deadlines, sensor_runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from econsignals.lib.paths import get_project_root

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJ_ROOT: Path = get_project_root()

DB_PATH: Path = Path(
    os.environ.get("ECONSIGNALS_DB", str(PROJ_ROOT / "data" / "econsignals.db"))
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    canonical_id TEXT UNIQUE,
    title TEXT NOT NULL,
    title_normalized TEXT,
    abstract TEXT,
    doi TEXT,
    url TEXT,
    published_at TEXT,
    paper_type TEXT,
    jel_codes TEXT,
    keywords TEXT,
    relevance_score REAL DEFAULT 0,
    first_seen_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_sources (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT,
    raw_metadata TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_normalized TEXT,
    openalex_id TEXT,
    semantic_scholar_id TEXT,
    orcid TEXT,
    affiliation TEXT,
    country TEXT,
    is_tracked INTEGER DEFAULT 0,
    auto_discovered INTEGER DEFAULT 0,
    paper_count INTEGER DEFAULT 0,
    relevance_score REAL DEFAULT 0,
    bluesky_handle TEXT,
    twitter_handle TEXT,
    UNIQUE(name_normalized, affiliation)
);

CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id INTEGER REFERENCES papers(id),
    author_id INTEGER REFERENCES authors(id),
    position INTEGER,
    PRIMARY KEY(paper_id, author_id)
);

CREATE TABLE IF NOT EXISTS social_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    author_handle TEXT,
    content TEXT,
    url TEXT,
    paper_id INTEGER REFERENCES papers(id),
    engagement_score REAL,
    published_at TEXT,
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS deadlines (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    organization TEXT,
    deadline_date TEXT,
    event_date TEXT,
    url TEXT,
    description TEXT,
    relevance_score REAL DEFAULT 0,
    notified_days TEXT,
    UNIQUE(name, deadline_date)
);

CREATE TABLE IF NOT EXISTS sensor_runs (
    id INTEGER PRIMARY KEY,
    sensor TEXT,
    watch TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    items_found INTEGER,
    items_new INTEGER,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_title_norm ON papers(title_normalized);
CREATE INDEX IF NOT EXISTS idx_papers_relevance ON papers(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_authors_tracked ON authors(is_tracked);
CREATE INDEX IF NOT EXISTS idx_deadlines_date ON deadlines(deadline_date);
"""

_initialized: bool = False


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    """Return a connection with WAL journal mode and foreign keys enabled.

    Retries up to 3 times with short backoff on transient open failures
    (e.g. macOS advisory-lock contention on the WAL shared-memory file).

    Returns:
        A configured sqlite3.Connection with row_factory set to sqlite3.Row.
    """
    global _initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            if not _initialized:
                _init_schema(conn)
                _initialized = True
            return conn
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def init_db() -> None:
    """Create all tables and indexes if they do not already exist.

    Safe to call multiple times (uses IF NOT EXISTS guards).
    """
    global _initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    conn.close()
    _initialized = True


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str | None:
    """Serialize value to JSON string, or None if value is None."""
    if value is None:
        return None
    return json.dumps(value)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Convert a sqlite3.Row to a plain dict, or None if row is None."""
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _deserialize_paper(paper: dict) -> dict:
    """Decode JSON-encoded fields in a paper dict in place."""
    for field in ("jel_codes", "keywords"):
        if paper.get(field) and isinstance(paper[field], str):
            paper[field] = json.loads(paper[field])
    return paper


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------


def insert_paper(paper: dict) -> int:
    """Insert a paper record and return its id.

    On canonical_id conflict the existing row is left untouched and its id
    is returned (INSERT OR IGNORE semantics).

    Args:
        paper: Dict with keys matching the papers table columns.
               jel_codes and keywords may be lists; they are serialized to JSON.

    Returns:
        The paper id (existing or newly created).
    """
    jel = _dumps(paper.get("jel_codes"))
    kw = _dumps(paper.get("keywords"))

    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO papers
                (canonical_id, title, title_normalized, abstract, doi, url,
                 published_at, paper_type, jel_codes, keywords)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper.get("canonical_id"),
                paper["title"],
                paper.get("title_normalized"),
                paper.get("abstract"),
                paper.get("doi"),
                paper.get("url"),
                paper.get("published_at"),
                paper.get("paper_type"),
                jel,
                kw,
            ),
        )
        conn.commit()
        if cur.lastrowid:
            return cur.lastrowid

        # Row already existed; fetch its id.
        row = conn.execute(
            "SELECT id FROM papers WHERE canonical_id = ?",
            (paper.get("canonical_id"),),
        ).fetchone()
        return row["id"] if row else 0
    finally:
        conn.close()


def find_paper_by_doi(doi: str) -> dict | None:
    """Return the paper with the given DOI, or None if not found.

    Args:
        doi: The DOI string to search for.

    Returns:
        Paper as a plain dict with jel_codes/keywords deserialized, or None.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    conn.close()
    if row is None:
        return None
    return _deserialize_paper(_row_to_dict(row))


def find_paper_by_normalized_title(title_norm: str) -> list[dict]:
    """Return all papers whose title_normalized matches exactly.

    Args:
        title_norm: Pre-normalized title string (exact match).

    Returns:
        List of paper dicts (may be empty).
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM papers WHERE title_normalized = ?", (title_norm,)
    ).fetchall()
    conn.close()
    return [_deserialize_paper(_row_to_dict(r)) for r in rows]


def update_paper_relevance(paper_id: int, score: float) -> None:
    """Set relevance_score for a paper.

    Args:
        paper_id: Primary key of the paper to update.
        score: New relevance score.
    """
    conn = get_db()
    with conn:
        conn.execute(
            "UPDATE papers SET relevance_score = ? WHERE id = ?",
            (score, paper_id),
        )
    conn.close()


# ---------------------------------------------------------------------------
# Paper Sources
# ---------------------------------------------------------------------------


def insert_paper_source(
    paper_id: int,
    source: str,
    source_id: str,
    source_url: str | None = None,
    raw_metadata: dict | None = None,
) -> int:
    """Record a source for a paper (INSERT OR IGNORE on duplicate source/source_id).

    Args:
        paper_id: FK to papers.id.
        source: Source name (e.g. "arxiv", "semantic_scholar").
        source_id: Identifier within that source.
        source_url: Direct URL at the source.
        raw_metadata: Arbitrary metadata dict; serialized to JSON.

    Returns:
        The paper_source id, or 0 if the record already existed.
    """
    conn = get_db()
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_sources
                (paper_id, source, source_id, source_url, raw_metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (paper_id, source, source_id, source_url, _dumps(raw_metadata)),
        )
    result = cur.lastrowid or 0
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------


def upsert_author(name: str, name_normalized: str, **kwargs: Any) -> int:
    """Insert an author or update non-null fields on conflict.

    Conflict key is (name_normalized, affiliation).

    Args:
        name: Display name.
        name_normalized: Normalized form used for deduplication.
        **kwargs: Optional fields: openalex_id, semantic_scholar_id, orcid,
                  affiliation, country, is_tracked, auto_discovered,
                  bluesky_handle, twitter_handle, relevance_score.

    Returns:
        Author id.
    """
    affiliation = kwargs.get("affiliation")
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO authors
                (name, name_normalized, openalex_id, semantic_scholar_id, orcid,
                 affiliation, country, is_tracked, auto_discovered,
                 bluesky_handle, twitter_handle, relevance_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_normalized, affiliation) DO UPDATE SET
                openalex_id         = COALESCE(excluded.openalex_id, openalex_id),
                semantic_scholar_id = COALESCE(excluded.semantic_scholar_id, semantic_scholar_id),
                orcid               = COALESCE(excluded.orcid, orcid),
                country             = COALESCE(excluded.country, country),
                is_tracked          = COALESCE(excluded.is_tracked, is_tracked),
                auto_discovered     = COALESCE(excluded.auto_discovered, auto_discovered),
                bluesky_handle      = COALESCE(excluded.bluesky_handle, bluesky_handle),
                twitter_handle      = COALESCE(excluded.twitter_handle, twitter_handle),
                relevance_score     = COALESCE(excluded.relevance_score, relevance_score)
            """,
            (
                name,
                name_normalized,
                kwargs.get("openalex_id"),
                kwargs.get("semantic_scholar_id"),
                kwargs.get("orcid"),
                affiliation,
                kwargs.get("country"),
                kwargs.get("is_tracked"),
                kwargs.get("auto_discovered"),
                kwargs.get("bluesky_handle"),
                kwargs.get("twitter_handle"),
                kwargs.get("relevance_score"),
            ),
        )

    row = conn.execute(
        "SELECT id FROM authors WHERE name_normalized = ? AND affiliation IS ?",
        (name_normalized, affiliation),
    ).fetchone()
    conn.close()
    return row["id"] if row else 0


def link_paper_author(paper_id: int, author_id: int, position: int) -> None:
    """Associate an author with a paper at a given position (INSERT OR IGNORE).

    Args:
        paper_id: FK to papers.id.
        author_id: FK to authors.id.
        position: 0-based or 1-based ordering within the author list.
    """
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_authors (paper_id, author_id, position)
            VALUES (?, ?, ?)
            """,
            (paper_id, author_id, position),
        )
    conn.close()


def get_tracked_authors() -> list[dict]:
    """Return all authors with is_tracked = 1.

    Returns:
        List of author dicts.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM authors WHERE is_tracked = 1"
    ).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Social Items
# ---------------------------------------------------------------------------


def insert_social_item(item: dict) -> int:
    """Insert a social media item (INSERT OR IGNORE on source/source_id).

    Args:
        item: Dict with keys: source, source_id, author_handle, content, url,
              paper_id, engagement_score, published_at.

    Returns:
        The social_item id, or 0 if the record already existed.
    """
    conn = get_db()
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO social_items
                (source, source_id, author_handle, content, url,
                 paper_id, engagement_score, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["source"],
                item["source_id"],
                item.get("author_handle"),
                item.get("content"),
                item.get("url"),
                item.get("paper_id"),
                item.get("engagement_score"),
                item.get("published_at"),
            ),
        )
    result = cur.lastrowid or 0
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


def upsert_deadline(deadline: dict) -> int:
    """Insert or update a deadline record.

    Conflict key is (name, deadline_date).

    Args:
        deadline: Dict with keys: type, name, organization, deadline_date,
                  event_date, url, description, relevance_score.
                  notified_days may be a list; it is serialized to JSON.

    Returns:
        The deadline id.
    """
    notified = _dumps(deadline.get("notified_days"))
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO deadlines
                (type, name, organization, deadline_date, event_date,
                 url, description, relevance_score, notified_days)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, deadline_date) DO UPDATE SET
                type            = excluded.type,
                organization    = COALESCE(excluded.organization, organization),
                event_date      = COALESCE(excluded.event_date, event_date),
                url             = COALESCE(excluded.url, url),
                description     = COALESCE(excluded.description, description),
                relevance_score = excluded.relevance_score,
                notified_days   = COALESCE(excluded.notified_days, notified_days)
            """,
            (
                deadline["type"],
                deadline["name"],
                deadline.get("organization"),
                deadline.get("deadline_date"),
                deadline.get("event_date"),
                deadline.get("url"),
                deadline.get("description"),
                deadline.get("relevance_score", 0),
                notified,
            ),
        )

    row = conn.execute(
        "SELECT id FROM deadlines WHERE name = ? AND deadline_date IS ?",
        (deadline["name"], deadline.get("deadline_date")),
    ).fetchone()
    conn.close()
    return row["id"] if row else 0


def get_upcoming_deadlines(days: int = 60) -> list[dict]:
    """Return deadlines with deadline_date within the next N days.

    Args:
        days: Window size in calendar days from today (inclusive).

    Returns:
        List of deadline dicts sorted by deadline_date ascending.
    """
    today = datetime.now(timezone.utc).date()
    cutoff = (today + timedelta(days=days)).isoformat()
    today_str = today.isoformat()

    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM deadlines
        WHERE deadline_date >= ? AND deadline_date <= ?
        ORDER BY deadline_date ASC
        """,
        (today_str, cutoff),
    ).fetchall()
    conn.close()

    result = []
    for row in rows:
        d = _row_to_dict(row)
        if d.get("notified_days") and isinstance(d["notified_days"], str):
            d["notified_days"] = json.loads(d["notified_days"])
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Sensor Runs
# ---------------------------------------------------------------------------


def log_sensor_start(sensor: str, watch: str) -> int:
    """Record the start of a sensor run with status 'running'.

    Args:
        sensor: Sensor name (e.g. "arxiv", "semantic_scholar").
        watch: Watch/filter identifier.

    Returns:
        The sensor_run id.
    """
    conn = get_db()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO sensor_runs (sensor, watch, started_at, status)
            VALUES (?, ?, ?, 'running')
            """,
            (sensor, watch, _now_iso()),
        )
    result = cur.lastrowid
    conn.close()
    return result


def log_sensor_end(
    run_id: int,
    status: str,
    items_found: int,
    items_new: int,
    error_message: str | None = None,
) -> None:
    """Update a sensor run record with its outcome.

    Args:
        run_id: Primary key returned by log_sensor_start.
        status: Final status string (e.g. "success", "error").
        items_found: Total items discovered by the sensor.
        items_new: Items that were not previously in the database.
        error_message: Optional error detail.
    """
    conn = get_db()
    with conn:
        conn.execute(
            """
            UPDATE sensor_runs
            SET finished_at = ?, status = ?, items_found = ?,
                items_new = ?, error_message = ?
            WHERE id = ?
            """,
            (_now_iso(), status, items_found, items_new, error_message, run_id),
        )
    conn.close()


def get_last_sensor_run(sensor: str) -> dict | None:
    """Return the most recent sensor_run record for the given sensor.

    Args:
        sensor: Sensor name to query.

    Returns:
        Sensor run as a plain dict, or None if no runs exist.
    """
    conn = get_db()
    row = conn.execute(
        """
        SELECT * FROM sensor_runs
        WHERE sensor = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (sensor,),
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Query Helpers
# ---------------------------------------------------------------------------


def get_recent_papers(days: int = 1, limit: int = 50) -> list[dict]:
    """Return papers first seen in the last N days, highest relevance first.

    Each paper dict includes an 'authors' key with a list of author dicts
    (name, position).

    Args:
        days: Look-back window in calendar days.
        limit: Maximum number of papers to return.

    Returns:
        List of paper dicts with jel_codes/keywords deserialized.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM papers
        WHERE first_seen_at >= ?
        ORDER BY relevance_score DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()

    papers = []
    for row in rows:
        paper = _deserialize_paper(_row_to_dict(row))
        author_rows = conn.execute(
            """
            SELECT a.id, a.name, a.affiliation, pa.position
            FROM authors a
            JOIN paper_authors pa ON pa.author_id = a.id
            WHERE pa.paper_id = ?
            ORDER BY pa.position
            """,
            (paper["id"],),
        ).fetchall()
        paper["authors"] = _rows_to_dicts(author_rows)
        papers.append(paper)

    conn.close()
    return papers


def get_paper_with_sources(paper_id: int) -> dict:
    """Return a paper with all its sources and authors.

    Args:
        paper_id: Primary key of the paper.

    Returns:
        Paper dict with 'sources' (list of source dicts) and
        'authors' (list of author dicts with position).

    Raises:
        KeyError: If no paper with the given id exists.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        conn.close()
        raise KeyError(f"No paper with id={paper_id}")

    paper = _deserialize_paper(_row_to_dict(row))

    source_rows = conn.execute(
        "SELECT * FROM paper_sources WHERE paper_id = ?", (paper_id,)
    ).fetchall()
    sources = []
    for s in source_rows:
        sd = _row_to_dict(s)
        if sd.get("raw_metadata") and isinstance(sd["raw_metadata"], str):
            sd["raw_metadata"] = json.loads(sd["raw_metadata"])
        sources.append(sd)
    paper["sources"] = sources

    author_rows = conn.execute(
        """
        SELECT a.*, pa.position
        FROM authors a
        JOIN paper_authors pa ON pa.author_id = a.id
        WHERE pa.paper_id = ?
        ORDER BY pa.position
        """,
        (paper_id,),
    ).fetchall()
    paper["authors"] = _rows_to_dicts(author_rows)

    conn.close()
    return paper


def get_recent_social_items(days: int = 1, limit: int = 50) -> list[dict]:
    """Return social items from the last N days, most recent first.

    Args:
        days: Look-back window in calendar days.
        limit: Maximum number of items to return.

    Returns:
        List of social_item dicts.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM social_items
        WHERE published_at >= ?
        ORDER BY published_at DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("DB initialized")
