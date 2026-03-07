# ruff: noqa: E402  (imports below the bootstrap block are intentional)
"""
dedup.py
--------
Cross-source paper deduplication for EconSignals.

The same paper can arrive from NBER, Semantic Scholar, OpenAlex, and RePEc.
decides whether an incoming paper already exists in the database and, if so,
merges any richer metadata from the new record before logging the additional
source.

Deduplication cascade (find_existing_paper):
    1. DOI exact match  (~60 % of duplicates)
    2. Exact normalized-title match
    3. Fuzzy title match: longest-word LIKE filter + Jaccard >= 0.85
    4. Author-overlap fallback: first-author last-name LIKE filter +
       Jaccard >= 0.70 + at least 2 shared author last names

All DB access goes through lib/db.py.  No raw SQL is written here except for
the LIKE candidate queries in steps 3 and 4, which require ad-hoc filtering
that the db module's narrow helpers cannot provide.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Bootstrap for direct execution: python3 lib/dedup.py
# ---------------------------------------------------------------------------
# Relative imports fail when this file is the __main__ module.  We detect
# that case, add the package root to sys.path, and re-invoke this module
# under its dotted package name via runpy so that relative imports resolve.
# The _DEDUP_SMOKETEST sentinel prevents infinite recursion.
import os as _os
import sys as _sys

if __name__ == "__main__" and not _os.environ.get("_DEDUP_SMOKETEST"):
    import pathlib as _pathlib
    import runpy as _runpy

    # .../econsignals/.claude/skills  <- the directory that contains the
    # 'econsignals' package folder
    _root = str(_pathlib.Path(__file__).resolve().parent.parent.parent)
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    _os.environ["_DEDUP_SMOKETEST"] = "1"
    _runpy.run_module("econsignals.lib.dedup", run_name="__main__", alter_sys=True)
    _sys.exit(0)

import json
import sqlite3
from typing import Any

from .db import (
    find_paper_by_doi,
    find_paper_by_normalized_title,
    get_db,
    insert_paper,
    insert_paper_source,
    link_paper_author,
    upsert_author,
)
from .normalize import (
    author_lastnames_overlap,
    canonical_paper_id,
    extract_last_name,
    jaccard_similarity,
    normalize_author_name as _normalize_author_name,
    normalize_title,
    title_token_set,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JACCARD_FUZZY_THRESHOLD = 0.85
_JACCARD_AUTHOR_THRESHOLD = 0.70
_AUTHOR_OVERLAP_MIN = 2

# Minimum character length for a title word to be used as a LIKE anchor.
# Short words (stop-words like 'the', 'and') match too many rows.
_MIN_ANCHOR_WORD_LEN = 4


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _longest_word(token_set: set[str]) -> str | None:
    """Return the longest token >= _MIN_ANCHOR_WORD_LEN chars, or None.

    The longest word is the most selective anchor for a LIKE query.

    Args:
        token_set: Set of normalized title tokens.

    Returns:
        Longest qualifying token, or None if all tokens are too short.
    """
    candidates = [t for t in token_set if len(t) >= _MIN_ANCHOR_WORD_LEN]
    if not candidates:
        return None
    return max(candidates, key=len)


def _fetch_candidates_by_title_word(anchor: str) -> list[dict[str, Any]]:
    """Fetch papers whose normalized title contains *anchor* as a substring.

    This is the only place in this module that runs raw SQL directly.  We do
    it here because find_paper_by_normalized_title performs an exact lookup and
    the db module has no LIKE-search helper.

    Args:
        anchor: A lowercase word expected to appear in title_normalized.

    Returns:
        List of paper row dicts from the papers table.
    """
    conn: sqlite3.Connection = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM papers WHERE title_normalized LIKE ?",
        (f"%{anchor}%",),
    )
    results = [dict(row) for row in cur.fetchall()]
    conn.close()
    return results


def _fetch_candidates_by_author_lastname(last_name: str) -> list[dict[str, Any]]:
    """Fetch papers linked to an author whose normalized name contains *last_name*.

    Joins papers -> paper_authors -> authors so we filter by author name rather
    than embedding the last name in the title field.

    Args:
        last_name: Lowercase, accent-stripped last name to search for.

    Returns:
        Distinct paper row dicts.  An author appearing multiple times on the
        same paper produces only one row thanks to SELECT DISTINCT.
    """
    conn: sqlite3.Connection = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT DISTINCT p.*
          FROM papers p
          JOIN paper_authors pa ON pa.paper_id = p.id
          JOIN authors a ON a.id = pa.author_id
         WHERE a.name_normalized LIKE ?
        """,
        (f"%{last_name}%",),
    )
    results = [dict(row) for row in cur.fetchall()]
    conn.close()
    return results


