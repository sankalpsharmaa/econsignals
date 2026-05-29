"""Database layer for EconSignals.

SQLite-backed persistence with WAL mode and foreign key enforcement.
Tables: papers, paper_sources, authors, paper_authors, social_items,
        deadlines, sensor_runs.

Author identity is keyed on ``name_normalized`` alone (one row per person,
regardless of how affiliation varies across sources). Rolling deadlines store
``deadline_date = ''`` (empty-string sentinel, never NULL) so the UNIQUE
constraint and ON CONFLICT dedup actually fire. A one-time ``_migrate`` pass
(guarded by ``PRAGMA user_version``) collapses legacy duplicate rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# File lives at:  <proj_root>/econsignals/lib/db.py
# parents[0] = lib, parents[1] = econsignals, parents[2] = project root
PROJ_ROOT: Path = Path(__file__).resolve().parents[2]

DB_PATH: Path = Path(
    os.environ.get("ECONSIGNALS_DB", str(PROJ_ROOT / "data" / "econsignals.db"))
)

# Bump when a new _migrate step is added.
_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Note: the authors UNIQUE key is created as a UNIQUE INDEX inside _migrate
# (after legacy duplicates are collapsed), not as a table constraint, so the
# migration can run on a database that still contains duplicates.
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
    affiliation TEXT DEFAULT '',
    country TEXT,
    is_tracked INTEGER DEFAULT 0,
    auto_discovered INTEGER DEFAULT 0,
    paper_count INTEGER DEFAULT 0,
    relevance_score REAL DEFAULT 0,
    bluesky_handle TEXT,
    twitter_handle TEXT
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
    deadline_date TEXT DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published_at);
CREATE INDEX IF NOT EXISTS idx_papers_first_seen ON papers(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_authors_tracked ON authors(is_tracked);
CREATE INDEX IF NOT EXISTS idx_deadlines_date ON deadlines(deadline_date);
CREATE INDEX IF NOT EXISTS idx_social_published ON social_items(published_at);
"""

