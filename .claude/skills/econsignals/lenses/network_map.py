"""Co-authorship network visualization lens for EconSignals.

Generates markdown reports with Mermaid flowchart diagrams showing author
collaboration networks. Can be centered on a single author or built from
all tracked authors.
"""

from __future__ import annotations

import sys
import json
import argparse
import sqlite3
from collections import Counter
from pathlib import Path
from datetime import date

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
PROJ_ROOT = SKILL_ROOT.parent.parent.parent

from lib.db import get_db, init_db, get_tracked_authors  # noqa: E402
from lib.authors import get_author_network  # noqa: E402

_MAX_NODES = 50

# ---------------------------------------------------------------------------
# Mermaid helpers
# ---------------------------------------------------------------------------


def _sanitize_mermaid_label(text: str) -> str:
    """Escape characters that break Mermaid double-quoted node labels.

    Mermaid node labels of the form ["..."] treat double-quotes and several
    other characters as syntax. This function produces a safe string that can
    be embedded directly inside the quotes.

    Args:
        text: Raw label text, may contain arbitrary Unicode.

    Returns:
        Sanitized text safe for use inside a Mermaid ["..."] label.

    Examples:
        >>> _sanitize_mermaid_label('Smith "John"')
        "Smith 'John'"
        >>> _sanitize_mermaid_label('Lab [MIT]')
        'Lab (MIT)'
    """
    # Double quotes would close the label string
    text = text.replace('"', "'")
    # Square brackets are used for Mermaid link/node syntax
    text = text.replace("[", "(").replace("]", ")")
    # Curly braces are used for shape modifiers
    text = text.replace("{", "(").replace("}", ")")
    # Semicolons can break graph statements
    text = text.replace(";", ",")
    # Pipe characters delimit edge labels
    text = text.replace("|", "/")
    return text


def _node_label(author: dict) -> str:
    """Build a multi-line Mermaid node label for an author.

    Lines:
        - Display name (required)
        - Affiliation (omitted when blank)
        - Paper count in DB

    Args:
        author: Author dict from the authors table.

    Returns:
        Label string using ``<br/>`` as line separator, suitable for
        embedding in a Mermaid ``["..."]`` node definition.
    """
    name = _sanitize_mermaid_label(author.get("name") or "Unknown")
    affiliation = author.get("affiliation") or ""
    paper_count = int(author.get("paper_count") or 0)

    parts = [name]
    if affiliation:
        parts.append(_sanitize_mermaid_label(affiliation))
    parts.append(f"{paper_count} papers")

    return "<br/>".join(parts)


def _generate_mermaid(graph: dict) -> str:
    """Convert a graph dict to a Mermaid flowchart string.

    Nodes are color-coded:
        - Tracked authors (``is_tracked=1``): green (#4CAF50)
        - Seed/initial authors that are not tracked: blue (#2196F3)
        - All others: default Mermaid style

    Args:
        graph: Output of :func:`_build_network_graph`.  Keys:
            ``"nodes"`` -- ``{author_id: author_dict}``
            ``"edges"`` -- ``{(a_id, b_id): {"shared_papers": N}}``

    Returns:
        Fenced Mermaid block (without the outer code fence markers; those
        are added by the caller to keep this function unit-testable).
    """
    nodes: dict[int, dict] = graph["nodes"]
    edges: dict[tuple[int, int], dict] = graph["edges"]

    lines: list[str] = ["graph LR"]

    if not nodes:
        lines.append('    NO_DATA["No authors found"]')
        return "\n".join(lines)

    # Node definitions
    for author_id, author in sorted(nodes.items(), key=lambda kv: kv[0]):
        node_id = f"A{author_id}"
        label = _node_label(author)
        lines.append(f'    {node_id}["{label}"]')

    # Edge definitions
    for (a_id, b_id), edge_data in sorted(edges.items()):
        shared = edge_data["shared_papers"]
        label_text = f"{shared} paper" + ("s" if shared != 1 else "")
        lines.append(f'    A{a_id} --> |"{label_text}"| A{b_id}')

    # Style declarations
    for author_id, author in nodes.items():
        node_id = f"A{author_id}"
        if author.get("is_tracked"):
            lines.append(f"    style {node_id} fill:#4CAF50,color:#fff")
        elif author.get("is_seed"):
            lines.append(f"    style {node_id} fill:#2196F3,color:#fff")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def _get_shared_jel(
    a_id: int,
    b_id: int,
    db: sqlite3.Connection,
    top_n: int = 3,
) -> list[str]:
    """Return the most frequent JEL codes across papers shared by two authors.

    Args:
        a_id: Author ID.
        b_id: Co-author ID.
        db: Open database connection.
        top_n: Maximum number of codes to return.

    Returns:
        List of JEL code strings, e.g. ``["O18", "R31"]``.
    """
    rows = db.execute(
        """
        SELECT p.jel_codes
        FROM papers p
        JOIN paper_authors pa1 ON pa1.paper_id = p.id AND pa1.author_id = ?
        JOIN paper_authors pa2 ON pa2.paper_id = p.id AND pa2.author_id = ?
        WHERE p.jel_codes IS NOT NULL
        """,
        (a_id, b_id),
    ).fetchall()

    counter: Counter[str] = Counter()
    for row in rows:
        raw = row[0]
        if not raw:
            continue
        try:
            codes = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(codes, list):
            counter.update(str(c) for c in codes)

    return [code for code, _ in counter.most_common(top_n)]


