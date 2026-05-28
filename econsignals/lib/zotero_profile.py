"""Utilities for building a lightweight personalization profile from Zotero.

Reads Zotero's local SQLite DB in read-only/immutable mode and extracts
authors, title/abstract terms, and DOI history for downstream reranking.
"""

from __future__ import annotations

import re
import sqlite3
import math
from datetime import datetime
from collections import Counter
from pathlib import Path
from typing import Any

_DEFAULT_ZOTERO_PATH = Path.home() / "Zotero" / "zotero.sqlite"

_ITEM_TYPES_EXCLUDED = {"attachment", "annotation", "note"}

_STOPWORDS = {
    "about", "after", "again", "also", "among", "analysis", "and", "are", "around",
    "article", "based", "because", "between", "both", "can", "data",
    "evidence", "for", "from", "has", "have", "how", "impact",
    "into", "its", "more", "most", "new", "not", "of", "paper", "policy", "results",
    "study", "that", "the", "their", "there", "these", "this", "through", "using",
    "with", "without", "what", "when", "where", "which", "while", "will", "work",
    "his", "her", "our", "their", "but", "than", "were", "find", "finds", "found",
    "show", "shows", "use", "uses", "used", "local", "global", "across", "within",
    "during", "over", "under", "into", "out", "all", "any", "may", "might", "must",
    "two", "three", "four", "five", "one", "first", "second", "third", "such",
    "increase", "increases", "decrease", "decreases", "higher", "lower", "average",
    "number", "large", "small", "different", "among", "across", "throughout", "overall",
    "effect", "effects", "model",
    # Garbled OCR fragments to suppress
    "vidence", "ndia", "rban", "ousing", "evelopment", "hese", "sing",
    "hina", "ffect", "inimum", "hana", "ausal", "olitical", "hen", "tate", "hree",
}

_TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")


def _tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and not t.isdigit()]


def _norm_author(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip().lower())
    return re.sub(r"[^a-z0-9\s\-']", "", name)


def _last_name(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name) if p]
    return parts[-1] if parts else ""


def zotero_db_path(path: str | None = None) -> Path | None:
    candidate = Path(path).expanduser() if path else _DEFAULT_ZOTERO_PATH
    return candidate if candidate.exists() else None


def _fetch_item_rows(conn: sqlite3.Connection, max_items: int) -> list[sqlite3.Row]:
    sql = """
    SELECT
        i.itemID,
        it.typeName,
        i.dateAdded,
        MAX(CASE WHEN f.fieldName = 'title' THEN v.value END) AS title,
        MAX(CASE WHEN f.fieldName = 'abstractNote' THEN v.value END) AS abstractNote,
        MAX(CASE WHEN f.fieldName = 'DOI' THEN v.value END) AS doi,
        MAX(CASE WHEN f.fieldName = 'date' THEN v.value END) AS itemDate
    FROM items i
    JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
    LEFT JOIN itemData d ON d.itemID = i.itemID
    LEFT JOIN fieldsCombined f ON f.fieldID = d.fieldID
    LEFT JOIN itemDataValues v ON v.valueID = d.valueID
    WHERE it.typeName NOT IN ('attachment', 'annotation', 'note')
    GROUP BY i.itemID, it.typeName, i.dateAdded
    HAVING title IS NOT NULL
    ORDER BY i.dateAdded DESC
    LIMIT ?
    """
    return conn.execute(sql, (max_items,)).fetchall()


def _fetch_creators(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[str]]:
    if not item_ids:
        return {}
    out: dict[int, list[str]] = {iid: [] for iid in item_ids}
    for i in range(0, len(item_ids), 900):
        batch = item_ids[i : i + 900]
        placeholders = ",".join("?" for _ in batch)
        sql = f"""
        SELECT ic.itemID, c.firstName, c.lastName
        FROM itemCreators ic
        JOIN creators c ON c.creatorID = ic.creatorID
        WHERE ic.itemID IN ({placeholders})
        ORDER BY ic.itemID, ic.orderIndex
        """
        for item_id, first_name, last_name in conn.execute(sql, batch).fetchall():
            full = " ".join(x for x in [(first_name or "").strip(), (last_name or "").strip()] if x).strip()
            if full:
                out.setdefault(item_id, []).append(full)
    return out


