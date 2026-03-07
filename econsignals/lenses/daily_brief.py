import argparse
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from econsignals.lib.db import get_db, get_recent_papers, get_upcoming_deadlines, init_db

PROJ_ROOT = Path(__file__).resolve().parents[2]


_TRUSTED_WORKING_PAPER_SOURCES = {
    "nber",
    "semantic_scholar",
    "repec_nep",
    "worldbank",
    "imf",
    "iza",
    "bread",
    "igc",
    "india_think_tanks",
    "rbi",
    "mospi",
    "jpal_sa",
}

_SOURCE_PRIORITY = {
    "nber": 100,
    "iza": 95,
    "bread": 92,
    "semantic_scholar": 90,
    "repec_nep": 88,
    "worldbank": 86,
    "imf": 84,
    "igc": 82,
    "jpal_sa": 80,
    "india_think_tanks": 76,
    "rbi": 74,
    "mospi": 72,
    "crossref": 60,
    "openalex": 40,
    "rss_feeds": 35,
}

_BLOCKED_DOI_PREFIXES = (
    "10.5281/",
    "10.17632/",
    "10.6084/",
)

_BLOCKED_DOMAINS = {
    "zenodo.org",
    "hal.science",
    "figshare.com",
    "osf.io",
    "arxiv.com",
    "researchsquare.com",
}

_ECON_TERMS_RE = re.compile(
    r"\b("
    r"econom(ic|ics)|development|poverty|inequality|labor|labour|employment|wage|"
    r"inflation|gdp|productivity|public finance|tax|fiscal|monetary|trade|"
    r"urban|housing|migration|education|health economics|microfinance|"
    r"randomized|rct|difference-in-differences|instrumental variable|"
    r"firm|household|consumption|expenditure|prices|policy"
    r")\b",
    re.IGNORECASE,
)

_ECON_JOURNAL_RE = re.compile(
    r"\b("
    r"economic|econometrica|economics|journal of|review of|"
    r"development|labor|labour|public economics|urban economics|"
    r"political economy|european economic"
    r")\b",
    re.IGNORECASE,
)


def _score_bar(score: float) -> str:
    filled = int(round(score * 10))
    filled = max(0, min(10, filled))
    return "#" * filled + "-" * (10 - filled)


def _paper_link(paper: dict) -> str:
    url = paper.get("url") or ""
    doi = paper.get("doi") or ""
    if url:
        return url
    if doi:
        return f"https://doi.org/{doi}"
    return ""


def _author_names_from_paper(paper: dict) -> list[str]:
    rows = paper.get("authors") or []
    names: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            name = (row.get("name") or "").strip()
            if name:
                names.append(name)
    return names or ["Unknown"]


def _format_author_str(names: list[str]) -> str:
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + f" +{len(names) - 3}"


def _abstract_snippet(abstract: str | None, length: int = 180) -> str:
    if not abstract:
        return "No abstract available."
    text = " ".join(abstract.split())
    if len(text) > length:
        return text[:length].rstrip() + "..."
    return text


def _type_label(paper_type: str | None) -> str:
    return (paper_type or "paper").replace("_", " ").title()


def _get_source_map(db, paper_ids: list[int]) -> dict[int, list[dict]]:
    if not paper_ids:
        return {}
    placeholders = ",".join("?" for _ in paper_ids)
    rows = db.execute(
        f"SELECT paper_id, source, source_url, raw_metadata FROM paper_sources WHERE paper_id IN ({placeholders})",
        paper_ids,
    ).fetchall()

    source_map: dict[int, list[dict]] = {pid: [] for pid in paper_ids}
    for paper_id, source, source_url, raw_meta in rows:
        meta = None
        if isinstance(raw_meta, str) and raw_meta:
            try:
                meta = json.loads(raw_meta)
            except json.JSONDecodeError:
                meta = None
        source_map.setdefault(paper_id, []).append(
            {
                "source": (source or "").strip().lower(),
                "source_url": (source_url or "").strip(),
                "raw_metadata": meta,
            }
        )
    return source_map