_initialized: bool = False


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    """Return a connection with WAL journal mode and foreign keys enabled.

    Retries up to 3 times with short backoff on transient open failures
    (e.g. macOS advisory-lock contention on the WAL shared-memory file).
    On a failed attempt the partially-opened connection is closed so it does
    not leak a WAL/SHM lock into the next retry.

    Returns:
        A configured sqlite3.Connection with row_factory set to sqlite3.Row.
    """
    global _initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(3):
        conn: sqlite3.Connection | None = None
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
            if conn is not None:
                conn.close()
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a DB connection and guarantee it is closed.

    Every persistence helper uses this so a raised exception cannot leak a
    connection (and its WAL lock).
    """
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables and indexes if they do not already exist.

    Safe to call multiple times (uses IF NOT EXISTS guards) and runs any
    pending data migrations.
    """
    global _initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(conn)
    finally:
        conn.close()
    _initialized = True


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()
    _migrate(conn)


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def _migrate(conn: sqlite3.Connection) -> None:
    """Run pending one-time data migrations, guarded by PRAGMA user_version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        _migrate_v1_collapse_duplicates(conn)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()


def _migrate_v1_collapse_duplicates(conn: sqlite3.Connection) -> None:
    """Collapse legacy duplicate authors and rolling deadlines.

    Historically authors were keyed on (name_normalized, affiliation); because
    NULL never equals NULL in SQLite, every NULL-affiliation author was
    re-inserted on each sensor run (~5.3k duplicate rows). Likewise rolling
    deadlines stored NULL dates that defeated dedup. This collapses both to one
    canonical row, repoints foreign keys, and installs a unique index on
    authors(name_normalized) for ON CONFLICT to target.
    """
    # Collapse duplicate authors FIRST, while NULL affiliations are still
    # distinct under the legacy UNIQUE(name_normalized, affiliation) constraint.
    # Filling NULLs with '' before dedup would collide on the duplicates.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_authors_nn_tmp ON authors(name_normalized)")

    dup_groups = conn.execute(
        """
        SELECT name_normalized, MIN(id) AS keep_id
        FROM authors
        WHERE name_normalized IS NOT NULL AND name_normalized <> ''
        GROUP BY name_normalized
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in dup_groups:
        keep_id = group["keep_id"]
        name_norm = group["name_normalized"]
        # preserve a real affiliation if the survivor lacks one
        best_affil = conn.execute(
            "SELECT affiliation FROM authors "
            "WHERE name_normalized = ? AND affiliation IS NOT NULL AND affiliation <> '' "
            "ORDER BY LENGTH(affiliation) DESC LIMIT 1",
            (name_norm,),
        ).fetchone()
        dup_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM authors WHERE name_normalized = ? AND id <> ?",
                (name_norm, keep_id),
            ).fetchall()
        ]
        for dup_id in dup_ids:
            # repoint links to the survivor, dropping rows that would collide
            conn.execute(
                "UPDATE OR IGNORE paper_authors SET author_id = ? WHERE author_id = ?",
                (keep_id, dup_id),
            )
            conn.execute("DELETE FROM paper_authors WHERE author_id = ?", (dup_id,))
            conn.execute("DELETE FROM authors WHERE id = ?", (dup_id,))
        # survivor is now alone for this name; setting affiliation cannot collide
        if best_affil and best_affil["affiliation"]:
            conn.execute(
                "UPDATE authors SET affiliation = ? WHERE id = ? "
                "AND (affiliation IS NULL OR affiliation = '')",
                (best_affil["affiliation"], keep_id),
            )

    # now safe to sentinel-fill remaining NULL affiliations (one row per name)
    conn.execute("UPDATE authors SET affiliation = '' WHERE affiliation IS NULL")

    conn.execute("DROP INDEX IF EXISTS idx_authors_nn_tmp")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_authors_name_norm ON authors(name_normalized)"
    )

    # Collapse rolling deadlines: dedup by name FIRST (NULL dates group together
    # in SQLite GROUP BY), then sentinel-fill so the UNIQUE key fires going forward.
    dl_groups = conn.execute(
        """
        SELECT name, deadline_date, MIN(id) AS keep_id
        FROM deadlines
        GROUP BY name, deadline_date
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in dl_groups:
        if group["deadline_date"] is None:
            conn.execute(
                "DELETE FROM deadlines WHERE name = ? AND deadline_date IS NULL AND id <> ?",
                (group["name"], group["keep_id"]),
            )
        else:
            conn.execute(
                "DELETE FROM deadlines WHERE name = ? AND deadline_date = ? AND id <> ?",
                (group["name"], group["deadline_date"], group["keep_id"]),
            )
    conn.execute("UPDATE deadlines SET deadline_date = '' WHERE deadline_date IS NULL")

    conn.commit()


def dedup_authors() -> int:
    """Collapse duplicate authors by name_normalized; return rows removed.

    Idempotent maintenance hook. Run after re-normalizing existing author
    names (e.g. after a normalize.py change), since re-normalization can map
    previously-distinct rows onto the same key.
    """
    with _connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        # drop the unique index so duplicates can be collapsed, then rebuild
        conn.execute("DROP INDEX IF EXISTS idx_authors_name_norm")
        _migrate_v1_collapse_duplicates(conn)
        after = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    return before - after


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


def _cutoff_date(days: int) -> str:
    """Return the YYYY-MM-DD date `days` before today (UTC).

    Used with substr(col, 1, 10) comparisons so look-back filters are immune to
    timestamp-separator differences ('2026-03-12 05:00:00' vs the ISO 'T...Z'
    form). A prior format mismatch silently dropped the newest papers.
    """
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


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
        The paper id (existing or newly created), or 0 if it could not be
        resolved (e.g. canonical_id is NULL and the row was ignored).
    """
    jel = _dumps(paper.get("jel_codes"))
    kw = _dumps(paper.get("keywords"))

    with _connect() as conn:
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
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid

        # Row already existed (or was ignored); resolve its id by canonical_id.
        if paper.get("canonical_id"):
            row = conn.execute(
                "SELECT id FROM papers WHERE canonical_id = ?",
                (paper.get("canonical_id"),),
            ).fetchone()
            return row["id"] if row else 0
        return 0


def find_paper_by_doi(doi: str) -> dict | None:
    """Return the paper with the given DOI, or None if not found."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    if row is None:
        return None
    return _deserialize_paper(_row_to_dict(row))


def find_paper_by_normalized_title(title_norm: str) -> list[dict]:
    """Return all papers whose title_normalized matches exactly."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM papers WHERE title_normalized = ?", (title_norm,)
        ).fetchall()
    return [_deserialize_paper(_row_to_dict(r)) for r in rows]


def update_paper_relevance(paper_id: int, score: float) -> None:
    """Set relevance_score for a paper."""
    with _connect() as conn:
        conn.execute(
            "UPDATE papers SET relevance_score = ? WHERE id = ?",
            (score, paper_id),
        )
        conn.commit()


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

    Returns:
        The paper_source id, or 0 if the record already existed.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_sources
                (paper_id, source, source_id, source_url, raw_metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (paper_id, source, source_id, source_url, _dumps(raw_metadata)),
        )
        conn.commit()
        return cur.lastrowid or 0


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------