def load_zotero_profile(
    db_path: str | None = None,
    *,
    max_items: int = 1500,
    max_terms: int = 4000,
) -> dict[str, Any]:
    """Build a compact profile from local Zotero data."""
    path = zotero_db_path(db_path)
    if not path:
        return {
            "ok": False,
            "error": "zotero_db_not_found",
            "db_path": str(Path(db_path).expanduser()) if db_path else str(_DEFAULT_ZOTERO_PATH),
        }

    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        item_rows = _fetch_item_rows(conn, max_items=max_items)
        item_ids = [int(r["itemID"]) for r in item_rows]
        creator_map = _fetch_creators(conn, item_ids)

        full_author_counter: Counter[str] = Counter()
        last_author_counter: Counter[str] = Counter()
        term_tf: Counter[str] = Counter()
        term_df: Counter[str] = Counter()
        doi_set: set[str] = set()
        kept_items = 0

        for row in item_rows:
            if (row["typeName"] or "").strip() in _ITEM_TYPES_EXCLUDED:
                continue
            title = (row["title"] or "").strip()
            if not title:
                continue
            kept_items += 1

            abstract = (row["abstractNote"] or "").strip()
            doi = (row["doi"] or "").strip().lower()
            if doi:
                doi_set.add(doi)

            title_tokens = _tokenize(title)
            abstract_tokens = _tokenize(abstract)
            for tok in title_tokens:
                term_tf[tok] += 2
            for tok in abstract_tokens:
                term_tf[tok] += 1
            for tok in set(title_tokens + abstract_tokens):
                term_df[tok] += 1

            for author in creator_map.get(int(row["itemID"]), []):
                norm = _norm_author(author)
                if not norm:
                    continue
                full_author_counter[norm] += 1
                ln = _last_name(norm)
                if ln:
                    last_author_counter[ln] += 1

        denom = max(kept_items, 1)
        weighted_raw: dict[str, float] = {}
        for tok, tf in term_tf.items():
            if tf < 3:
                continue
            df = term_df.get(tok, 1)
            if (df / denom) > 0.25:
                continue
            idf = math.log((denom + 1) / (df + 1)) + 1.0
            weighted_raw[tok] = float(tf) * idf

        top_sorted = sorted(weighted_raw.items(), key=lambda kv: kv[1], reverse=True)[:max_terms]
        max_w = max((v for _, v in top_sorted), default=1.0)
        weighted_terms = {k: v / max_w for k, v in top_sorted}

        max_full = max(full_author_counter.values()) if full_author_counter else 1
        max_last = max(last_author_counter.values()) if last_author_counter else 1

        return {
            "ok": True,
            "db_path": str(path),
            "items_scanned": len(item_rows),
            "items_kept": kept_items,
            "unique_dois": len(doi_set),
            "doi_set": sorted(doi_set),
            "authors_full": {k: v / max_full for k, v in full_author_counter.items()},
            "authors_last": {k: v / max_last for k, v in last_author_counter.items()},
            "terms": weighted_terms,
            "top_authors": [a for a, _ in full_author_counter.most_common(40)],
        }
    finally:
        conn.close()


def load_zotero_corpus(
    db_path: str | None = None,
    *,
    max_items: int = 500,
) -> list[dict[str, Any]]:
    """Load Zotero items as corpus for embedding-based similarity.

    Returns list of dicts with keys: title, abstract, text, date_added.
    Sorted by date_added descending (newest first). Used for weighted-average
    semantic similarity (zotero-arxiv-daily style).
    """
    path = zotero_db_path(db_path)
    if not path:
        return []

    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = _fetch_item_rows(conn, max_items=max_items)
        corpus: list[dict[str, Any]] = []
        for r in rows:
            if (r["typeName"] or "").strip() in _ITEM_TYPES_EXCLUDED:
                continue
            title = (r["title"] or "").strip()
            abstract = (r["abstractNote"] or "").strip()
            if not title and not abstract:
                continue
            text = f"{title} {abstract}".strip() if abstract else title
            if not text or len(text) < 20:
                continue
            # Zotero stores dateAdded as space-separated "YYYY-MM-DD HH:MM:SS"
            # (no 'T'); the prior 'T' format raised on every row and collapsed
            # the sort key to datetime.min.
            date_str = (r["dateAdded"] or "").strip()
            try:
                dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                dt = datetime.min
            corpus.append({
                "title": title,
                "abstract": abstract,
                "text": text,
                "date_added": dt,
            })
        return sorted(corpus, key=lambda x: x["date_added"], reverse=True)
    finally:
        conn.close()


def zotero_author_score(authors: list[str], profile: dict[str, Any]) -> tuple[float, list[str]]:
    """Return (score, matched_author_names) against Zotero profile."""
    full_map: dict[str, float] = profile.get("authors_full") or {}
    last_map: dict[str, float] = profile.get("authors_last") or {}
    if not authors:
        return 0.0, []

    score = 0.0
    matches: list[str] = []
    for a in authors:
        norm = _norm_author(a)
        if not norm:
            continue
        full_hit = float(full_map.get(norm, 0.0))
        last_hit = float(last_map.get(_last_name(norm), 0.0)) * 0.7
        best = max(full_hit, last_hit)
        if best > 0:
            score = max(score, best)
            matches.append(a)

    seen: set[str] = set()
    dedup = []
    for m in matches:
        lm = m.lower()
        if lm not in seen:
            seen.add(lm)
            dedup.append(m)
    return min(score, 1.0), dedup[:5]


def zotero_content_score(title: str, abstract: str | None, profile: dict[str, Any]) -> tuple[float, list[str]]:
    """Return (score, matched_terms) using weighted Zotero term overlap."""
    terms: dict[str, float] = profile.get("terms") or {}
    if not terms:
        return 0.0, []

    toks = set(_tokenize(title) + _tokenize(abstract or ""))
    overlap = [(t, terms[t]) for t in toks if t in terms]
    if not overlap:
        return 0.0, []

    overlap.sort(key=lambda x: x[1], reverse=True)
    raw = sum(w for _, w in overlap[:12])
    return min(raw / 4.0, 1.0), [t for t, _ in overlap[:6]]