def _source_list(source_rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for src in sorted(
        (row.get("source") or "" for row in source_rows),
        key=lambda s: _SOURCE_PRIORITY.get(s, 0),
        reverse=True,
    ):
        if src and src not in seen:
            seen.add(src)
            ordered.append(src)
    return ordered


def _primary_source(source_rows: list[dict]) -> str:
    sources = _source_list(source_rows)
    return sources[0] if sources else "unknown"


def _has_blocked_repository_signal(paper: dict, source_rows: list[dict]) -> bool:
    doi = (paper.get("doi") or "").lower()
    for prefix in _BLOCKED_DOI_PREFIXES:
        if doi.startswith(prefix):
            return True

    link = (_paper_link(paper) or "").lower()
    if link:
        domain = urlparse(link).netloc.lower()
        if any(domain == d or domain.endswith("." + d) for d in _BLOCKED_DOMAINS):
            return True

    for row in source_rows:
        surl = (row.get("source_url") or "").lower()
        if not surl:
            continue
        domain = urlparse(surl).netloc.lower()
        if any(domain == d or domain.endswith("." + d) for d in _BLOCKED_DOMAINS):
            return True
    return False


def _extract_venue_tokens(source_rows: list[dict]) -> str:
    parts: list[str] = []
    for row in source_rows:
        meta = row.get("raw_metadata")
        if not isinstance(meta, dict):
            continue
        for key in (
            "container-title",
            "journal",
            "journal_name",
            "venue",
            "host_venue",
            "primary_location",
            "source",
        ):
            value = meta.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        parts.append(item)
            elif isinstance(value, dict):
                name = value.get("display_name") or value.get("name")
                if isinstance(name, str):
                    parts.append(name)
    return " ".join(parts)


def _econ_quality(paper: dict, source_rows: list[dict]) -> int:
    score = 0
    sources = _source_list(source_rows)
    source_set = set(sources)

    title = (paper.get("title") or "")
    abstract = (paper.get("abstract") or "")
    keywords = paper.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []

    text_blob = " ".join([title, abstract] + [str(k) for k in keywords])
    venue_blob = _extract_venue_tokens(source_rows)

    if _ECON_TERMS_RE.search(text_blob):
        score += 2
    if _ECON_JOURNAL_RE.search(venue_blob):
        score += 2

    jel_codes = paper.get("jel_codes") or []
    if isinstance(jel_codes, list) and jel_codes:
        score += 2

    trusted_hits = len(source_set.intersection(_TRUSTED_WORKING_PAPER_SOURCES))
    if trusted_hits:
        score += 3

    if "crossref" in source_set:
        score += 1  # journal feed, but noisy unless reinforced by econ signals
    if "openalex" in source_set and len(source_set) == 1:
        score -= 1

    if _has_blocked_repository_signal(paper, source_rows):
        score -= 5

    return score


def _why_line(paper: dict, source_rows: list[dict]) -> str:
    reasons: list[str] = []
    sources = _source_list(source_rows)
    if sources:
        reasons.append("source: " + ", ".join(sources[:2]))

    jel_codes = paper.get("jel_codes") or []
    if isinstance(jel_codes, list) and jel_codes:
        reasons.append("JEL " + ", ".join(jel_codes[:3]))

    text_blob = " ".join([
        str(paper.get("title") or ""),
        str(paper.get("abstract") or ""),
        " ".join(str(k) for k in (paper.get("keywords") or []) if isinstance(k, str)),
    ])
    if re.search(r"\bindia\b", text_blob, re.IGNORECASE):
        reasons.append("India-related")
    elif _ECON_TERMS_RE.search(text_blob):
        reasons.append("economics-topic match")

    if not reasons:
        reasons.append("high relevance score")
    return "; ".join(reasons)


def _render_priority_item(rank: int, paper: dict, source_rows: list[dict]) -> list[str]:
    score = float(paper.get("relevance_score") or 0)
    link = _paper_link(paper)
    title = paper.get("title") or "Untitled"
    label = _type_label(paper.get("paper_type"))
    authors = _format_author_str(_author_names_from_paper(paper))
    why = _why_line(paper, source_rows)

    lines: list[str] = []
    if link:
        lines.append(f"{rank}. [{title}]({link}) - {score:.2f}")
    else:
        lines.append(f"{rank}. {title} - {score:.2f}")
    lines.append(f"   {label} | Authors: {authors}")
    lines.append(f"   Why: {why}")
    lines.append("")
    return lines


def _render_track_table(title: str, papers: list[dict], source_map: dict[int, list[dict]], limit: int) -> list[str]:
    lines: list[str] = [f"## {title}", ""]
    if not papers:
        lines.extend(["No items.", ""])
        return lines

    lines.append("| Paper | Type | Source | Score |")
    lines.append("|-|-|-|-|")
    for paper in papers[:limit]:
        link = _paper_link(paper)
        raw_title = paper.get("title") or "Untitled"
        title_md = f"[{raw_title}]({link})" if link else raw_title
        ptype = _type_label(paper.get("paper_type"))
        source = _primary_source(source_map.get(paper["id"], []))
        score = float(paper.get("relevance_score") or 0)
        lines.append(f"| {title_md} | {ptype} | {source} | {score:.2f} |")
    lines.append("")
    return lines


def _sort_brief_rank(papers: list[dict]) -> list[dict]:
    return sorted(
        papers,
        key=lambda p: (
            int(p.get("_brief_quality") or 0),
            float(p.get("relevance_score") or 0),
            p.get("published_at") or "",
        ),
        reverse=True,
    )


def _build_priority_reads(journal_items: list[dict], wp_policy_items: list[dict], limit: int = 10) -> list[dict]:
    """Build a balanced top list with journal-first emphasis.

    Keeps the brief focused on journals while still showing related working
    paper and policy items.
    """
    priority: list[dict] = []
    used_ids: set[int] = set()

    for paper in journal_items[:6]:
        priority.append(paper)
        used_ids.add(paper["id"])
        if len(priority) >= limit:
            return priority

    for paper in wp_policy_items[:6]:
        if paper["id"] in used_ids:
            continue
        priority.append(paper)
        used_ids.add(paper["id"])
        if len(priority) >= limit:
            return priority

    for paper in _sort_brief_rank(journal_items + wp_policy_items):
        if paper["id"] in used_ids:
            continue
        priority.append(paper)
        used_ids.add(paper["id"])
        if len(priority) >= limit:
            break

    return priority


def _curate_econ_papers(papers: list[dict], source_map: dict[int, list[dict]]) -> list[dict]:
    enriched: list[dict] = []
    for paper in papers:
        source_rows = source_map.get(paper["id"], [])
        quality = _econ_quality(paper, source_rows)
        paper_copy = dict(paper)
        paper_copy["_brief_quality"] = quality
        enriched.append(paper_copy)

    curated = [p for p in enriched if int(p.get("_brief_quality") or 0) >= 2]

    return _sort_brief_rank(curated)


def _source_mix(curated: list[dict], source_map: dict[int, list[dict]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for paper in curated:
        primary = _primary_source(source_map.get(paper["id"], []))
        counts[primary] = counts.get(primary, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def generate_daily_brief(days: int = 1, limit: int = 20) -> str:
    today = date.today().isoformat()
    db = get_db()

    try:
        fetch_limit = max(limit * 8, 200)
        papers = get_recent_papers(days=days, limit=fetch_limit)

        # If the strict daily window is too sparse after filtering, broaden scope.
        effective_days = days
        if len(papers) < 50 and days < 7:
            papers = get_recent_papers(days=7, limit=fetch_limit)
            effective_days = 7

        source_map = _get_source_map(db, [p["id"] for p in papers])
        curated_all = _curate_econ_papers(papers, source_map)

        journal_items = _sort_brief_rank(
            [p for p in curated_all if (p.get("paper_type") or "") == "journal_article"]
        )
        wp_policy_items = _sort_brief_rank(
            [p for p in curated_all if (p.get("paper_type") or "") != "journal_article"]
        )
        priority_reads = _build_priority_reads(journal_items, wp_policy_items, limit=10)
        priority_ids = {p["id"] for p in priority_reads}
        curated = priority_reads + [
            p for p in curated_all if p["id"] not in priority_ids
        ]
        curated = curated[: max(limit, 30)]

        deadlines = get_upcoming_deadlines(days=30)

        lines: list[str] = []
        lines.append(f"# EconSignals Daily Brief - {today}")
        lines.append("")
        lines.append(
            f"*Curated economics items from the last {effective_days} day(s). "
            f"Filtered to remove repository noise and non-econ drift.*"
        )
        lines.append("")

        lines.append("## Snapshot")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|-|-|")
        lines.append(f"| Scanned candidates | {len(papers)} |")
        lines.append(f"| Curated economics items | {len(curated_all)} |")
        lines.append(f"| Items shown | {len(curated)} |")
        lines.append(f"| Journal articles | {len(journal_items)} |")
        lines.append(f"| Working papers/reports | {len(wp_policy_items)} |")
        lines.append("")

        lines.append("## Priority Reads")
        lines.append("")
        if priority_reads:
            for idx, paper in enumerate(priority_reads, 1):
                lines.extend(_render_priority_item(idx, paper, source_map.get(paper["id"], [])))
        else:
            lines.append("No high-confidence economics items matched the filter.")
            lines.append("")

        lines.extend(_render_track_table("Journal Track", journal_items, source_map, limit=8))
        lines.extend(_render_track_table("Working Paper and Policy Track", wp_policy_items, source_map, limit=8))

        mix = _source_mix(curated, source_map)
        if mix:
            lines.append("## Source Mix")
            lines.append("")
            lines.append("| Source | Count |")
            lines.append("|-|-|")
            for source, count in mix[:10]:
                lines.append(f"| {source} | {count} |")
            lines.append("")

        if deadlines:
            lines.append("## Upcoming Deadlines")
            lines.append("")
            lines.append("| Date | Type | Name | Organization |")
            lines.append("|-|-|-|-|")
            for d in deadlines[:10]:
                org = d.get("organization") or ""
                lines.append(f"| {d['deadline_date']} | {d['type']} | {d['name']} | {org} |")
            lines.append("")

        runs = db.execute(
            "SELECT sensor, status, finished_at, items_found, items_new FROM sensor_runs ORDER BY finished_at DESC LIMIT 50"
        ).fetchall()

        if runs:
            lines.append("## Sensor Health")
            lines.append("")
            lines.append("| Sensor | Status | Last Run | Found | New |")
            lines.append("|-|-|-|-|-|")
            seen: set[str] = set()
            for sensor, status, finished_at, items_found, items_new in runs:
                if sensor in seen:
                    continue
                seen.add(sensor)
                lines.append(
                    f"| {sensor} | {status} | {finished_at or 'never'} | {items_found} | {items_new} |"
                )
                if len(seen) >= 12:
                    break
            lines.append("")

        return "\n".join(lines)
    finally:
        db.close()


def write_brief(days: int = 1) -> Path:
    today = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today
    report_dir.mkdir(parents=True, exist_ok=True)

    brief = generate_daily_brief(days=days)
    out_path = report_dir / "daily_brief.md"
    out_path.write_text(brief, encoding="utf-8")

    print(json.dumps({"status": "ok", "path": str(out_path), "date": today}))
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate EconSignals daily brief.")
    parser.add_argument("--days", type=int, default=1, help="Look-back window in days")
    args = parser.parse_args()

    init_db()
    write_brief(days=args.days)