def upsert_author(name: str, name_normalized: str, **kwargs: Any) -> int:
    """Insert an author or update non-null fields on conflict.

    Conflict key is ``name_normalized`` alone: one row per person, regardless
    of affiliation variation across sources. Affiliation is treated as a
    mergeable attribute (the longest non-empty value wins).

    Args:
        name: Display name.
        name_normalized: Normalized form used for deduplication.
        **kwargs: Optional fields: openalex_id, semantic_scholar_id, orcid,
                  affiliation, country, is_tracked, auto_discovered,
                  bluesky_handle, twitter_handle, relevance_score.

    Returns:
        Author id.
    """
    affiliation = kwargs.get("affiliation") or ""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO authors
                (name, name_normalized, openalex_id, semantic_scholar_id, orcid,
                 affiliation, country, is_tracked, auto_discovered,
                 bluesky_handle, twitter_handle, relevance_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_normalized) DO UPDATE SET
                openalex_id         = COALESCE(excluded.openalex_id, openalex_id),
                semantic_scholar_id = COALESCE(excluded.semantic_scholar_id, semantic_scholar_id),
                orcid               = COALESCE(excluded.orcid, orcid),
                affiliation         = CASE
                                          WHEN LENGTH(excluded.affiliation) > LENGTH(COALESCE(affiliation, ''))
                                          THEN excluded.affiliation ELSE affiliation END,
                country             = COALESCE(excluded.country, country),
                is_tracked          = MAX(COALESCE(is_tracked, 0), COALESCE(excluded.is_tracked, 0)),
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
        conn.commit()
        row = conn.execute(
            "SELECT id FROM authors WHERE name_normalized = ?",
            (name_normalized,),
        ).fetchone()
        return row["id"] if row else 0


def link_paper_author(paper_id: int, author_id: int, position: int) -> None:
    """Associate an author with a paper at a given position (INSERT OR IGNORE)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_authors (paper_id, author_id, position)
            VALUES (?, ?, ?)
            """,
            (paper_id, author_id, position),
        )
        conn.commit()


def get_tracked_authors() -> list[dict]:
    """Return all authors with is_tracked = 1."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM authors WHERE is_tracked = 1").fetchall()
    return _rows_to_dicts(rows)


def has_tracked_authors() -> bool:
    """Return True if any author is flagged is_tracked.

    The relevance scorer uses this to decide whether author-proximity is a real
    signal: with nobody tracked, auto-discovered co-authorship is pure noise.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM authors WHERE is_tracked = 1 LIMIT 1"
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Social Items
# ---------------------------------------------------------------------------


def insert_social_item(item: dict) -> int:
    """Insert a social media item (INSERT OR IGNORE on source/source_id).

    Returns:
        The social_item id, or 0 if the record already existed.
    """
    with _connect() as conn:
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
        conn.commit()
        return cur.lastrowid or 0


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


def upsert_deadline(deadline: dict) -> int:
    """Insert or update a deadline record.

    Conflict key is (name, deadline_date). Rolling deadlines (no date) are
    stored with deadline_date = '' (never NULL) so they dedup instead of
    re-inserting on every scan.

    Returns:
        The deadline id.
    """
    notified = _dumps(deadline.get("notified_days"))
    deadline_date = deadline.get("deadline_date") or ""
    with _connect() as conn:
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
                deadline_date,
                deadline.get("event_date"),
                deadline.get("url"),
                deadline.get("description"),
                deadline.get("relevance_score", 0),
                notified,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM deadlines WHERE name = ? AND deadline_date = ?",
            (deadline["name"], deadline_date),
        ).fetchone()
        return row["id"] if row else 0


def get_upcoming_deadlines(days: int = 60, include_rolling: bool = True) -> list[dict]:
    """Return dated deadlines within the next N days, plus rolling deadlines.

    Args:
        days: Window size in calendar days from today (inclusive).
        include_rolling: If True, also return rolling deadlines (deadline_date
            = ''), sorted after the dated ones.

    Returns:
        Deadline dicts: dated ones first (ascending), rolling ones last.
    """
    today = datetime.now(timezone.utc).date()
    cutoff = (today + timedelta(days=days)).isoformat()
    today_str = today.isoformat()

    if include_rolling:
        where = (
            "WHERE deadline_date = '' "
            "OR (deadline_date >= ? AND deadline_date <= ?)"
        )
    else:
        where = "WHERE deadline_date >= ? AND deadline_date <= ?"

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM deadlines
            {where}
            ORDER BY (deadline_date = '') ASC, deadline_date ASC
            """,
            (today_str, cutoff),
        ).fetchall()

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

    Returns:
        The sensor_run id.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO sensor_runs (sensor, watch, started_at, status)
            VALUES (?, ?, ?, 'running')
            """,
            (sensor, watch, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid or 0


def log_sensor_end(
    run_id: int,
    status: str,
    items_found: int,
    items_new: int,
    error_message: str | None = None,
) -> None:
    """Update a sensor run record with its outcome."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE sensor_runs
            SET finished_at = ?, status = ?, items_found = ?,
                items_new = ?, error_message = ?
            WHERE id = ?
            """,
            (_now_iso(), status, items_found, items_new, error_message, run_id),
        )
        conn.commit()


