"""Deep paper analysis lens for EconSignals.

Generates a detailed markdown report for a single paper, including metadata,
authors, abstract, JEL codes, cross-source availability, related papers,
social discussion, and links.
"""

from __future__ import annotations

import sys
import json
import argparse
import sqlite3
from pathlib import Path
from datetime import date

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
PROJ_ROOT = SKILL_ROOT.parent.parent.parent

from lib.db import get_db, get_paper_with_sources, init_db


# ---------------------------------------------------------------------------
# JEL code descriptions
# ---------------------------------------------------------------------------

JEL_DESCRIPTIONS: dict[str, str] = {
    # Development Economics
    "O1": "Economic Development",
    "O10": "Economic Development: General",
    "O11": "Macroeconomic Analyses of Economic Development",
    "O12": "Microeconomic Analyses of Economic Development",
    "O15": "Human Resources; Income Distribution; Migration",
    "O16": "Financial Markets; Saving and Capital Investment",
    "O17": "Formal and Informal Sectors; Shadow Economy",
    "O18": "Urban, Rural, Regional, and Transportation Analysis; Housing",
    "O2": "Development Planning and Policy",
    "O3": "Innovation; Research and Development; Technological Change",
    # Regional and Urban Economics
    "R1": "General Regional Economics",
    "R10": "General Regional Economics: General",
    "R11": "Regional Economic Activity: Growth, Development, Environmental Issues",
    "R12": "Size and Spatial Distributions of Regional Economic Activity",
    "R13": "General Equilibrium and Welfare Economic Analysis of Regional Economies",
    "R14": "Land Use Patterns",
    "R2": "Household Analysis",
    "R20": "Household Analysis: General",
    "R21": "Housing Demand",
    "R23": "Regional Migration; Regional Labor Markets; Population",
    "R3": "Real Estate Markets, Spatial Production Analysis, and Firm Location",
    "R30": "Real Estate Markets: General",
    "R31": "Housing Supply and Markets",
    "R38": "Government Policy: Efficiency, Incidence, and Effects",
    "R4": "Transportation Economics",
    "R5": "Regional Government Analysis",
    "R58": "Regional Development Planning and Policy",
    # Econometrics and Statistics
    "C1": "Econometric and Statistical Methods: General",
    "C21": "Cross-Sectional Models; Spatial Models; Treatment Effect Models",
    "C23": "Panel Data Models; Spatio-temporal Models",
    "C25": "Discrete Regression and Qualitative Choice Models",
    "C26": "Instrumental Variables (IV) Estimation",
    "C31": "Cross-Sectional Models; Spatial Models (Applied)",
    "C36": "Instrumental Variables (IV) Estimation (Applied)",
    "C38": "Classification Methods; Cluster Analysis; Principal Components",
    "C5": "Econometric Modeling",
    "C52": "Model Evaluation, Validation, and Selection",
    # Public Economics
    "H4": "Publicly Provided Goods",
    "H41": "Public Goods",
    "H42": "Publicly Provided Private Goods",
    "H5": "National Government Expenditures and Related Policies",
    "H54": "Infrastructures; Other Public Investment and Capital Stock",
    "H7": "State and Local Government; Intergovernmental Relations",
    "H70": "State and Local Government: General",
    "H71": "State and Local Taxation, Subsidies, and Revenue",
    "H72": "State and Local Budget and Expenditures",
    "H77": "Intergovernmental Relations; Federalism; Secession",
    # Labor Economics
    "J1": "Demographic Economics",
    "J11": "Demographic Trends, Macroeconomic Effects, and Forecasts",
    "J13": "Fertility; Family Planning; Child Care; Children; Youth",
    "J15": "Economics of Minorities, Races, Indigenous Peoples, and Immigrants",
    "J2": "Demand and Supply of Labor",
    "J22": "Time Allocation and Labor Supply",
    "J23": "Labor Demand",
    "J3": "Wages, Compensation, and Labor Costs",
    "J31": "Wage Level and Structure; Wage Differentials",
    "J6": "Mobility, Unemployment, Vacancies, and Immigrant Workers",
    "J61": "Geographic Labor Mobility; Immigrant Workers",
    "J62": "Job, Occupational, and Intergenerational Mobility",
    # Health and Education
    "I1": "Health",
    "I10": "Health: General",
    "I12": "Health Behavior",
    "I14": "Health and Inequality",
    "I15": "Health and Economic Development",
    "I18": "Government Policy; Regulation; Public Health",
    "I2": "Education and Research Institutions",
    "I20": "Education: General",
    "I21": "Analysis of Education",
    "I24": "Education and Inequality",
    "I25": "Education and Economic Development",
    "I3": "Welfare, Well-Being, and Poverty",
    "I32": "Measurement and Analysis of Poverty",
    "I38": "Government Policy; Provision and Effects of Welfare Programs",
    # Environmental Economics
    "Q5": "Environmental Economics",
    "Q50": "Environmental Economics: General",
    "Q52": "Pollution Control Adoption and Costs",
    "Q53": "Air Pollution; Water Pollution; Noise; Hazardous Waste",
    "Q54": "Climate; Natural Disasters and Their Management",
    "Q56": "Environment and Development; Sustainability",
    # Economic History
    "N3": "Economic History: Labor and Consumers",
    "N35": "Economic History: Labor and Consumers; Asia",
    # Political Economy and Institutions
    "D7": "Analysis of Collective Decision-Making",
    "D72": "Political Processes: Rent-Seeking, Lobbying, Elections",
    "D73": "Bureaucracy; Administrative Processes; Corruption",
    # International Economics
    "F2": "International Factor Movements and International Business",
    "F22": "International Migration",
    "F6": "Economic Impacts of Globalization",
    # Finance
    "G2": "Financial Institutions and Services",
    "G21": "Banks; Depository Institutions; Micro Finance Institutions; Mortgages",
    # Industrial Organization - Infrastructure
    "L9": "Industry Studies: Transportation and Utilities",
    "L94": "Electric Utilities",
    # Law and Economics
    "K1": "Basic Areas of Law",
    "K11": "Property Law",
    "K42": "Illegal Behavior and the Enforcement of Law",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _score_bar(score: float) -> str:
    """Render a 10-block filled/empty bar for a 0-1 relevance score."""
    filled = int(round(score * 10))
    filled = max(0, min(10, filled))
    return "█" * filled + "░" * (10 - filled)


def _format_date(raw: str | None) -> str:
    """Return a human-readable date from an ISO string, or 'Unknown'."""
    if not raw:
        return "Unknown"
    return raw[:10]  # Take YYYY-MM-DD prefix regardless of format


def _openalex_url(openalex_id: str | None) -> str | None:
    if not openalex_id:
        return None
    oid = openalex_id.lstrip("https://openalex.org/")
    return f"https://openalex.org/{oid}"


def _s2_url(s2_id: str | None) -> str | None:
    if not s2_id:
        return None
    return f"https://www.semanticscholar.org/author/{s2_id}"


def _doi_url(doi: str | None) -> str | None:
    if not doi:
        return None
    if doi.startswith("http"):
        return doi
    return f"https://doi.org/{doi}"


def _get_related_papers(
    db: sqlite3.Connection, paper_id: int, author_ids: list[int], jel_codes: list[str]
) -> list[dict]:
    """Return up to 5 related papers by shared author or JEL code.

    Uses a single UNION query to collect candidates, then deduplicates and
    ranks by relevance_score descending.
    """
    if not author_ids and not jel_codes:
        return []

    seen: set[int] = {paper_id}
    candidates: dict[int, dict] = {}

    # Papers with overlapping authors
    if author_ids:
        placeholders = ",".join("?" * len(author_ids))
        rows = db.execute(
            f"""
            SELECT DISTINCT p.id, p.title, p.published_at, p.relevance_score,
                   p.jel_codes, p.doi, p.url
            FROM papers p
            JOIN paper_authors pa ON pa.paper_id = p.id
            WHERE pa.author_id IN ({placeholders})
              AND p.id != ?
            ORDER BY p.relevance_score DESC
            LIMIT 20
            """,
            (*author_ids, paper_id),
        ).fetchall()
        for row in rows:
            pid = row["id"]
            if pid not in seen:
                candidates[pid] = dict(row)

    # Papers with overlapping JEL codes (LIKE match per code)
    if jel_codes:
        for code in jel_codes[:5]:  # guard against huge code lists
            rows = db.execute(
                """
                SELECT DISTINCT p.id, p.title, p.published_at, p.relevance_score,
                       p.jel_codes, p.doi, p.url
                FROM papers p
                WHERE p.jel_codes LIKE ?
                  AND p.id != ?
                ORDER BY p.relevance_score DESC
                LIMIT 10
                """,
                (f'%"{code}"%', paper_id),
            ).fetchall()
            for row in rows:
                pid = row["id"]
                if pid not in seen:
                    candidates[pid] = dict(row)

    if not candidates:
        return []

    # Fetch authors for each candidate in one pass
    all_ids = list(candidates.keys())
    placeholders = ",".join("?" * len(all_ids))
    author_rows = db.execute(
        f"""
        SELECT pa.paper_id, a.name
        FROM authors a
        JOIN paper_authors pa ON pa.author_id = a.id
        WHERE pa.paper_id IN ({placeholders})
        ORDER BY pa.position
        """,
        all_ids,
    ).fetchall()

    author_map: dict[int, list[str]] = {}
    for row in author_rows:
        author_map.setdefault(row["paper_id"], []).append(row["name"])

    # Sort by relevance and return top 5
    ranked = sorted(
        candidates.values(), key=lambda p: p.get("relevance_score") or 0, reverse=True
    )[:5]

    result = []
    for p in ranked:
        p["authors"] = author_map.get(p["id"], [])
        result.append(p)
    return result


def _get_social_items(db: sqlite3.Connection, paper_id: int) -> list[dict]:
    rows = db.execute(
        """
        SELECT source, author_handle, content, url, published_at, engagement_score
        FROM social_items
        WHERE paper_id = ?
        ORDER BY published_at DESC
        LIMIT 20
        """,
        (paper_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_header(paper: dict, today: str) -> list[str]:
    title = paper.get("title") or "Untitled"
    lines = [
        f"# Deep Analysis: {title}",
        "",
        f"*Generated {today}*",
        "",
    ]
    return lines


def _render_metadata(paper: dict) -> list[str]:
    score = float(paper.get("relevance_score") or 0)
    bar = _score_bar(score)
    paper_type = (paper.get("paper_type") or "paper").replace("_", " ").title()
    pub_date = _format_date(paper.get("published_at"))
    doi = paper.get("doi") or ""
    doi_display = doi if doi else "—"

    lines = [
        "## Metadata",
        "",
        "| Field | Value |",
        "|-|-|",
        f"| Type | {paper_type} |",
        f"| Published | {pub_date} |",
        f"| DOI | {doi_display} |",
        f"| Relevance | {score:.2f} {bar} |",
        f"| Paper ID | {paper['id']} |",
        "",
    ]
    return lines


def _render_authors(paper: dict) -> list[str]:
    authors = paper.get("authors") or []
    if not authors:
        return ["## Authors", "", "*No author information available.*", ""]

    lines = ["## Authors", ""]
    for author in authors:
        name = author.get("name") or "Unknown"
        affiliation = author.get("affiliation") or ""
        country = author.get("country") or ""
        is_tracked = bool(author.get("is_tracked"))
        openalex_id = author.get("openalex_id")
        s2_id = author.get("semantic_scholar_id")

        tracked_badge = " **[TRACKED]**" if is_tracked else ""
        parts = [f"**{name}**{tracked_badge}"]

        meta_parts: list[str] = []
        if affiliation:
            meta_parts.append(affiliation)
        if country:
            meta_parts.append(country)
        if meta_parts:
            parts.append(", ".join(meta_parts))

        profile_links: list[str] = []
        oa_url = _openalex_url(openalex_id)
        if oa_url:
            profile_links.append(f"[OpenAlex]({oa_url})")
        s2_url = _s2_url(s2_id)
        if s2_url:
            profile_links.append(f"[Semantic Scholar]({s2_url})")
        if profile_links:
            parts.append(" | ".join(profile_links))

        lines.append("- " + " | ".join(parts))

    lines.append("")
    return lines


def _render_abstract(paper: dict) -> list[str]:
    abstract = paper.get("abstract") or ""
    lines = ["## Abstract", ""]
    if abstract:
        lines.append(abstract)
    else:
        lines.append("*No abstract available.*")
    lines.append("")
    return lines


def _render_jel_codes(paper: dict) -> list[str]:
    jel_codes = paper.get("jel_codes") or []
    if not jel_codes:
        return ["## JEL Codes", "", "*No JEL codes assigned.*", ""]

    lines = ["## JEL Codes", ""]
    for code in jel_codes:
        description = JEL_DESCRIPTIONS.get(code)
        if description:
            lines.append(f"- **{code}**: {description}")
        else:
            # Partial match: try the 2-char prefix
            prefix = code[:2] if len(code) >= 2 else code
            parent_desc = JEL_DESCRIPTIONS.get(prefix)
            if parent_desc:
                lines.append(f"- **{code}**: {parent_desc} (subcategory)")
            else:
                lines.append(f"- **{code}**")
    lines.append("")
    return lines


def _render_sources(paper: dict) -> list[str]:
    sources = paper.get("sources") or []
    if not sources:
        return ["## Sources", "", "*No source records found.*", ""]

    lines = [
        "## Sources",
        "",
        "| Source | ID | URL | Fetched |",
        "|-|-|-|-|",
    ]
    for src in sources:
        source_name = src.get("source") or "unknown"
        source_id = src.get("source_id") or "—"
        source_url = src.get("source_url") or ""
        fetched_at = _format_date(src.get("fetched_at"))
        url_cell = f"[link]({source_url})" if source_url else "—"
        lines.append(f"| {source_name} | {source_id} | {url_cell} | {fetched_at} |")
    lines.append("")
    return lines


def _render_related_papers(related: list[dict]) -> list[str]:
    if not related:
        return ["## Related Papers", "", "*No related papers found.*", ""]

    lines = ["## Related Papers", ""]
    for i, p in enumerate(related, 1):
        title = p.get("title") or "Untitled"
        doi = p.get("doi")
        url = p.get("url") or ""
        link_url = _doi_url(doi) or url
        score = float(p.get("relevance_score") or 0)
        bar = _score_bar(score)

        author_names: list[str] = p.get("authors") or []
        if len(author_names) <= 3:
            author_str = ", ".join(author_names) if author_names else "Unknown"
        else:
            author_str = ", ".join(author_names[:3]) + f" +{len(author_names) - 3}"

        pub = _format_date(p.get("published_at"))

        jel_raw = p.get("jel_codes")
        jel_str = ""
        if jel_raw:
            try:
                codes = json.loads(jel_raw) if isinstance(jel_raw, str) else jel_raw
                if codes:
                    jel_str = " | JEL: " + " ".join(f"`{c}`" for c in codes[:4])
            except (json.JSONDecodeError, TypeError):
                pass

        if link_url:
            lines.append(f"{i}. [{title}]({link_url})")
        else:
            lines.append(f"{i}. {title}")
        lines.append(f"   {author_str} | {pub} | {bar} {score:.2f}{jel_str}")
        lines.append("")

    return lines


def _render_social(social: list[dict]) -> list[str]:
    if not social:
        return ["## Social Discussion", "", "*No social items linked to this paper.*", ""]

    lines = ["## Social Discussion", ""]
    for item in social:
        source = item.get("source") or "unknown"
        handle = item.get("author_handle") or ""
        content = item.get("content") or ""
        url = item.get("url") or ""
        pub = _format_date(item.get("published_at"))
        engagement = item.get("engagement_score")

        header_parts = [f"**{source}**"]
        if handle:
            header_parts.append(f"@{handle}")
        header_parts.append(pub)
        if engagement is not None:
            header_parts.append(f"engagement: {engagement:.1f}")

        lines.append("> " + " | ".join(header_parts))
        if content:
            # Indent content lines for block-quote styling
            for content_line in content.splitlines():
                lines.append(f"> {content_line}")
        if url:
            lines.append(f"> [{url}]({url})")
        lines.append("")

    return lines


def _render_links(paper: dict) -> list[str]:
    lines = ["## Links", ""]

    doi_url = _doi_url(paper.get("doi"))
    if doi_url:
        lines.append(f"- DOI: [{doi_url}]({doi_url})")

    canonical_url = paper.get("url")
    if canonical_url and canonical_url != doi_url:
        lines.append(f"- URL: [{canonical_url}]({canonical_url})")

    sources = paper.get("sources") or []
    seen_urls: set[str] = {doi_url or "", canonical_url or ""}
    for src in sources:
        src_url = src.get("source_url") or ""
        if src_url and src_url not in seen_urls:
            source_name = src.get("source") or "source"
            lines.append(f"- {source_name.title()}: [{src_url}]({src_url})")
            seen_urls.add(src_url)

    if len(lines) == 2:
        lines.append("*No links available.*")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_deep_analysis(paper_id: int) -> str:
    """Generate detailed markdown analysis of a single paper.

    Sections:
    1. Title and metadata header
    2. Metadata table (type, date, DOI, relevance score)
    3. Authors with affiliations, tracking status, and profile links
    4. Full abstract
    5. JEL codes with descriptions
    6. Cross-source availability table
    7. Related papers (shared authors or JEL codes, up to 5)
    8. Social discussion (linked social_items)
    9. Links (DOI, source URLs)

    Args:
        paper_id: Primary key of the paper in the papers table.

    Returns:
        Markdown string for the report.

    Raises:
        KeyError: If no paper with the given id exists.
    """
    paper = get_paper_with_sources(paper_id)
    today = date.today().isoformat()

    db = get_db()

    # Gather author IDs for related-paper query
    author_ids = [a["id"] for a in (paper.get("authors") or []) if a.get("id")]
    jel_codes: list[str] = paper.get("jel_codes") or []

    related = _get_related_papers(db, paper_id, author_ids, jel_codes)
    social = _get_social_items(db, paper_id)

    db.close()

    sections: list[list[str]] = [
        _render_header(paper, today),
        _render_metadata(paper),
        _render_authors(paper),
        _render_abstract(paper),
        _render_jel_codes(paper),
        _render_sources(paper),
        _render_related_papers(related),
        _render_social(social),
        _render_links(paper),
    ]

    lines: list[str] = []
    for section in sections:
        lines.extend(section)

    return "\n".join(lines)


def write_deep_analysis(paper_id: int) -> Path:
    """Generate and write to reports/{date}/deep_{paper_id}.md.

    Args:
        paper_id: Primary key of the paper to analyze.

    Returns:
        Path to the written report file.
    """
    today = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today
    report_dir.mkdir(parents=True, exist_ok=True)

    report = generate_deep_analysis(paper_id)
    out_path = report_dir / f"deep_{paper_id}.md"
    out_path.write_text(report, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate deep analysis for a paper.")
    parser.add_argument("--id", type=int, required=True, help="Paper ID")
    args = parser.parse_args()

    init_db()
    path = write_deep_analysis(args.id)
    print(json.dumps({"status": "ok", "path": str(path), "paper_id": args.id}))
