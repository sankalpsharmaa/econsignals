"""Author spotlight lens for EconSignals.

Generates a detailed markdown profile for a single author, including
publication summary, JEL distribution, recent papers, co-authors,
social activity, and external profile links.
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

from lib.db import get_db, init_db
from lib.authors import get_author_papers, get_coauthors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_date(raw: str | None) -> str:
    """Return YYYY-MM-DD from an ISO string, or 'Unknown'."""
    if not raw:
        return "Unknown"
    return raw[:10]


def _score_bar(score: float) -> str:
    """Render a 10-block filled/empty bar for a 0-1 relevance score."""
    filled = int(round(score * 10))
    filled = max(0, min(10, filled))
    return "█" * filled + "░" * (10 - filled)


def _openalex_url(openalex_id: str | None) -> str | None:
    if not openalex_id:
        return None
    # Strip full URL prefix if stored as a URL rather than bare ID
    oid = openalex_id
    if oid.startswith("https://openalex.org/"):
        oid = oid[len("https://openalex.org/"):]
    return f"https://openalex.org/{oid}"


def _s2_url(s2_id: str | None) -> str | None:
    if not s2_id:
        return None
    return f"https://www.semanticscholar.org/author/{s2_id}"


def _orcid_url(orcid: str | None) -> str | None:
    if not orcid:
        return None
    return f"https://orcid.org/{orcid}"


def _get_author(db: sqlite3.Connection, author_id: int) -> dict:
    """Fetch author row and raise KeyError if not found."""
    row = db.execute("SELECT * FROM authors WHERE id = ?", (author_id,)).fetchone()
    if row is None:
        raise KeyError(f"No author with id={author_id}")
    return dict(row)


def _parse_jel_codes(raw: str | list | None) -> list[str]:
    """Deserialize jel_codes from JSON string or list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _jel_distribution(papers: list[dict]) -> list[tuple[str, int]]:
    """Return top-5 JEL codes by frequency across the given papers."""
    counter: Counter[str] = Counter()
    for paper in papers:
        codes = _parse_jel_codes(paper.get("jel_codes"))
        counter.update(codes)
    return counter.most_common(5)


def _get_paper_coauthors(
    db: sqlite3.Connection, paper_id: int, focal_author_id: int
) -> list[str]:
    """Return display names of all co-authors on a paper (excluding focal author)."""
    rows = db.execute(
        """
        SELECT a.name
        FROM authors a
        JOIN paper_authors pa ON pa.author_id = a.id
        WHERE pa.paper_id = ?
          AND a.id != ?
        ORDER BY pa.position
        """,
        (paper_id, focal_author_id),
    ).fetchall()
    return [r["name"] for r in rows]


