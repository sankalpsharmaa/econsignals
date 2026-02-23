"""
dedup.py
--------
Cross-source paper deduplication for EconSignals.

The same paper can arrive from NBER, SSRN, OpenAlex, and RePEc.  This module
is responsible for deciding whether an incoming paper already exists in the
database and, if so, merging any richer metadata from the new record before
logging the additional source.

Deduplication cascade (find_existing_paper):
    1. DOI exact match  (~60 % of duplicates)
    2. Exact normalized-title match
    3. Fuzzy title match: longest-word LIKE filter + Jaccard >= 0.85
    4. Author-overlap fallback: first-author last-name LIKE filter +
       Jaccard >= 0.70 + at least 2 shared author last names

All DB access goes through lib/db.py.  No raw SQL is executed here except for
the LIKE candidate queries in steps 3 and 4, which require ad-hoc filtering
that the db module's narrow helpers cannot provide.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
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
# Internal helpers
# ---------------------------------------------------------------------------

_JACCARD_FUZZY_THRESHOLD = 0.85
_JACCARD_AUTHOR_THRESHOLD = 0.70
_AUTHOR_OVERLAP_MIN = 2

# Minimum word length considered "significant" for the LIKE candidate query.
_MIN_ANCHOR_WORD_LEN = 4


def _longest_word(token_set: set[str]) -> str | None:
    """Return the longest token that meets the minimum length threshold.

    Short tokens (stop-words like 'the', 'and') make terrible LIKE anchors
    because they match far too many rows.  We prefer the longest word in the
    title as a high-selectivity anchor.

    Returns None when the token set is empty or every token is too short.
    """
    candidates = [t for t in token_set if len(t) >= _MIN_ANCHOR_WORD_LEN]
    if not candidates:
        return None
    return max(candidates, key=len)


def _fetch_candidates_by_title_word(anchor: str) -> list[dict[str, Any]]:
    """Query papers whose normalized title contains *anchor* as a substring.

    This is the only place outside db.py where we touch SQLite directly.
    We do it here because find_paper_by_normalized_title performs an exact
    lookup and the db module has no LIKE-search helper.  The result set is
    small in practice because *anchor* is deliberately the longest, most
    selective word in the title.

    Args:
        anchor: A lowercase word that must appear somewhere in title_normalized.

    Returns:
        List of paper row dicts from the papers table.
    """
    conn: sqlite3.Connection = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM papers WHERE title_normalized LIKE ?",
        (f"%{anchor}%",),
    )
    rows = cur.fetchall()
    return [dict(row) for row in rows]


def _fetch_candidates_by_author_lastname(last_name: str) -> list[dict[str, Any]]:
    """Query papers linked to an author whose normalized name contains *last_name*.

    Joins papers -> paper_authors -> authors so we can filter by author name
    rather than embedding the last name in the title.

    Args:
        last_name: Lowercase, accent-stripped last name to search for.

    Returns:
        List of paper row dicts (may contain duplicates if an author appears
        multiple times; callers should de-duplicate by paper_id if needed).
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
    rows = cur.fetchall()
    return [dict(row) for row in rows]