def _build_network_graph(
    author_ids: list[int],
    depth: int,
    min_shared: int,
    db: sqlite3.Connection,
) -> dict:
    """Build an adjacency structure by expanding co-authorship from seed authors.

    Starting from ``author_ids`` as the seed set, each depth level fetches
    direct co-authors.  The final node set is capped at :data:`_MAX_NODES`
    nodes, preferring seed authors then highest ``relevance_score``.  Edges
    are computed only among nodes in the final set and filtered by
    ``min_shared``.

    Args:
        author_ids: Seed author IDs (tracked authors or a single focal author).
        depth: Number of co-authorship hops to expand (1 or 2 recommended).
        min_shared: Minimum shared papers required for an edge.
        db: Open database connection.

    Returns:
        Dict with:
            ``"nodes"`` -- ``{author_id: author_dict}`` where author_dict
            includes an ``"is_seed"`` boolean.
            ``"edges"`` -- ``{(a_id, b_id): {"shared_papers": N}}`` where
            ``a_id < b_id`` always.
    """
    if not author_ids:
        return {"nodes": {}, "edges": {}}

    seed_ids: set[int] = set(author_ids)
    all_ids: set[int] = set(author_ids)
    current_frontier: list[int] = list(author_ids)

    for _depth_level in range(depth):
        if not current_frontier:
            break
        placeholders = ",".join("?" * len(current_frontier))
        rows = db.execute(
            f"""
            SELECT DISTINCT pa2.author_id
            FROM paper_authors pa1
            JOIN paper_authors pa2
                ON pa2.paper_id = pa1.paper_id
               AND pa2.author_id != pa1.author_id
            WHERE pa1.author_id IN ({placeholders})
            """,
            current_frontier,
        ).fetchall()
        new_ids = {int(r[0]) for r in rows} - all_ids
        all_ids.update(new_ids)
        current_frontier = list(new_ids)

    # Enforce node cap: seeds first, then by relevance_score DESC
    if len(all_ids) > _MAX_NODES:
        placeholders = ",".join("?" * len(all_ids))
        candidate_rows = db.execute(
            f"""
            SELECT id, relevance_score, is_tracked
            FROM authors
            WHERE id IN ({placeholders})
            """,
            list(all_ids),
        ).fetchall()

        def _sort_key(row: sqlite3.Row) -> tuple[int, float]:
            # Seed authors first (sort key 0), then by descending relevance
            is_non_seed = 0 if int(row["id"]) in seed_ids else 1
            score = float(row["relevance_score"] or 0.0)
            return (is_non_seed, -score)

        sorted_rows = sorted(candidate_rows, key=_sort_key)
        all_ids = {int(r["id"]) for r in sorted_rows[:_MAX_NODES]}
        seed_ids &= all_ids

    if not all_ids:
        return {"nodes": {}, "edges": {}}

    # Fetch full author records for retained nodes
    id_list = sorted(all_ids)
    placeholders = ",".join("?" * len(id_list))
    author_rows = db.execute(
        f"SELECT * FROM authors WHERE id IN ({placeholders})",
        id_list,
    ).fetchall()

    nodes: dict[int, dict] = {}
    for row in author_rows:
        d = dict(row)
        d["is_seed"] = d["id"] in seed_ids
        nodes[d["id"]] = d

    # Build edges: pairs within the retained node set with >= min_shared papers
    placeholders_double = ",".join("?" * len(id_list))
    edge_rows = db.execute(
        f"""
        SELECT
            pa1.author_id AS a,
            pa2.author_id AS b,
            COUNT(DISTINCT pa1.paper_id) AS shared_papers
        FROM paper_authors pa1
        JOIN paper_authors pa2
            ON pa2.paper_id = pa1.paper_id
           AND pa2.author_id != pa1.author_id
        WHERE pa1.author_id IN ({placeholders_double})
          AND pa2.author_id IN ({placeholders_double})
          AND pa1.author_id < pa2.author_id
        GROUP BY pa1.author_id, pa2.author_id
        HAVING shared_papers >= ?
        """,
        id_list + id_list + [min_shared],
    ).fetchall()

    edges: dict[tuple[int, int], dict] = {}
    for row in edge_rows:
        key = (int(row["a"]), int(row["b"]))
        edges[key] = {"shared_papers": int(row["shared_papers"])}

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Statistics and table renderers
# ---------------------------------------------------------------------------


