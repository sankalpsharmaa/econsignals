import sys
import json
from pathlib import Path
from datetime import datetime, date

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
PROJ_ROOT = SKILL_ROOT.parent.parent.parent

from lib.db import get_recent_papers, get_upcoming_deadlines, get_db, init_db


def _score_bar(score: float) -> str:
    """Render a 10-block filled/empty bar for a 0-1 relevance score."""
    filled = int(round(score * 10))
    filled = max(0, min(10, filled))
    return "█" * filled + "░" * (10 - filled)


def _get_paper_authors(db, paper_id: int) -> list[str]:
    rows = db.execute(
        "SELECT a.name FROM authors a"
        " JOIN paper_authors pa ON a.id = pa.author_id"
        " WHERE pa.paper_id = ? ORDER BY pa.position",
        (paper_id,),
    ).fetchall()
    return [r[0] for r in rows] if rows else ["Unknown"]


def _format_author_str(names: list[str]) -> str:
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + f" +{len(names) - 3}"


def _abstract_snippet(abstract: str | None, length: int = 200) -> str:
    if not abstract:
        return "No abstract."
    snippet = abstract[:length]
    if len(abstract) > length:
        snippet += "..."
    return snippet


def _paper_link(paper: dict) -> str:
    url = paper.get("url") or ""
    doi = paper.get("doi") or ""
    if url:
        return url
    if doi:
        return f"https://doi.org/{doi}"
    return ""


def _jel_str(paper: dict) -> str:
    raw = paper.get("jel_codes")
    if not raw:
        return ""
    try:
        codes = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    return " ".join(f"`{j}`" for j in codes[:5])


def _type_label(paper_type: str | None) -> str:
    return (paper_type or "paper").replace("_", " ").title()


def _render_paper(index: int, paper: dict, authors: list[str]) -> list[str]:
    """Render a single paper block; returns a list of markdown lines."""
    score = float(paper.get("relevance_score") or 0)
    bar = _score_bar(score)
    link = _paper_link(paper)
    title = paper["title"]
    author_str = _format_author_str(authors)
    label = _type_label(paper.get("paper_type"))
    jel = _jel_str(paper)
    snippet = _abstract_snippet(paper.get("abstract"))

    lines: list[str] = []
    if link:
        lines.append(f"### {index}. [{title}]({link})")
    else:
        lines.append(f"### {index}. {title}")

    lines.append(f"**{author_str}** | {label} | {bar} {score:.2f}")
    if jel:
        lines.append(f"JEL: {jel}")
    lines.append(f"> {snippet}")
    lines.append("")
    return lines


def _group_papers(papers: list[dict]) -> dict[str, list[dict]]:
    """Return papers bucketed by paper_type, preserving insertion order."""
    groups: dict[str, list[dict]] = {}
    for p in papers:
        key = p.get("paper_type") or "other"
        groups.setdefault(key, []).append(p)
    return groups


def generate_daily_brief(days: int = 1, limit: int = 20) -> str:
    """Generate a markdown daily brief.

    Sections:
    1. Header with date and stats
    2. Top Papers (up to 10, relevance > 0)
       - Grouped by type when total > 5, flat list otherwise
    3. Upcoming Deadlines (within 30 days)
    4. Sensor Health (last run per sensor)
    """
    today = date.today().isoformat()
    papers = get_recent_papers(days=days, limit=limit)
    deadlines = get_upcoming_deadlines(days=30)
    db = get_db()

    lines: list[str] = []

    # --- Header ---
    lines.append(f"# EconSignals Daily Brief - {today}")
    lines.append("")
    lines.append(f"*{len(papers)} paper(s) from the last {days} day(s)*")
    lines.append("")

    # --- Top Papers ---
    top_papers = papers[:10]

    if top_papers:
        lines.append("## Top Papers")
        lines.append("")

        use_groups = len(top_papers) > 5
        if use_groups:
            groups = _group_papers(top_papers)
            counter = 1
            for group_key, group_papers in groups.items():
                lines.append(f"### {_type_label(group_key)}")
                lines.append("")
                for paper in group_papers:
                    authors = _get_paper_authors(db, paper["id"])
                    lines.extend(_render_paper(counter, paper, authors))
                    counter += 1
        else:
            for i, paper in enumerate(top_papers, 1):
                authors = _get_paper_authors(db, paper["id"])
                lines.extend(_render_paper(i, paper, authors))
    else:
        lines.append("## No New Papers")
        lines.append("")
        lines.append("Run `/scan papers` to collect papers.")
        lines.append("")

    # --- Upcoming Deadlines ---
    if deadlines:
        lines.append("## Upcoming Deadlines")
        lines.append("")
        lines.append("| Date | Type | Name | Organization |")
        lines.append("|-|-|-|-|")
        for d in deadlines[:10]:
            org = d.get("organization") or ""
            lines.append(
                f"| {d['deadline_date']} | {d['type']} | {d['name']} | {org} |"
            )
        lines.append("")

    # --- Sensor Health ---
    runs = db.execute(
        "SELECT sensor, status, finished_at, items_found, items_new"
        " FROM sensor_runs"
        " ORDER BY finished_at DESC"
        " LIMIT 10"
    ).fetchall()

    if runs:
        lines.append("## Sensor Health")
        lines.append("")
        lines.append("| Sensor | Status | Last Run | Found | New |")
        lines.append("|-|-|-|-|-|")
        seen: set[str] = set()
        for row in runs:
            sensor, status, finished_at, items_found, items_new = row
            if sensor not in seen:
                seen.add(sensor)
                lines.append(
                    f"| {sensor} | {status} | {finished_at or 'never'}"
                    f" | {items_found} | {items_new} |"
                )
        lines.append("")

    return "\n".join(lines)


def write_brief(days: int = 1) -> Path:
    """Generate and write the daily brief to reports/{YYYY-MM-DD}/daily_brief.md.

    Prints a JSON summary to stdout for Claude to consume.
    """
    today = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today
    report_dir.mkdir(parents=True, exist_ok=True)

    brief = generate_daily_brief(days=days)
    out_path = report_dir / "daily_brief.md"
    out_path.write_text(brief, encoding="utf-8")

    print(json.dumps({"status": "ok", "path": str(out_path), "date": today}))
    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate EconSignals daily brief.")
    parser.add_argument("--days", type=int, default=1, help="Look-back window in days")
    args = parser.parse_args()

    init_db()
    write_brief(days=args.days)