def _parse_authors_field(row: dict[str, Any]) -> list[str]:
    """Extract a list of author name strings from a paper row.

    The papers table may store authors as a JSON array or a pipe-delimited
    string depending on how insert_paper was called.  This helper normalizes
    both representations so callers get a plain list[str].
    """
    import json

    raw = row.get("authors") or row.get("author_names") or ""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        # Try JSON first, then fall back to pipe-separation
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

    1. **DOI exact match** (cheapest, ~60 % of duplicates):
       If *doi* is provided, delegate to find_paper_by_doi().  A hit returns
       immediately without running the title strategies.

    2. **Exact normalized-title match**:
       Normalize the incoming title and query find_paper_by_normalized_title().
       If exactly one result comes back we treat it as a match.  Multiple
       results (unlikely but possible for very short generic titles) fall
       through to the Jaccard filter below.

    3. **Fuzzy title match** (Jaccard >= 0.85):
       Pick the longest significant word from the title token set and run a
       LIKE-based candidate query.  For each candidate compute Jaccard
       similarity against the incoming title's token set.  The first candidate
       that clears 0.85 is returned.

    4. **Author-overlap fallback** (Jaccard >= 0.70 + 2+ shared last names):
       If no title match is found we try the first author's last name as the
       LIKE anchor against the authors table.  Candidates that share at least
       two author last names *and* achieve title Jaccard >= 0.70 are returned.
       This catches cases where the title differs substantially between sources
       (e.g. NBER adds a subtitle that SSRN omits).

    Args:
        title:   Raw paper title from the incoming sensor payload.
        authors: List of raw author name strings.
        doi:     Optional DOI string (e.g. "10.3386/w31705").

    Returns:
        The integer primary key of the matching papers row, or None if no
        match is found.
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
        # Multiple hits: pick the one with highest Jaccard (should be 1.0 for
        # an exact title match, but guard against edge cases).
        best_id, best_score = None, 0.0
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
        fuzzy_candidates = _fetch_candidates_by_title_word(anchor)
        for row in fuzzy_candidates:
            candidate_tokens = title_token_set(row.get("title", ""))
            score = jaccard_similarity(tokens, candidate_tokens)
            if score >= _JACCARD_FUZZY_THRESHOLD:
                return row["id"]

    # 4. Author-overlap fallback (Jaccard >= 0.70 + 2+ shared last names)
    if authors:
        first_last = extract_last_name(authors[0])
        if first_last:
            author_candidates = _fetch_candidates_by_author_lastname(first_last)
            for row in author_candidates:
                candidate_tokens = title_token_set(row.get("title", ""))
                title_score = jaccard_similarity(tokens, candidate_tokens)
                if title_score < _JACCARD_AUTHOR_THRESHOLD:
                    continue
                candidate_authors = _parse_authors_field(row)
                if author_lastnames_overlap(authors, candidate_authors) >= _AUTHOR_OVERLAP_MIN:
                    return row["id"]

    return None


def merge_paper_metadata(existing: dict[str, Any], new_data: dict[str, Any]) -> dict[str, Any]:
    """Compute metadata updates by merging *new_data* into *existing*.

    Merge rules (applied in priority order):

    - **DOI**: prefer the version that has one.
    - **Abstract**: prefer the longer string.
    - **paper_type**: 'journal_article' beats 'working_paper' beats anything else.
    - **jel_codes**: union of both lists (order-preserving, de-duplicated).
    - **keywords**: union of both lists (order-preserving, de-duplicated, case-folded).
    - **published_at**: keep the earliest date (ISO strings compared lexicographically;
      partial dates like "2023" are handled by string prefix comparison which works
      correctly for ISO 8601 ordering).

    Only fields that differ from *existing* are returned, so the caller can
    issue a minimal UPDATE.

    Args:
        existing:  Current paper record as a dict (from the DB).
        new_data:  Incoming paper data dict (from the sensor payload).

    Returns:
        Dict of {field: new_value} for fields that should be updated.
        Empty dict means no changes are needed.
    """
    updates: dict[str, Any] = {}

    # DOI: prefer whichever has one
    if not existing.get("doi") and new_data.get("doi"):
        updates["doi"] = new_data["doi"]

    # Abstract: prefer the longer one
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

    # JEL codes: union (order-preserving, de-duplicated)
    existing_jel: list[str] = existing.get("jel_codes") or []
    new_jel: list[str] = new_data.get("jel_codes") or []
    if isinstance(existing_jel, str):
        import json
        try:
            existing_jel = json.loads(existing_jel)
        except (json.JSONDecodeError, TypeError):
            existing_jel = [j.strip() for j in existing_jel.split(",") if j.strip()]
    merged_jel = list(dict.fromkeys(existing_jel + [j for j in new_jel if j not in existing_jel]))
    if merged_jel != existing_jel:
        updates["jel_codes"] = merged_jel

    # Keywords: union (order-preserving, de-duplicated, case-folded comparison)
    existing_kw: list[str] = existing.get("keywords") or []
    new_kw: list[str] = new_data.get("keywords") or []
    if isinstance(existing_kw, str):
        import json
        try:
            existing_kw = json.loads(existing_kw)
        except (json.JSONDecodeError, TypeError):
            existing_kw = [k.strip() for k in existing_kw.split(",") if k.strip()]
    existing_kw_lower = {k.lower() for k in existing_kw}
    merged_kw = existing_kw + [k for k in new_kw if k.lower() not in existing_kw_lower]
    if len(merged_kw) != len(existing_kw):
        updates["keywords"] = merged_kw

    # published_at: keep earliest (ISO string prefix ordering works for YYYY-MM-DD)
    existing_date: str | None = existing.get("published_at")
    new_date: str | None = new_data.get("published_at")
    if new_date and existing_date:
        if str(new_date) < str(existing_date):
            updates["published_at"] = new_date
    elif new_date and not existing_date:
        updates["published_at"] = new_date

    return updates


