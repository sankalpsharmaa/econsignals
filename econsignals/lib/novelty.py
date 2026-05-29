"""
novelty.py
----------
In-memory novelty helpers for the newsletter / dashboard presentation layer.

Where dedup.py decides whether an *incoming* paper already exists in the
database (DB-backed, one paper at a time), this module operates purely in
memory on a *list* of already-surfaced paper dicts.  It answers two questions
the presentation layer needs:

1. collapse_duplicates  -- the same work arrives across sources and as
   preprint-vs-published; show it once, noting where else it appeared.
2. suppress_seen        -- the user has already seen a work (it is in their
   Zotero library, or it was surfaced in a previous brief); drop it.

All functions are pure: no DB writes, no network, no mutation of inputs.

Input contract (the dict shape produced by lib/snapshot.build_snapshot):
    title         (str)        raw display title
    authors       (list[str])  display names, NOT pre-normalized
    doi           (str | None) raw DOI, e.g. "10.3386/w31705" (no URL prefix)
    source        (str | None) single source string, e.g. "nber", "openalex"
    abstract      (str | None)
    published_at  (str | None) ISO date
    ...            other keys are preserved untouched on the surviving record.

Every field is accessed with .get() so a missing key degrades to a sensible
default rather than raising.  The same-work matching reuses the thresholds
dedup.py already tuned (see _SAME_WORK_* below) and the normalize.py
primitives, so this module stays consistent with the DB-side cascade.
"""

from __future__ import annotations

from typing import Any

from .normalize import (
    author_lastnames_overlap,
    jaccard_similarity,
    normalize_title,
    title_token_set,
)
from .relevance import _PRESTIGE_SOURCES

# ---------------------------------------------------------------------------
# Constants (mirror dedup.py's tuned same-work thresholds)
# ---------------------------------------------------------------------------

# Title-only Jaccard high enough to call two papers the same work outright.
_SAME_WORK_TITLE_JACCARD = 0.85

# Lower title Jaccard that becomes conclusive when enough authors also overlap.
_SAME_WORK_TITLE_JACCARD_WITH_AUTHORS = 0.70
_SAME_WORK_AUTHOR_OVERLAP_MIN = 2

# Default prestige for a source missing from _PRESTIGE_SOURCES (mirrors the
# 0.3 fallback relevance.py uses for unknown sources).
_PRESTIGE_DEFAULT = 0.3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_doi(doi: str | None) -> str | None:
    """Canonicalize a DOI to a bare lowercase identifier.

    Strips the common URL and scheme prefixes so that "10.x",
    "https://doi.org/10.x", "http://dx.doi.org/10.x", and "doi:10.x" all
    compare equal.  Returns None for falsy input.

    Args:
        doi: Raw DOI string or None.

    Returns:
        Lowercase bare DOI (e.g. "10.3386/w31705"), or None.
    """
    if not doi:
        return None
    d = doi.strip().lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi.org/",
        "doi:",
    ):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d.strip() or None


def _prestige(paper: dict[str, Any]) -> float:
    """Return the source-prestige score for a paper dict.

    Args:
        paper: Paper dict with an optional "source" key.

    Returns:
        Prestige in [0, 1]; the shared _PRESTIGE_DEFAULT for unknown/missing sources.
    """
    return _PRESTIGE_SOURCES.get(paper.get("source") or "", _PRESTIGE_DEFAULT)


def _completeness(paper: dict[str, Any]) -> int:
    """Count how many high-value metadata fields a paper dict carries.

    Used only as a tie-breaker after prestige when choosing the canonical
    record, so a richer record wins among equally prestigious sources.

    Args:
        paper: Paper dict.

    Returns:
        Number of populated fields among (doi, abstract, published_at, jel).
    """
    fields = ("doi", "abstract", "published_at", "jel")
    return sum(1 for f in fields if paper.get(f))