def get_last_sensor_run(sensor: str) -> dict | None:
    """Return the most recent sensor_run record for the given sensor."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM sensor_runs
            WHERE sensor = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (sensor,),
        ).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Query Helpers
# ---------------------------------------------------------------------------


def _venue_from_meta(raw: str | None) -> str | None:
    """Extract a human journal/venue name from a source's raw_metadata JSON.

    Handles Crossref (container-title list) and OpenAlex
    (primary_location.source.display_name) shapes; returns None if absent.
    """
    if not raw:
        return None
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    ct = meta.get("container-title")
    if isinstance(ct, list) and ct:
        return ct[0]
    if isinstance(ct, str) and ct:
        return ct
    loc = meta.get("primary_location") or meta.get("host_venue")
    if isinstance(loc, dict):
        src = loc.get("source")
        if isinstance(src, dict) and src.get("display_name"):
            return src["display_name"]
        if loc.get("display_name"):
            return loc["display_name"]
    for key in ("journal", "venue", "journal_name"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _attach_primary_source(conn: sqlite3.Connection, paper: dict) -> None:
    """Set primary_source, primary_venue, and a fallback url on a paper dict.

    Prefers the Crossref source for the venue (it carries the real journal),
    falling back to any source that reports one.
    """
    rows = conn.execute(
        "SELECT source, source_url, raw_metadata FROM paper_sources "
        "WHERE paper_id = ? ORDER BY id",
        (paper["id"],),
    ).fetchall()
    paper["primary_source"] = rows[0]["source"] if rows else None
    venue = None
    for r in sorted(rows, key=lambda r: 0 if r["source"] == "crossref" else 1):
        venue = _venue_from_meta(r["raw_metadata"])
        if venue:
            break
    paper["primary_venue"] = venue
    if not paper.get("url"):
        for r in rows:
            if r["source_url"]:
                paper["url"] = r["source_url"]
                break


def _attach_authors(conn: sqlite3.Connection, paper: dict) -> dict:
    """Attach a deduplicated, position-ordered author list to a paper dict."""
    author_rows = conn.execute(
        """
        SELECT a.id, a.name, a.affiliation, MIN(pa.position) AS position
        FROM authors a
        JOIN paper_authors pa ON pa.author_id = a.id
        WHERE pa.paper_id = ?
        GROUP BY a.name_normalized
        ORDER BY position
        """,
        (paper["id"],),
    ).fetchall()
    paper["authors"] = _rows_to_dicts(author_rows)
    return paper


def get_recent_papers(days: int = 1, limit: int = 50) -> list[dict]:
    """Return papers first seen in the last N days, highest relevance first.

    Each paper dict includes an 'authors' key with a deduplicated list of
    author dicts (name, position).
    """
    cutoff = _cutoff_date(days)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM papers
            WHERE substr(first_seen_at, 1, 10) >= ?
            ORDER BY relevance_score DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        papers = []
        for row in rows:
            paper = _deserialize_paper(_row_to_dict(row))
            papers.append(_attach_authors(conn, paper))
    return papers


def get_top_papers(limit: int = 200, min_score: float = 0.0) -> list[dict]:
    """Return the highest-relevance papers regardless of age.

    Used by the dashboard/snapshot builder, which should surface the best
    papers even when the most recent scan is stale. Each paper gets a
    deduplicated 'authors' list and a 'primary_source' string.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM papers
            WHERE relevance_score >= ?
            ORDER BY relevance_score DESC, published_at DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()
        papers = []
        for row in rows:
            paper = _deserialize_paper(_row_to_dict(row))
            _attach_authors(conn, paper)
            _attach_primary_source(conn, paper)
            papers.append(paper)
    return papers


def get_paper_with_sources(paper_id: int) -> dict:
    """Return a paper with all its sources and authors.

    Raises:
        KeyError: If no paper with the given id exists.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        if row is None:
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
        _attach_authors(conn, paper)
    return paper


def get_recent_social_items(days: int = 1, limit: int = 50) -> list[dict]:
    """Return social items from the last N days, most recent first."""
    cutoff = _cutoff_date(days)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM social_items
            WHERE substr(published_at, 1, 10) >= ?
            ORDER BY published_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("DB initialized")