def _apply_metadata_updates(paper_id: int, updates: dict[str, Any]) -> None:
    """Persist *updates* to the papers table for *paper_id*.

    Args:
        paper_id: Primary key of the paper to update.
        updates:  Dict of {column: value} produced by merge_paper_metadata().
    """
    if not updates:
        return

    import json

    conn: sqlite3.Connection = get_db()

    # Serialize list fields to JSON before writing
    serialized: dict[str, Any] = {}
    for k, v in updates.items():
        if isinstance(v, list):
            serialized[k] = json.dumps(v)
        else:
            serialized[k] = v

    set_clause = ", ".join(f"{col} = ?" for col in serialized)
    values = list(serialized.values()) + [paper_id]
    conn.execute(f"UPDATE papers SET {set_clause} WHERE id = ?", values)
    conn.commit()


def ingest_paper(
    paper_data: dict[str, Any],
    source: str,
    source_id: str,
    source_url: str | None = None,
    raw_metadata: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Main entry point for adding a paper from any sensor.

    Handles deduplication, metadata merging, source linking, and author
    upsertion in one call.  Sensors should call this function once per paper
    instead of writing to the DB directly.

    paper_data keys
    ---------------
    Required:
        title (str)
        authors (list[str])
    Optional:
        abstract (str)
        doi (str)
        url (str)
        published_at (str, ISO 8601 date)
        paper_type (str, e.g. 'working_paper' | 'journal_article')
        jel_codes (list[str])
        keywords (list[str])
        author_affiliations (list[str], parallel to authors)
        author_ids (list[dict], parallel to authors; each dict may contain
                    'openalex_id', 'semantic_scholar_id', 'orcid')

    Args:
        paper_data:   Normalized payload from the sensor.
        source:       Source name, e.g. 'nber', 'ssrn', 'openalex', 'repec'.
        source_id:    Source's own identifier for this paper (e.g. NBER wp number).
        source_url:   Optional canonical URL at the source.
        raw_metadata: Optional full raw response payload for audit purposes.

    Returns:
        (paper_id, is_new) where *is_new* is True when a new row was inserted
        into the papers table, False when an existing paper was matched.
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
        # 3a. Existing paper: add the new source and merge any richer metadata
        print(f"[dedup] duplicate found: paper_id={existing_id} source={source} source_id={source_id}")

        insert_paper_source(
            paper_id=existing_id,
            source=source,
            source_id=source_id,
            source_url=source_url,
            raw_metadata=raw_metadata,
        )

        # Fetch current record and merge
        conn: sqlite3.Connection = get_db()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM papers WHERE id = ?", (existing_id,)).fetchone()
        if row:
            existing_record = dict(row)
            updates = merge_paper_metadata(existing_record, paper_data)
            if updates:
                print(f"[dedup] merging fields {list(updates.keys())} into paper_id={existing_id}")
                _apply_metadata_updates(existing_id, updates)

        paper_id = existing_id
        is_new = False

    else:
        # 3b. New paper: insert then add source
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
        print(f"[dedup] new paper inserted: paper_id={paper_id} source={source} source_id={source_id}")

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

        # Build kwargs for upsert_author from available external IDs
        author_kwargs: dict[str, Any] = {}
        if affiliation:
            author_kwargs["affiliation"] = affiliation
        for id_key in ("openalex_id", "semantic_scholar_id", "orcid"):
            if id_key in ext_ids and ext_ids[id_key]:
                author_kwargs[id_key] = ext_ids[id_key]

        name_normalized = _normalize_author_name(name)

        author_id = upsert_author(
            name=name,
            name_normalized=name_normalized,
            **author_kwargs,
        )
        link_paper_author(paper_id=paper_id, author_id=author_id, position=position)

    return paper_id, is_new


# ---------------------------------------------------------------------------
# Smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Smoke-test: simulate the same paper arriving from NBER then SSRN.
    #
    # Running directly (python3 lib/dedup.py) breaks relative imports, so we
    # bootstrap the package on sys.path and re-execute via runpy so the module
    # loads under its proper dotted name.
    import pathlib
    import sys

    _lib_dir = pathlib.Path(__file__).resolve().parent   # .../lib
    _pkg_dir = _lib_dir.parent                           # .../econsignals (package)
    _root_dir = _pkg_dir.parent                          # directory that contains econsignals/

    if str(_root_dir) not in sys.path:
        sys.path.insert(0, str(_root_dir))

    # Re-run this file as part of the package so relative imports work.
    # Guard against infinite recursion with a sentinel env-var.
    import os
    if not os.environ.get("_DEDUP_SMOKETEST"):
        os.environ["_DEDUP_SMOKETEST"] = "1"
        import runpy
        runpy.run_module("econsignals.lib.dedup", run_name="__main__", alter_sys=True)
        sys.exit(0)

    # ------------------------------------------------------------------ #
    # Reached only when re-invoked via runpy (relative imports work here)
    # ------------------------------------------------------------------ #
    import json
    import sqlite3

    # ------------------------------------------------------------------ #
    # In-memory SQLite schema mirroring what db.py creates
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

    import econsignals.lib.dedup as _dedup_mod

    def _stub_get_db() -> sqlite3.Connection:
        _mem_conn.row_factory = sqlite3.Row
        return _mem_conn

    def _stub_find_paper_by_doi(doi: str) -> dict | None:
        row = _mem_conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
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
            "INSERT INTO paper_sources (paper_id, source, source_id, source_url, raw_metadata) VALUES (?, ?, ?, ?, ?)",
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
               (name, name_normalized, affiliation, openalex_id, semantic_scholar_id, orcid)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, name_normalized, kwargs.get("affiliation"),
             kwargs.get("openalex_id"), kwargs.get("semantic_scholar_id"),
             kwargs.get("orcid")),
        )
        _mem_conn.commit()
        return cur.lastrowid

    def _stub_link_paper_author(paper_id: int, author_id: int, position: int) -> None:
        _mem_conn.execute(
            "INSERT OR IGNORE INTO paper_authors (paper_id, author_id, position) VALUES (?, ?, ?)",
            (paper_id, author_id, position),
        )
        _mem_conn.commit()

    # Monkey-patch the module globals so all internal calls use stubs
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
    paper_nber = {
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

    # SSRN version: same paper, richer abstract, has DOI, adds a JEL code
    paper_ssrn = {
        "title": "Inflation and the Labor Market",
        "authors": ["Lawrence Katz", "Alan Krueger"],
        "abstract": (
            "We study the dynamic relationship between inflation and labor market "
            "outcomes using a novel dataset spanning four decades of US data. "
            "Our findings suggest that the Phillips curve remains operative at "
            "low unemployment rates."
        ),
        "doi": "10.2139/ssrn.4567890",
        "url": "https://ssrn.com/abstract=4567890",
        "published_at": "2023-08",
        "paper_type": "working_paper",
        "jel_codes": ["E31", "J30", "E52"],
        "keywords": ["inflation", "unemployment", "phillips curve"],
    }

    # ------------------------------------------------------------------ #
    # Run the test
    # ------------------------------------------------------------------ #

    print("=" * 60)
    print("EconSignals dedup smoke-test")
    print("=" * 60)

    print("\n[test] Ingesting NBER version ...")
    pid1, new1 = _dedup_mod.ingest_paper(
        paper_nber, source="nber", source_id="w31705", source_url=paper_nber["url"]
    )
    print(f"       paper_id={pid1}  is_new={new1}")
    assert new1 is True, "First ingest must be new"

    print("\n[test] Ingesting SSRN version (same paper, should dedup via title Jaccard) ...")
    pid2, new2 = _dedup_mod.ingest_paper(
        paper_ssrn, source="ssrn", source_id="4567890", source_url=paper_ssrn["url"]
    )
    print(f"       paper_id={pid2}  is_new={new2}")
    assert new2 is False, "Second ingest must NOT be new"
    assert pid1 == pid2, f"paper_ids must match: {pid1} != {pid2}"

    # Verify metadata merge
    row = _mem_conn.execute("SELECT * FROM papers WHERE id = ?", (pid1,)).fetchone()
    assert row["published_at"] == "2023-08", f"published_at not updated: {row['published_at']}"
    assert row["doi"] == "10.2139/ssrn.4567890", f"doi not merged: {row['doi']}"

    # Verify both sources are recorded
    sources = _mem_conn.execute(
        "SELECT source FROM paper_sources WHERE paper_id = ? ORDER BY id", (pid1,)
    ).fetchall()
    source_names = [s["source"] for s in sources]
    assert source_names == ["nber", "ssrn"], f"unexpected sources: {source_names}"

    merged_jel = json.loads(row["jel_codes"])
    assert set(merged_jel) == {"E31", "J30", "E52"}, f"jel merge wrong: {merged_jel}"

    print("\n[test] All assertions passed.")
    print(f"       Sources recorded : {source_names}")
    print(f"       published_at     : {row['published_at']}")
    print(f"       doi              : {row['doi']}")
    print(f"       jel_codes        : {merged_jel}")
    print("\nDedup smoke-test: PASSED")