def _get_social_items(
    db: sqlite3.Connection,
    bluesky_handle: str | None,
    twitter_handle: str | None,
    limit: int = 10,
) -> list[dict]:
    """Fetch recent social_items for this author by handle."""
    if not bluesky_handle and not twitter_handle:
        return []

    conditions: list[str] = []
    params: list[str] = []

    if bluesky_handle:
        conditions.append("author_handle = ?")
        params.append(bluesky_handle)
    if twitter_handle:
        conditions.append("author_handle = ?")
        params.append(twitter_handle)

    where = " OR ".join(conditions)
    rows = db.execute(
        f"""
        SELECT source, author_handle, content, url, published_at, paper_id
        FROM social_items
        WHERE {where}
        ORDER BY published_at DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_header(author: dict, today: str) -> list[str]:
    name = author.get("name") or "Unknown Author"
    affiliation = author.get("affiliation") or ""
    country = author.get("country") or ""

    meta_parts: list[str] = []
    if affiliation:
        meta_parts.append(affiliation)
    if country:
        meta_parts.append(country)

    lines = [f"# Author Spotlight: {name}", ""]
    if meta_parts:
        lines.append(f"*{', '.join(meta_parts)}*")
    lines.append(f"*Generated {today}*")
    lines.append("")
    return lines


def _render_profile(author: dict, papers: list[dict]) -> list[str]:
    # Tracking status
    is_tracked = bool(author.get("is_tracked"))
    auto_disc = author.get("auto_discovered", 0)
    if is_tracked:
        tracking_label = "Tracked"
    elif auto_disc == 1:
        tracking_label = "Auto-discovered (pending)"
    elif auto_disc == -1:
        tracking_label = "Auto-discovered (dismissed)"
    else:
        tracking_label = "Not tracked"

    paper_count = author.get("paper_count", 0) or 0

    # Avg relevance across fetched papers (more accurate than stored field)
    scores = [
        float(p["relevance_score"])
        for p in papers
        if p.get("relevance_score") is not None
    ]
    avg_relevance = sum(scores) / len(scores) if scores else 0.0

    openalex_id = author.get("openalex_id")
    s2_id = author.get("semantic_scholar_id")
    orcid = author.get("orcid")
    bluesky = author.get("bluesky_handle")
    twitter = author.get("twitter_handle")

    oa_url = _openalex_url(openalex_id)
    s2_url_str = _s2_url(s2_id)
    orcid_url = _orcid_url(orcid)

    lines = [
        "## Profile",
        "",
        "| Field | Value |",
        "|-|-|",
        f"| Tracking | {tracking_label} |",
        f"| Papers in DB | {paper_count} |",
        f"| Avg Relevance | {avg_relevance:.2f} |",
    ]

    if oa_url:
        lines.append(f"| OpenAlex | [link]({oa_url}) |")
    else:
        lines.append("| OpenAlex | — |")

    if s2_url_str:
        lines.append(f"| Semantic Scholar | [link]({s2_url_str}) |")
    else:
        lines.append("| Semantic Scholar | — |")

    if orcid_url:
        lines.append(f"| ORCID | [link]({orcid_url}) |")
    else:
        lines.append("| ORCID | — |")

    if bluesky:
        lines.append(f"| Bluesky | @{bluesky} |")
    if twitter:
        lines.append(f"| Twitter | @{twitter} |")

    lines.append("")
    return lines


def _render_jel_distribution(papers: list[dict]) -> list[str]:
    dist = _jel_distribution(papers)
    if not dist:
        return ["## JEL Distribution", "", "*No JEL codes found across papers.*", ""]

    lines = [
        "## JEL Distribution",
        "",
        "Top codes by frequency across all papers.",
        "",
        "| Code | Count |",
        "|-|-|",
    ]
    for code, count in dist:
        lines.append(f"| {code} | {count} |")
    lines.append("")
    return lines


def _render_recent_papers(
    papers: list[dict], author_id: int, db: sqlite3.Connection
) -> list[str]:
    recent = papers[:10]
    if not recent:
        return ["## Recent Papers", "", "*No papers found.*", ""]

    lines = ["## Recent Papers", ""]
    for i, paper in enumerate(recent, 1):
        title = paper.get("title") or "Untitled"
        doi = paper.get("doi")
        url = paper.get("url") or ""
        link_url = (
            (doi if doi and doi.startswith("http") else f"https://doi.org/{doi}")
            if doi
            else url
        )
        score = float(paper.get("relevance_score") or 0)
        bar = _score_bar(score)
        pub = _format_date(paper.get("published_at"))

        coauthors = _get_paper_coauthors(db, paper["id"], author_id)
        if len(coauthors) <= 3:
            coauthor_str = ", ".join(coauthors) if coauthors else "sole author"
        else:
            coauthor_str = ", ".join(coauthors[:3]) + f" +{len(coauthors) - 3}"

        if link_url:
            lines.append(f"{i}. [{title}]({link_url})")
        else:
            lines.append(f"{i}. {title}")
        lines.append(f"   {coauthor_str} | {pub} | {bar} {score:.2f}")
        lines.append("")

    return lines


def _render_coauthors(coauthors: list[dict]) -> list[str]:
    top = coauthors[:10]
    if not top:
        return ["## Top Co-authors", "", "*No co-authors found.*", ""]

    lines = [
        "## Top Co-authors",
        "",
        "| Name | Affiliation | Shared Papers | Tracked |",
        "|-|-|-|-|",
    ]
    for ca in top:
        name = ca.get("name") or "Unknown"
        affiliation = ca.get("affiliation") or "—"
        shared = ca.get("shared_papers", 0)
        tracked = "Yes" if ca.get("is_tracked") else "No"
        lines.append(f"| {name} | {affiliation} | {shared} | {tracked} |")

    lines.append("")
    return lines


def _render_social(social: list[dict]) -> list[str]:
    if not social:
        return ["## Social Activity", "", "*No social posts found for this author.*", ""]

    lines = ["## Social Activity", ""]
    for item in social:
        source = item.get("source") or "unknown"
        handle = item.get("author_handle") or ""
        content = item.get("content") or ""
        url = item.get("url") or ""
        pub = _format_date(item.get("published_at"))

        header_parts = [f"**{source}**"]
        if handle:
            header_parts.append(f"@{handle}")
        header_parts.append(pub)

        lines.append("> " + " | ".join(header_parts))
        if content:
            for content_line in content.splitlines():
                lines.append(f"> {content_line}")
        if url:
            lines.append(f"> [{url}]({url})")
        lines.append("")

    return lines


def _render_external_links(author: dict) -> list[str]:
    lines = ["## External Links", ""]

    oa_url = _openalex_url(author.get("openalex_id"))
    if oa_url:
        lines.append(f"- OpenAlex: [{oa_url}]({oa_url})")

    s2_url_str = _s2_url(author.get("semantic_scholar_id"))
    if s2_url_str:
        lines.append(f"- Semantic Scholar: [{s2_url_str}]({s2_url_str})")

    orcid_url = _orcid_url(author.get("orcid"))
    if orcid_url:
        lines.append(f"- ORCID: [{orcid_url}]({orcid_url})")

    if len(lines) == 2:
        lines.append("*No external profile links available.*")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_author_spotlight(author_id: int) -> str:
    """Generate detailed markdown profile for an author.

    Sections:
    1. Name and affiliation header
    2. Profile metadata (IDs, tracking status, handles)
    3. Publication summary (count, avg relevance, JEL distribution)
    4. Recent papers (up to 10, with relevance scores)
    5. Top co-authors (with shared paper counts)
    6. Social presence (recent posts if any)
    7. External links (OpenAlex, S2, ORCID profiles)

    Args:
        author_id: Primary key of the author in the authors table.

    Returns:
        Markdown string for the report.

    Raises:
        KeyError: If no author with the given id exists.
    """
    db = get_db()
    author = _get_author(db, author_id)
    today = date.today().isoformat()

    # Fetch all papers for JEL distribution; limit=20 for recent papers section
    papers = get_author_papers(author_id, db=db, limit=20)
    coauthors = get_coauthors(author_id, db=db)
    social = _get_social_items(
        db,
        bluesky_handle=author.get("bluesky_handle"),
        twitter_handle=author.get("twitter_handle"),
    )

    sections: list[list[str]] = [
        _render_header(author, today),
        _render_profile(author, papers),
        _render_jel_distribution(papers),
        _render_recent_papers(papers, author_id, db),
        _render_coauthors(coauthors),
        _render_social(social),
        _render_external_links(author),
    ]

    db.close()

    lines: list[str] = []
    for section in sections:
        lines.extend(section)

    return "\n".join(lines)


def write_author_spotlight(author_id: int) -> Path:
    """Generate and write to reports/{date}/author_{author_id}.md.

    Args:
        author_id: Primary key of the author to profile.

    Returns:
        Path to the written report file.
    """
    today = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today
    report_dir.mkdir(parents=True, exist_ok=True)

    report = generate_author_spotlight(author_id)
    out_path = report_dir / f"author_{author_id}.md"
    out_path.write_text(report, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate author spotlight report.")
    parser.add_argument("--id", type=int, required=True, help="Author ID")
    args = parser.parse_args()

    init_db()
    path = write_author_spotlight(args.id)
    print(json.dumps({"status": "ok", "path": str(path), "author_id": args.id}))