def _fetch_author_names_for_paper(paper_id: int) -> list[str]:
    """Fetch author display names for a paper from the authors table.

    Args:
        paper_id: Primary key of the paper.

    Returns:
        List of author name strings, ordered by position.
    """
    conn: sqlite3.Connection = get_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT a.name
              FROM authors a
              JOIN paper_authors pa ON pa.author_id = a.id
             WHERE pa.paper_id = ?
             ORDER BY pa.position
            """,
            (paper_id,),
        ).fetchall()
        return [row["name"] for row in rows if row["name"]]
    finally:
        conn.close()


def _parse_authors_field(row: dict[str, Any]) -> list[str]:
    """Normalize the authors field from a paper row into a list of name strings.

    The papers table may store authors as a JSON array or a pipe-delimited
    string depending on how insert_paper was called.

    Args:
        row: A paper row dict from the database.

    Returns:
        List of author name strings, possibly empty.
    """
    raw = row.get("authors") or row.get("author_names") or ""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except json.JSONDecodeError:
                pass
        if "|" in stripped:
            return [s.strip() for s in stripped.split("|") if s.strip()]
        if stripped:
            return [stripped]
    return []


def _deserialize_list_field(value: Any) -> list[str]:
    """Normalize a DB field that may be a list, JSON string, or comma-separated string.

    Args:
        value: Raw field value from the database.

    Returns:
        List of strings.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except json.JSONDecodeError:
                pass
        return [v.strip() for v in stripped.split(",") if v.strip()]
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_existing_paper(
    title: str,
    authors: list[str],
    doi: str | None = None,
) -> int | None:
    """Cascading lookup to find whether a paper already exists in the database.

    The cascade applies four strategies in order of increasing cost:

    1. **DOI exact match** (~60 % of duplicates):
       If *doi* is provided, delegate to find_paper_by_doi().  A hit returns
       immediately without running the title strategies.

    2. **Exact normalized-title match**:
       Normalize the incoming title and query find_paper_by_normalized_title().
       A single result is returned directly.  Multiple results (short generic
       titles) fall through to the Jaccard filter.

    3. **Fuzzy title match** (Jaccard >= 0.85):
       Pick the longest significant word from the title token set and run a
       LIKE candidate query on title_normalized.  Each candidate is scored by
       Jaccard similarity against the incoming token set.  The first candidate
       that clears 0.85 is returned.

    4. **Author-overlap fallback** (Jaccard >= 0.70 + 2+ shared last names):
       Use the first author's last name as a LIKE anchor against the authors
       table.  Candidates that share at least two author last names *and*
       achieve title Jaccard >= 0.70 are returned.  This catches cases where
       the title differs between sources (e.g. NBER adds a subtitle another source omits).

    Args:
        title:   Raw paper title from the incoming sensor payload.
        authors: List of raw author name strings.
        doi:     Optional DOI string (e.g. "10.3386/w31705").

    Returns:
        Integer primary key of the matching papers row, or None.
    """
    # 1. DOI exact match
    if doi:
        existing = find_paper_by_doi(doi)
        if existing:
            return existing["id"]

    title_norm = normalize_title(title)
    tokens = title_token_set(title)

    # 2. Exact normalized-title match
    candidates = find_paper_by_normalized_title(title_norm)
    if len(candidates) == 1:
        return candidates[0]["id"]
    if len(candidates) > 1:
        best_id: int | None = None
        best_score = 0.0
        for row in candidates:
            score = jaccard_similarity(tokens, title_token_set(row.get("title", "")))
            if score > best_score:
                best_score = score
                best_id = row["id"]
        if best_id is not None:
            return best_id

    # 3. Fuzzy title match via LIKE + Jaccard >= 0.85
    anchor = _longest_word(tokens)
    if anchor:
        for row in _fetch_candidates_by_title_word(anchor):
            score = jaccard_similarity(tokens, title_token_set(row.get("title", "")))
            if score >= _JACCARD_FUZZY_THRESHOLD:
                return row["id"]

    # 4. Author-overlap fallback (Jaccard >= 0.70 + 2+ shared last names)
    if authors:
        first_last = extract_last_name(authors[0])
        if first_last:
            for row in _fetch_candidates_by_author_lastname(first_last):
                title_score = jaccard_similarity(tokens, title_token_set(row.get("title", "")))
                if title_score < _JACCARD_AUTHOR_THRESHOLD:
                    continue
                existing_authors = _fetch_author_names_for_paper(row["id"])
                if author_lastnames_overlap(authors, existing_authors) >= _AUTHOR_OVERLAP_MIN:
                    return row["id"]

    return None