def _abstract_len(paper: dict[str, Any]) -> int:
    """Length of the paper's abstract, 0 when absent.

    Args:
        paper: Paper dict.

    Returns:
        Character count of the abstract.
    """
    return len(paper.get("abstract") or "")


def _is_same_work(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Decide whether two paper dicts describe the same scholarly work.

    Matches on ANY of the criteria dedup.py uses, in the same cost order:

    1. Exact DOI match (after canonicalization).
    2. Exact normalized-title match.
    3. Title Jaccard >= 0.85.
    4. Title Jaccard >= 0.70 AND >= 2 shared author surnames.

    Criteria are OR-combined deliberately: a *differing* DOI must not block a
    strong title+author match, because the preprint-vs-published case is
    exactly when DOIs differ (or the preprint has none).

    Args:
        a: First paper dict.
        b: Second paper dict.

    Returns:
        True if the two records should collapse into one.
    """
    # 1. DOI exact match
    doi_a = _normalize_doi(a.get("doi"))
    doi_b = _normalize_doi(b.get("doi"))
    if doi_a and doi_b and doi_a == doi_b:
        return True

    # 2. Exact normalized-title match
    title_a = a.get("title") or ""
    title_b = b.get("title") or ""
    norm_a = normalize_title(title_a)
    norm_b = normalize_title(title_b)
    if norm_a and norm_a == norm_b:
        return True

    # 3 & 4. Fuzzy title match, alone or backed by author overlap
    tokens_a = title_token_set(title_a)
    tokens_b = title_token_set(title_b)
    title_score = jaccard_similarity(tokens_a, tokens_b)
    if title_score >= _SAME_WORK_TITLE_JACCARD:
        return True
    if title_score >= _SAME_WORK_TITLE_JACCARD_WITH_AUTHORS:
        overlap = author_lastnames_overlap(
            a.get("authors") or [],
            b.get("authors") or [],
        )
        if overlap >= _SAME_WORK_AUTHOR_OVERLAP_MIN:
            return True

    return False


def _cluster_indices(papers: list[dict[str, Any]]) -> list[list[int]]:
    """Group paper indices into same-work clusters via transitive closure.

    Builds same-work edges pairwise (O(n^2), fine at newsletter scale) and
    unions connected papers with a union-find so that A~B and B~C collapse
    into one cluster even when A and C do not directly match.

    Args:
        papers: List of paper dicts.

    Returns:
        List of clusters, each a list of indices into *papers*.  Clusters are
        ordered by their smallest member index, and indices within a cluster
        are sorted ascending, so input order is preserved downstream.
    """
    n = len(papers)
    parent = list(range(n))

    def find(x: int) -> int:
        # Path-compress to the cluster root.
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    # Connect every same-work pair.
    for i in range(n):
        for j in range(i + 1, n):
            if _is_same_work(papers[i], papers[j]):
                union(i, j)

    # Gather members under each root, preserving ascending index order.
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    # Order clusters by their first (smallest-index) member.
    return sorted(groups.values(), key=lambda members: members[0])


def _choose_canonical(members: list[dict[str, Any]]) -> int:
    """Pick the index of the canonical record within a same-work cluster.

    Selection key, applied as a descending sort:
    1. Source prestige (NBER > working paper > preprint > social).
    2. Metadata completeness (populated doi/abstract/published_at/jel).
    3. Abstract length, as a final richness tie-break.

    Args:
        members: Paper dicts in one cluster.

    Returns:
        Index into *members* of the record to keep as canonical.
    """
    best = 0
    best_key = (_prestige(members[0]), _completeness(members[0]), _abstract_len(members[0]))
    for idx in range(1, len(members)):
        key = (_prestige(members[idx]), _completeness(members[idx]), _abstract_len(members[idx]))
        if key > best_key:
            best_key = key
            best = idx
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collapse_duplicates(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-work duplicates in a list of paper dicts.

    Groups papers that describe the same work across sources or as
    preprint-vs-published (see :func:`_is_same_work`), then for each group:

    - keeps the highest-prestige / most-complete record as canonical;
    - backfills the canonical record's empty fields from the other members
      using the same merge rules as dedup.merge_paper_metadata
      (longer abstract, present DOI, earlier date).  NOTE: that merge reads the
      ``jel_codes`` / ``keywords`` keys, but snapshot dicts use ``jel`` (and
      carry no ``keywords``), so JEL/keyword union is a no-op here; the
      integrator must map ``jel`` -> ``jel_codes`` before the merge if wanted;
    - attaches an ``also_in`` list of the *other* members' sources (sorted,
      de-duplicated).  ``also_in`` is always present (empty for singletons),
      so downstream rendering has a stable shape.

    The returned list preserves input order: each surviving record appears at
    the position of its cluster's first (smallest-index) member, so a prior
    relevance ranking is not reshuffled.

    Args:
        papers: List of paper dicts (see module docstring for the contract).

    Returns:
        A new list of paper dicts with duplicates collapsed.  Inputs are not
        mutated; surviving records are shallow copies with ``also_in`` set.
    """
    if not papers:
        return []

    # Lazy import: merge logic lives in dedup, which itself imports normalize.
    from .dedup import merge_paper_metadata

    out: list[dict[str, Any]] = []
    for cluster in _cluster_indices(papers):
        members = [papers[i] for i in cluster]

        if len(members) == 1:
            survivor = dict(members[0])
            survivor["also_in"] = []
            out.append(survivor)
            continue

        canonical_idx = _choose_canonical(members)
        survivor = dict(members[canonical_idx])

        # Backfill the canonical record from every other cluster member.
        for idx, other in enumerate(members):
            if idx == canonical_idx:
                continue
            updates = merge_paper_metadata(survivor, other)
            survivor.update(updates)

        # also_in: the sources of every OTHER member, de-duplicated and sorted.
        other_sources = {
            other.get("source")
            for idx, other in enumerate(members)
            if idx != canonical_idx and other.get("source")
        }
        survivor["also_in"] = sorted(other_sources)
        out.append(survivor)

    return out


def suppress_seen(
    papers: list[dict[str, Any]],
    seen_keys: set[str],
) -> list[dict[str, Any]]:
    """Drop papers the user has already seen.

    A paper is suppressed when its canonicalized DOI or its normalized title is
    present in *seen_keys*.  ``seen_keys`` must therefore hold normalized DOIs
    (bare lowercase, no URL prefix) and/or normalized titles -- build it with
    :func:`_normalize_doi` and :func:`normalize.normalize_title` so membership
    does not silently miss on a URL-form DOI.

    Args:
        papers:    List of paper dicts.
        seen_keys: Set of normalized DOIs and/or normalized titles already seen
                   (e.g. from the Zotero library or a prior brief).

    Returns:
        A new list containing only papers whose DOI and normalized title are
        both absent from *seen_keys*.  Input order is preserved; inputs are not
        mutated.  An empty/falsy key in *seen_keys* never suppresses a paper.
    """
    # Guard against an empty-string key suppressing every untitled paper.
    keys = {k for k in seen_keys if k}
    if not keys:
        return list(papers)

    kept: list[dict[str, Any]] = []
    for paper in papers:
        doi = _normalize_doi(paper.get("doi"))
        title_norm = normalize_title(paper.get("title") or "") or None
        if doi and doi in keys:
            continue
        if title_norm and title_norm in keys:
            continue
        kept.append(paper)
    return kept


def cluster_similar(papers: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group same-work papers for a "+N similar" display.

    Thin wrapper over :func:`_cluster_indices`: returns the clusters as lists
    of the original paper dicts (not collapsed).  A caller can render the first
    member and a "+N similar" affordance for the rest.

    Args:
        papers: List of paper dicts.

    Returns:
        List of clusters, each a list of paper dicts in input order.  Singletons
        appear as one-element lists.
    """
    return [[papers[i] for i in cluster] for cluster in _cluster_indices(papers)]