def _compute_stats(graph: dict) -> dict:
    """Compute summary statistics for the network graph.

    Args:
        graph: Output of :func:`_build_network_graph`.

    Returns:
        Dict with keys: ``node_count``, ``edge_count``,
        ``avg_degree`` (float), ``most_connected`` (name, int) or None.
    """
    nodes: dict[int, dict] = graph["nodes"]
    edges: dict[tuple[int, int], dict] = graph["edges"]

    node_count = len(nodes)
    edge_count = len(edges)

    if node_count == 0:
        return {
            "node_count": 0,
            "edge_count": 0,
            "avg_degree": 0.0,
            "most_connected": None,
        }

    # Degree = number of distinct edges per node
    degree: dict[int, int] = {aid: 0 for aid in nodes}
    for a_id, b_id in edges:
        degree[a_id] = degree.get(a_id, 0) + 1
        degree[b_id] = degree.get(b_id, 0) + 1

    avg_degree = sum(degree.values()) / node_count if node_count else 0.0

    max_id = max(degree, key=lambda k: degree[k])
    most_connected = (nodes[max_id].get("name") or "Unknown", degree[max_id])

    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "avg_degree": avg_degree,
        "most_connected": most_connected,
    }


def _render_stats(stats: dict) -> list[str]:
    """Render network statistics as a markdown section.

    Args:
        stats: Output of :func:`_compute_stats`.

    Returns:
        List of markdown lines.
    """
    lines: list[str] = [
        "## Network Statistics",
        "",
        "| Metric | Value |",
        "|-|-|",
        f"| Authors | {stats['node_count']} |",
        f"| Connections | {stats['edge_count']} |",
        f"| Avg connections per author | {stats['avg_degree']:.1f} |",
    ]

    mc = stats.get("most_connected")
    if mc:
        lines.append(f"| Most connected | {mc[0]} ({mc[1]}) |")

    lines.append("")
    return lines


def _render_connections_table(
    graph: dict,
    db: sqlite3.Connection,
) -> list[str]:
    """Render the top connections as a sorted markdown table.

    Edges are sorted by shared paper count descending.  JEL codes are
    computed lazily from the shared papers' ``jel_codes`` field.

    Args:
        graph: Output of :func:`_build_network_graph`.
        db: Open database connection (used for JEL lookup).

    Returns:
        List of markdown lines.
    """
    nodes: dict[int, dict] = graph["nodes"]
    edges: dict[tuple[int, int], dict] = graph["edges"]

    if not edges:
        return [
            "## Connections",
            "",
            "*No co-authorship connections found.*",
            "",
        ]

    lines: list[str] = [
        "## Connections",
        "",
        "| Author A | Author B | Shared Papers | Top JEL |",
        "|-|-|-|-|",
    ]

    sorted_edges = sorted(edges.items(), key=lambda kv: kv[1]["shared_papers"], reverse=True)

    for (a_id, b_id), edge_data in sorted_edges:
        name_a = (nodes.get(a_id) or {}).get("name") or f"Author {a_id}"
        name_b = (nodes.get(b_id) or {}).get("name") or f"Author {b_id}"
        shared = edge_data["shared_papers"]
        jel_codes = _get_shared_jel(a_id, b_id, db)
        jel_str = ", ".join(jel_codes) if jel_codes else "-"
        lines.append(f"| {name_a} | {name_b} | {shared} | {jel_str} |")

    lines.append("")
    return lines