def merge_paper_metadata(
    existing: dict[str, Any],
    new_data: dict[str, Any],
) -> dict[str, Any]:
    """Compute a minimal update dict by merging *new_data* into *existing*.

    Merge rules:

    - **doi**: prefer the version that has one.
    - **abstract**: prefer the longer string.
    - **paper_type**: 'journal_article' > 'working_paper' > anything else.
    - **jel_codes**: order-preserving union, de-duplicated.
    - **keywords**: order-preserving union, de-duplicated (case-insensitive).
    - **published_at**: keep the earliest ISO date (lexicographic comparison
      works correctly for ISO 8601 strings including partial dates like '2023').

    Only fields that differ from *existing* are included in the returned dict,
    so the caller can issue a minimal UPDATE.

    Args:
        existing: Current paper record as a dict (from the DB).
        new_data: Incoming paper data dict (from the sensor payload).

    Returns:
        Dict of {field: new_value} for fields that should be updated.
        Empty dict means no update is needed.
    """
    updates: dict[str, Any] = {}

    # doi: prefer whichever has one
    if not existing.get("doi") and new_data.get("doi"):
        updates["doi"] = new_data["doi"]

    # abstract: prefer the longer one
    existing_abstract = existing.get("abstract") or ""
    new_abstract = new_data.get("abstract") or ""
    if len(new_abstract) > len(existing_abstract):
        updates["abstract"] = new_abstract

    # paper_type: journal_article > working_paper > anything else
    _type_rank: dict[str, int] = {"journal_article": 2, "working_paper": 1}
    existing_rank = _type_rank.get(existing.get("paper_type") or "", 0)
    new_rank = _type_rank.get(new_data.get("paper_type") or "", 0)
    if new_rank > existing_rank:
        updates["paper_type"] = new_data["paper_type"]

    # jel_codes: order-preserving union
    existing_jel = _deserialize_list_field(existing.get("jel_codes"))
    new_jel: list[str] = new_data.get("jel_codes") or []
    existing_jel_set = set(existing_jel)
    merged_jel = existing_jel + [j for j in new_jel if j not in existing_jel_set]
    if len(merged_jel) != len(existing_jel):
        updates["jel_codes"] = merged_jel

    # keywords: order-preserving union, case-insensitive de-duplication
    existing_kw = _deserialize_list_field(existing.get("keywords"))
    new_kw: list[str] = new_data.get("keywords") or []
    existing_kw_lower = {k.lower() for k in existing_kw}
    merged_kw = existing_kw + [k for k in new_kw if k.lower() not in existing_kw_lower]
    if len(merged_kw) != len(existing_kw):
        updates["keywords"] = merged_kw

    # published_at: keep earliest (ISO string prefix ordering)
    existing_date: str | None = existing.get("published_at")
    new_date: str | None = new_data.get("published_at")
    if new_date and existing_date:
        if str(new_date) < str(existing_date):
            updates["published_at"] = new_date
    elif new_date and not existing_date:
        updates["published_at"] = new_date

    return updates


def _apply_metadata_updates(paper_id: int, updates: dict[str, Any]) -> None:
    """Write *updates* to the papers row identified by *paper_id*.

    List-valued fields are serialized to JSON before writing.

    Args:
        paper_id: Primary key of the paper to update.
        updates:  Dict produced by merge_paper_metadata().
    """
    if not updates:
        return

    conn: sqlite3.Connection = get_db()
    serialized: dict[str, Any] = {
        k: json.dumps(v) if isinstance(v, list) else v
        for k, v in updates.items()
    }
    set_clause = ", ".join(f"{col} = ?" for col in serialized)
    conn.execute(
        f"UPDATE papers SET {set_clause} WHERE id = ?",
        [*serialized.values(), paper_id],
    )
    conn.commit()
    conn.close()