def _render_legend() -> list[str]:
    """Render a color legend for the Mermaid diagram.

    Returns:
        List of markdown lines.
    """
    return [
        "**Legend:** "
        "green nodes = tracked authors, "
        "blue nodes = auto-discovered / seed authors, "
        "default = other co-authors",
        "",
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_network_map(
    author_id: int | None = None,
    depth: int = 2,
    min_shared: int = 1,
) -> str:
    """Generate a co-authorship network visualization as markdown.

    When ``author_id`` is given the network is centered on that author and
    expanded outward by ``depth`` hops.  When ``None``, all tracked authors
    are used as seeds.

    The output contains:
    1. A Mermaid ``graph LR`` flowchart (capped at 50 nodes).
    2. Network statistics (node count, edge count, avg degree, most connected).
    3. A connection table sorted by shared paper count descending.

    Args:
        author_id: Focal author primary key, or ``None`` to use all tracked
            authors as the seed set.
        depth: Number of co-authorship hops to traverse from the seed set.
            Values above 2 can produce very large graphs.
        min_shared: Minimum number of shared papers required to include an
            edge in the diagram and connection table.

    Returns:
        Markdown string ready to write to a ``.md`` file.

    Raises:
        KeyError: If ``author_id`` is given but no such author exists.
    """
    db = get_db()

    # Determine seed author IDs and title
    if author_id is not None:
        # Validate author exists (get_author_network raises KeyError if not)
        center_row = db.execute(
            "SELECT * FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
        if center_row is None:
            db.close()
            raise KeyError(f"No author with id={author_id}")
        center = dict(center_row)
        seed_ids = [author_id]
        title = f"# Co-authorship Network: {center.get('name') or f'Author {author_id}'}"
        subtitle = (
            f"*Depth {depth} from {center.get('name') or f'Author {author_id}'}*"
        )
    else:
        tracked = get_tracked_authors()
        seed_ids = [a["id"] for a in tracked]
        title = "# Co-authorship Network: All Tracked Authors"
        subtitle = f"*{len(seed_ids)} tracked author(s), depth {depth}*"

    today = date.today().isoformat()

    graph = _build_network_graph(
        author_ids=seed_ids,
        depth=depth,
        min_shared=min_shared,
        db=db,
    )

    stats = _compute_stats(graph)
    mermaid_body = _generate_mermaid(graph)

    lines: list[str] = []

    # Header
    lines.append(title)
    lines.append("")
    lines.append(subtitle)
    lines.append(f"*Generated {today} | min shared papers: {min_shared}*")
    lines.append("")

    # Mermaid diagram
    lines.append("## Network Diagram")
    lines.append("")
    lines.extend(_render_legend())

    if stats["node_count"] == 0:
        lines.append(
            "*No co-authorship data found. Run `/scan authors` to populate the database.*"
        )
        lines.append("")
    elif stats["node_count"] == 1:
        only_id = next(iter(graph["nodes"]))
        only_name = (graph["nodes"][only_id].get("name") or "This author")
        lines.append(f"*{only_name} has no co-authors in the database.*")
        lines.append("")
    else:
        lines.append("```mermaid")
        lines.append(mermaid_body)
        lines.append("```")
        lines.append("")
        if stats["node_count"] >= _MAX_NODES:
            lines.append(
                f"*Diagram truncated to {_MAX_NODES} nodes (most relevant authors shown first).*"
            )
            lines.append("")

    # Statistics
    lines.extend(_render_stats(stats))

    # Connection table
    lines.extend(_render_connections_table(graph, db))

    db.close()
    return "\n".join(lines)


def write_network_map(
    author_id: int | None = None,
    depth: int = 2,
    min_shared: int = 1,
) -> Path:
    """Generate the network map and write it to the reports directory.

    Output path:
        - ``reports/{date}/network_map.md`` when ``author_id`` is ``None``.
        - ``reports/{date}/network_{author_id}.md`` when centered on an author.

    Args:
        author_id: Focal author ID, or ``None`` for the full tracked network.
        depth: Co-authorship hops to expand.
        min_shared: Minimum shared papers per edge.

    Returns:
        :class:`~pathlib.Path` of the written report file.
    """
    today = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today
    report_dir.mkdir(parents=True, exist_ok=True)

    filename = f"network_{author_id}.md" if author_id is not None else "network_map.md"
    out_path = report_dir / filename

    report = generate_network_map(author_id=author_id, depth=depth, min_shared=min_shared)
    out_path.write_text(report, encoding="utf-8")

    return out_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate EconSignals co-authorship network map."
    )
    parser.add_argument(
        "--id",
        type=int,
        default=None,
        metavar="AUTHOR_ID",
        help="Center the network on this author ID. Omit to use all tracked authors.",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="Co-authorship hops to expand from the seed set (default: 2).",
    )
    parser.add_argument(
        "--min-shared",
        type=int,
        default=1,
        dest="min_shared",
        help="Minimum shared papers required for an edge (default: 1).",
    )
    args = parser.parse_args()

    init_db()
    path = write_network_map(
        author_id=args.id,
        depth=args.depth,
        min_shared=args.min_shared,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "path": str(path),
                "author_id": args.id,
                "depth": args.depth,
                "min_shared": args.min_shared,
            }
        )
    )