def ingest_paper(
    paper_data: dict[str, Any],
    source: str,
    source_id: str,
    source_url: str | None = None,
    raw_metadata: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Main entry point for adding a paper from any sensor.

    Handles deduplication, metadata merging, source linking, and author
    upsertion in a single call.  Sensors call this once per paper instead of
    writing to the DB directly.

    paper_data keys
    ---------------
    Required:
        title (str)
        authors (list[str])
    Optional:
        abstract (str)
        doi (str)
        url (str)
        published_at (str, ISO 8601 date, e.g. '2023-09' or '2023-09-15')
        paper_type (str, 'working_paper' | 'journal_article')
        jel_codes (list[str])
        keywords (list[str])
        author_affiliations (list[str], parallel to authors)
        author_ids (list[dict], parallel to authors; each dict may contain
                    'openalex_id', 'semantic_scholar_id', 'orcid')

    Args:
        paper_data:   Normalized payload from the sensor.
        source:       Source identifier, e.g. 'nber', 'semantic_scholar', 'openalex', 'repec'.
        source_id:    The source's own identifier for this paper.
        source_url:   Optional canonical URL at the source.
        raw_metadata: Optional full raw response for audit purposes.

    Returns:
        (paper_id, is_new) where *is_new* is True when a new row was inserted.
    """
    title: str = paper_data.get("title", "")
    authors: list[str] = paper_data.get("authors") or []
    doi: str | None = paper_data.get("doi") or None

    # 1. Normalize title and compute canonical ID
    title_norm = normalize_title(title)
    canonical_id = canonical_paper_id(title, authors)

    # 2. Deduplication check
    existing_id = find_existing_paper(title, authors, doi=doi)

    if existing_id is not None:
        # 3a. Existing paper: log the new source and merge any richer metadata
        print(f"[dedup] duplicate: paper_id={existing_id} source={source} source_id={source_id}")
        insert_paper_source(
            paper_id=existing_id,
            source=source,
            source_id=source_id,
            source_url=source_url,
            raw_metadata=raw_metadata,
        )
        conn: sqlite3.Connection = get_db()
        conn.row_factory = sqlite3.Row
        db_row = conn.execute("SELECT * FROM papers WHERE id = ?", (existing_id,)).fetchone()
        conn.close()
        if db_row:
            updates = merge_paper_metadata(dict(db_row), paper_data)
            if updates:
                print(f"[dedup] merging {list(updates.keys())} into paper_id={existing_id}")
                _apply_metadata_updates(existing_id, updates)
        paper_id = existing_id
        is_new = False

    else:
        # 3b. New paper: insert row then log the source
        insert_payload: dict[str, Any] = {
            "title": title,
            "title_normalized": title_norm,
            "canonical_id": canonical_id,
            "abstract": paper_data.get("abstract"),
            "doi": doi,
            "url": paper_data.get("url"),
            "published_at": paper_data.get("published_at"),
            "paper_type": paper_data.get("paper_type"),
            "jel_codes": paper_data.get("jel_codes"),
            "keywords": paper_data.get("keywords"),
        }
        paper_id = insert_paper(insert_payload)
        print(f"[dedup] new paper: paper_id={paper_id} source={source} source_id={source_id}")
        insert_paper_source(
            paper_id=paper_id,
            source=source,
            source_id=source_id,
            source_url=source_url,
            raw_metadata=raw_metadata,
        )
        is_new = True

    # 4. Upsert authors and link to this paper
    author_affiliations: list[str] = paper_data.get("author_affiliations") or []
    author_ids: list[dict[str, Any]] = paper_data.get("author_ids") or []

    for position, name in enumerate(authors):
        affiliation: str | None = (
            author_affiliations[position]
            if position < len(author_affiliations)
            else None
        )
        ext_ids: dict[str, Any] = (
            author_ids[position]
            if position < len(author_ids)
            else {}
        )

        author_kwargs: dict[str, Any] = {}
        if affiliation:
            author_kwargs["affiliation"] = affiliation
        for id_key in ("openalex_id", "semantic_scholar_id", "orcid"):
            if ext_ids.get(id_key):
                author_kwargs[id_key] = ext_ids[id_key]

        author_id = upsert_author(
            name=name,
            name_normalized=_normalize_author_name(name),
            **author_kwargs,
        )
        link_paper_author(paper_id=paper_id, author_id=author_id, position=position)

    return paper_id, is_new


# ---------------------------------------------------------------------------
# Smoke-test  (python3 lib/dedup.py  or  python3 -m econsignals.lib.dedup)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Relative imports are now resolved because the bootstrap at the top of
    # this file re-invoked us via runpy.run_module() under the package name.

    import econsignals.lib.dedup as _dedup_mod

    # ------------------------------------------------------------------ #
    # In-memory SQLite schema that mirrors what db.py creates
    # ------------------------------------------------------------------ #
    _mem_conn = sqlite3.connect(":memory:")
    _mem_conn.row_factory = sqlite3.Row
    _mem_conn.executescript(
        """
        CREATE TABLE papers (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            title            TEXT,
            title_normalized TEXT,
            canonical_id     TEXT UNIQUE,
            abstract         TEXT,
            doi              TEXT UNIQUE,
            url              TEXT,
            published_at     TEXT,
            paper_type       TEXT,
            jel_codes        TEXT,
            keywords         TEXT
        );
        CREATE TABLE authors (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            name                TEXT,
            name_normalized     TEXT UNIQUE,
            affiliation         TEXT,
            openalex_id         TEXT,
            semantic_scholar_id TEXT,
            orcid               TEXT
        );
        CREATE TABLE paper_authors (
            paper_id  INTEGER,
            author_id INTEGER,
            position  INTEGER,
            PRIMARY KEY (paper_id, author_id)
        );
        CREATE TABLE paper_sources (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id     INTEGER,
            source       TEXT,
            source_id    TEXT,
            source_url   TEXT,
            raw_metadata TEXT
        );
        """
    )

    # ------------------------------------------------------------------ #
    # Lightweight stubs for db.py functions
    # ------------------------------------------------------------------ #

    def _stub_get_db() -> sqlite3.Connection:
        _mem_conn.row_factory = sqlite3.Row
        return _mem_conn

    def _stub_find_paper_by_doi(doi: str) -> dict | None:
        row = _mem_conn.execute(
            "SELECT * FROM papers WHERE doi = ?", (doi,)
        ).fetchone()
        return dict(row) if row else None

    def _stub_find_paper_by_normalized_title(title_norm: str) -> list[dict]:
        rows = _mem_conn.execute(
            "SELECT * FROM papers WHERE title_normalized = ?", (title_norm,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _stub_insert_paper(paper: dict) -> int:
        jel = json.dumps(paper.get("jel_codes") or [])
        kw = json.dumps(paper.get("keywords") or [])
        cur = _mem_conn.execute(
            """INSERT INTO papers
                   (title, title_normalized, canonical_id, abstract, doi, url,
                    published_at, paper_type, jel_codes, keywords)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper.get("title"), paper.get("title_normalized"),
                paper.get("canonical_id"), paper.get("abstract"),
                paper.get("doi"), paper.get("url"),
                paper.get("published_at"), paper.get("paper_type"),
                jel, kw,
            ),
        )
        _mem_conn.commit()
        return cur.lastrowid

    def _stub_insert_paper_source(
        paper_id: int, source: str, source_id: str,
        source_url: str | None = None, raw_metadata: dict | None = None,
    ) -> int:
        cur = _mem_conn.execute(
            """INSERT INTO paper_sources
                   (paper_id, source, source_id, source_url, raw_metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (paper_id, source, source_id, source_url,
             json.dumps(raw_metadata) if raw_metadata else None),
        )
        _mem_conn.commit()
        return cur.lastrowid

    def _stub_upsert_author(name: str, name_normalized: str, **kwargs) -> int:
        row = _mem_conn.execute(
            "SELECT id FROM authors WHERE name_normalized = ?", (name_normalized,)
        ).fetchone()
        if row:
            return row["id"]
        cur = _mem_conn.execute(
            """INSERT INTO authors
                   (name, name_normalized, affiliation,
                    openalex_id, semantic_scholar_id, orcid)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, name_normalized, kwargs.get("affiliation"),
             kwargs.get("openalex_id"), kwargs.get("semantic_scholar_id"),
             kwargs.get("orcid")),
        )
        _mem_conn.commit()
        return cur.lastrowid

    def _stub_link_paper_author(paper_id: int, author_id: int, position: int) -> None:
        _mem_conn.execute(
            """INSERT OR IGNORE INTO paper_authors (paper_id, author_id, position)
               VALUES (?, ?, ?)""",
            (paper_id, author_id, position),
        )
        _mem_conn.commit()

    # Monkey-patch the fully-imported module so all internal calls use stubs
    _dedup_mod.get_db = _stub_get_db
    _dedup_mod.find_paper_by_doi = _stub_find_paper_by_doi
    _dedup_mod.find_paper_by_normalized_title = _stub_find_paper_by_normalized_title
    _dedup_mod.insert_paper = _stub_insert_paper
    _dedup_mod.insert_paper_source = _stub_insert_paper_source
    _dedup_mod.upsert_author = _stub_upsert_author
    _dedup_mod.link_paper_author = _stub_link_paper_author

    # ------------------------------------------------------------------ #
    # Test fixtures
    # ------------------------------------------------------------------ #

    # NBER version: shorter abstract, no DOI yet
    _paper_nber = {
        "title": "NBER Working Paper No. 31705: Inflation and the Labor Market",
        "authors": ["Lawrence Katz", "Alan Krueger"],
        "abstract": "We study inflation and labor market dynamics.",
        "doi": None,
        "url": "https://www.nber.org/papers/w31705",
        "published_at": "2023-09",
        "paper_type": "working_paper",
        "jel_codes": ["E31", "J30"],
        "keywords": ["inflation", "wages"],
    }

    # Semantic Scholar version: same paper, longer abstract, has DOI, one new JEL code
    _paper_semantic_scholar = {
        "title": "Inflation and the Labor Market",
        "authors": ["Lawrence Katz", "Alan Krueger"],
        "abstract": (
            "We study the dynamic relationship between inflation and labor market "
            "outcomes using a novel dataset spanning four decades of US data. "
            "Our findings suggest that the Phillips curve remains operative at "
            "low unemployment rates."
        ),
        "doi": "10.3386/w31705",
        "url": "https://www.semanticscholar.org/paper/4567890",
        "published_at": "2023-08",
        "paper_type": "working_paper",
        "jel_codes": ["E31", "J30", "E52"],
        "keywords": ["inflation", "unemployment", "phillips curve"],
    }

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    print("=" * 60)
    print("EconSignals dedup smoke-test")
    print("=" * 60)

    print("\n[test] Ingesting NBER version ...")
    _pid1, _new1 = _dedup_mod.ingest_paper(
        _paper_nber, source="nber", source_id="w31705", source_url=_paper_nber["url"]
    )
    print(f"       paper_id={_pid1}  is_new={_new1}")
    assert _new1 is True, "First ingest must be new"

    print(
        "\n[test] Ingesting Semantic Scholar version "
        "(same paper, dedup via Jaccard title match) ..."
    )
    _pid2, _new2 = _dedup_mod.ingest_paper(
        _paper_semantic_scholar,
        source="semantic_scholar",
        source_id="S2-4567890",
        source_url=_paper_semantic_scholar["url"],
    )
    print(f"       paper_id={_pid2}  is_new={_new2}")
    assert _new2 is False, "Second ingest must NOT be new"
    assert _pid1 == _pid2, f"paper_ids must match: {_pid1} != {_pid2}"

    # Verify metadata merge
    _row = _mem_conn.execute("SELECT * FROM papers WHERE id = ?", (_pid1,)).fetchone()
    assert _row["published_at"] == "2023-08", (
        f"published_at not updated to earlier date: got {_row['published_at']}"
    )
    assert _row["doi"] == "10.3386/w31705", (
        f"doi not merged: {_row['doi']}"
    )

    # Verify both sources are recorded
    _sources = _mem_conn.execute(
        "SELECT source FROM paper_sources WHERE paper_id = ? ORDER BY id", (_pid1,)
    ).fetchall()
    _source_names = [s["source"] for s in _sources]
    assert _source_names == ["nber", "semantic_scholar"], f"unexpected sources: {_source_names}"

    _merged_jel = json.loads(_row["jel_codes"])
    assert set(_merged_jel) == {"E31", "J30", "E52"}, f"jel merge wrong: {_merged_jel}"

    print("\n[test] All assertions passed.")
    print(f"       Sources recorded : {_source_names}")
    print(f"       published_at     : {_row['published_at']}")
    print(f"       doi              : {_row['doi']}")
    print(f"       jel_codes        : {_merged_jel}")
    print("\nDedup smoke-test: PASSED")
