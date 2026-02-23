"""Field pulse lens for EconSignals.

Analyzes topic trends, emerging methods, and geographic focus across
recent papers over a configurable time window. Compares current period
against the preceding equal-length window to identify rising/falling trends.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
PROJ_ROOT = SKILL_ROOT.parent.parent.parent

from lib.db import get_db, init_db

# ---------------------------------------------------------------------------
# JEL code descriptions (partial - common codes)
# ---------------------------------------------------------------------------

JEL_DESCRIPTIONS: dict[str, str] = {
    "C1": "Econometric Methods",
    "C2": "Econometric Methods: Single Equation Models",
    "C3": "Econometric Methods: Multiple/Simultaneous Models",
    "C4": "Econometric and Statistical Methods: Special Topics",
    "C5": "Econometric Modeling",
    "C6": "Mathematical Methods; Programming Models",
    "C7": "Game Theory and Bargaining Theory",
    "C8": "Data Collection and Estimation Methodology",
    "C9": "Design of Experiments",
    "D0": "Microeconomics: General",
    "D1": "Household Behavior and Family Economics",
    "D2": "Production and Organizations",
    "D3": "Distribution",
    "D4": "Market Structure, Pricing, and Design",
    "D6": "Welfare Economics",
    "D7": "Analysis of Collective Decision-Making",
    "D8": "Information, Knowledge, and Uncertainty",
    "D9": "Intertemporal Choice",
    "E0": "Macroeconomics: General",
    "E1": "General Aggregative Models",
    "E2": "Consumption, Saving, Production, Investment",
    "E3": "Prices, Business Fluctuations, and Cycles",
    "E4": "Money and Interest Rates",
    "E5": "Monetary Policy, Central Banking",
    "E6": "Macroeconomic Policy",
    "F0": "International Economics: General",
    "F1": "Trade",
    "F2": "International Factor Movements and International Business",
    "F3": "International Finance",
    "F4": "Macroeconomic Aspects of International Trade",
    "F6": "Economic Impacts of Globalization",
    "G0": "Financial Economics: General",
    "G1": "General Financial Markets",
    "G2": "Financial Institutions and Services",
    "G3": "Corporate Finance and Governance",
    "H0": "Public Economics: General",
    "H1": "Structure and Scope of Government",
    "H2": "Taxation, Subsidies, and Revenue",
    "H3": "Fiscal Policies and Behavior of Economic Agents",
    "H4": "Publicly Provided Goods",
    "H5": "National Government Expenditures and Related Policies",
    "H6": "National Budget, Deficit, and Debt",
    "H7": "State and Local Government; Intergovernmental Relations",
    "I0": "Health, Education, and Welfare: General",
    "I1": "Health",
    "I2": "Education and Research Institutions",
    "I3": "Welfare, Well-Being, and Poverty",
    "J0": "Labor and Demographic Economics: General",
    "J1": "Demographic Economics",
    "J2": "Demand and Supply of Labor",
    "J3": "Wages, Compensation, and Labor Costs",
    "J4": "Particular Labor Markets",
    "J5": "Labor-Management Relations, Trade Unions",
    "J6": "Mobility, Unemployment, Vacancies, and Immigrant Workers",
    "J7": "Labor Discrimination",
    "J8": "Labor Standards: National and International",
    "K0": "Law and Economics: General",
    "L0": "Industrial Organization: General",
    "L1": "Market Structure, Firm Strategy, and Market Performance",
    "L2": "Firm Objectives, Organization, and Behavior",
    "N0": "Economic History: General",
    "O1": "Economic Development",
    "O2": "Development Planning and Policy",
    "O3": "Innovation; Research and Development",
    "O4": "Economic Growth and Aggregate Productivity",
    "O5": "Economywide Country Studies",
    "O10": "Economic Development: General",
    "O12": "Microeconomic Analyses of Economic Development",
    "O15": "Human Resources; Income Distribution; Migration",
    "O16": "Financial Markets; Saving and Capital Investment",
    "O17": "Formal and Informal Sectors; Shadow Economy",
    "O18": "Urban, Rural, Regional, and Transportation Analysis",
    "O2": "Development Planning and Policy",
    "Q0": "Agricultural and Natural Resource Economics",
    "Q1": "Agriculture",
    "Q5": "Environmental Economics",
    "R0": "Urban, Rural, Regional, Real Estate, and Transportation Economics",
    "R1": "General Regional Economics",
    "R2": "Household Analysis",
    "R3": "Real Estate Markets, Spatial Production Analysis",
    "R4": "Transportation Economics",
    "R5": "Regional Government Analysis",
}


def _jel_description(code: str) -> str:
    """Return a human-readable description for a JEL code."""
    # Try exact match first, then 2-char prefix, then 1-char prefix
    if code in JEL_DESCRIPTIONS:
        return JEL_DESCRIPTIONS[code]
    if code[:2] in JEL_DESCRIPTIONS:
        return JEL_DESCRIPTIONS[code[:2]]
    if code[:1] in JEL_DESCRIPTIONS:
        return JEL_DESCRIPTIONS[code[:1]]
    return "—"


# ---------------------------------------------------------------------------
# Method detection patterns
# ---------------------------------------------------------------------------

METHODS: dict[str, str] = {
    "RCT/Experiment": r"\b(rct|randomized?\s+(control|experiment)|field\s+experiment)\b",
    "Difference-in-Differences": r"\b(diff(erence)?[\s-]in[\s-]diff|did\b|difference.in.difference)",
    "Regression Discontinuity": r"\b(regression\s+discontinuity|rdd?\b|sharp\s+discontinuity|fuzzy\s+discontinuity)",
    "Instrumental Variables": r"\b(instrumental\s+variable|iv\s+estimat|\b2sls\b|two.stage)",
    "Machine Learning": r"\b(machine\s+learning|random\s+forest|neural\s+network|deep\s+learning|nlp\b|lasso\b|xgboost)",
    "Synthetic Control": r"\b(synthetic\s+control|synth\s+method)",
    "Bunching": r"\b(bunching\s+(estimat|design|analysis))",
    "Event Study": r"\b(event\s+stud)",
    "Panel Data": r"\b(panel\s+data|fixed\s+effect|random\s+effect)",
    "Survey/Qualitative": r"\b(survey\s+data|qualitative|interview|focus\s+group)",
}

_COMPILED_METHODS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE) for name, pattern in METHODS.items()
}

# ---------------------------------------------------------------------------
# Geographic patterns
# ---------------------------------------------------------------------------

COUNTRIES_AND_REGIONS: list[str] = [
    # South Asia
    "India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal", "Bhutan",
    # East / Southeast Asia
    "China", "Japan", "South Korea", "Indonesia", "Vietnam", "Thailand",
    "Philippines", "Malaysia", "Myanmar", "Cambodia",
    # Sub-Saharan Africa
    "Nigeria", "Kenya", "Ethiopia", "Ghana", "Tanzania", "Uganda",
    "Rwanda", "Senegal", "Zambia", "Mozambique", "South Africa",
    # Latin America
    "Brazil", "Mexico", "Colombia", "Peru", "Chile", "Argentina",
    "Ecuador", "Bolivia",
    # MENA
    "Egypt", "Morocco", "Turkey", "Iran",
    # High-income / Other
    "United States", "United Kingdom", "Germany", "France",
    # Regions
    "Sub-Saharan Africa", "South Asia", "Latin America",
    "Southeast Asia", "East Africa", "West Africa", "Middle East",
    "OECD", "Europe",
]

_COMPILED_GEO: dict[str, re.Pattern[str]] = {
    name: re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
    for name in COUNTRIES_AND_REGIONS
}

# ---------------------------------------------------------------------------
# Stopwords for keyword extraction
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset(
    [
        # Common English
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "this", "that",
        "these", "those", "we", "our", "their", "its", "it", "as", "if",
        "not", "no", "so", "than", "into", "over", "after", "between",
        "through", "across", "during", "within", "without", "both",
        "each", "such", "other", "more", "most", "also", "can", "all",
        "up", "out", "about", "which", "how", "when", "where", "who",
        "they", "them", "then", "there", "while", "among", "under", "new",
        "well", "large", "high", "low", "long", "short", "good", "first",
        "second", "third", "one", "two", "three", "per", "via",
        # Common academic
        "paper", "study", "results", "analysis", "evidence", "effect",
        "effects", "using", "based", "data", "model", "models", "approach",
        "approach", "framework", "literature", "findings", "research",
        "show", "shows", "find", "finds", "found", "examine", "examines",
        "explore", "explores", "investigate", "investigates", "test", "tests",
        "estimate", "estimates", "estimated", "provide", "provides",
        "suggest", "suggests", "recent", "existing", "important", "key",
        "significant", "significantly", "however", "therefore", "thus",
        "although", "despite", "across", "large", "large-scale",
        "international", "national", "local", "public", "private",
        "abstract", "introduction", "conclusion", "section", "table",
        "figure", "equation", "sample", "variable", "variables", "robust",
        "consistent", "empirical", "theoretical", "economic", "economics",
        "market", "markets", "policy", "policies", "government", "social",
        "overall", "average", "increase", "decrease", "higher", "lower",
        "role", "impact", "level", "levels", "use", "used", "case",
        "whether", "related", "given", "identify", "number", "different",
        "associated", "country", "countries", "time", "year", "years",
        "period", "sample", "set", "two", "three", "standard",
    ]
)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


class PaperWindow(NamedTuple):
    papers: list[dict]
    start_iso: str
    end_iso: str


def _fetch_window(days: int, offset_days: int = 0) -> PaperWindow:
    """Fetch all papers whose first_seen_at falls within a time window.

    Args:
        days: Width of the window in days.
        offset_days: How many days ago the window *ends*. 0 = now.

    Returns:
        PaperWindow with papers and boundary timestamps.
    """
    now = datetime.now(timezone.utc)
    end = now - timedelta(days=offset_days)
    start = end - timedelta(days=days)

    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    db = get_db()
    rows = db.execute(
        """
        SELECT p.id, p.title, p.abstract, p.jel_codes, p.keywords,
               p.published_at, p.first_seen_at,
               ps.source
        FROM papers p
        LEFT JOIN paper_sources ps ON ps.paper_id = p.id
        WHERE p.first_seen_at >= ? AND p.first_seen_at < ?
        ORDER BY p.first_seen_at DESC
        """,
        (start_iso, end_iso),
    ).fetchall()
    db.close()

    # Deduplicate by paper id (LEFT JOIN can produce multiple rows per paper)
    seen_ids: set[int] = set()
    papers: list[dict] = []
    for row in rows:
        d = dict(row)
        pid = d["id"]
        if pid not in seen_ids:
            seen_ids.add(pid)
            for field in ("jel_codes", "keywords"):
                raw = d.get(field)
                if raw and isinstance(raw, str):
                    try:
                        d[field] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
                elif not raw:
                    d[field] = []
            papers.append(d)

    return PaperWindow(papers=papers, start_iso=start_iso, end_iso=end_iso)


def _fetch_sources_for_window(days: int, offset_days: int = 0) -> Counter[str]:
    """Count papers per source within a time window."""
    now = datetime.now(timezone.utc)
    end = now - timedelta(days=offset_days)
    start = end - timedelta(days=days)

    db = get_db()
    rows = db.execute(
        """
        SELECT ps.source, COUNT(DISTINCT p.id) as cnt
        FROM papers p
        JOIN paper_sources ps ON ps.paper_id = p.id
        WHERE p.first_seen_at >= ? AND p.first_seen_at < ?
        GROUP BY ps.source
        ORDER BY cnt DESC
        """,
        (
            start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    ).fetchall()
    db.close()
    return Counter({row["source"]: row["cnt"] for row in rows})


def _fetch_top_authors(days: int) -> list[dict]:
    """Return authors with most papers in the recent window."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    db = get_db()
    rows = db.execute(
        """
        SELECT a.name, a.affiliation, COUNT(pa.paper_id) as paper_count
        FROM authors a
        JOIN paper_authors pa ON pa.author_id = a.id
        JOIN papers p ON p.id = pa.paper_id
        WHERE p.first_seen_at >= ?
        GROUP BY a.id
        ORDER BY paper_count DESC
        LIMIT 10
        """,
        (cutoff,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Analysis: JEL code trends
# ---------------------------------------------------------------------------


def _count_jel_codes(papers: list[dict]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for paper in papers:
        codes = paper.get("jel_codes") or []
        if isinstance(codes, list):
            counter.update(codes)
    return counter


def _jel_trend_rows(
    current: Counter[str],
    previous: Counter[str],
    top_n: int = 10,
) -> list[dict]:
    """Build rows for the JEL trends table.

    Includes top_n codes by current count, computing change vs previous.
    """
    top_codes = [code for code, _ in current.most_common(top_n)]

    # Also include any rising codes not in top_n
    all_codes = set(current) | set(previous)
    rising_extras: list[str] = []
    for code in all_codes:
        if code in top_codes:
            continue
        prev_cnt = previous[code]
        curr_cnt = current[code]
        if prev_cnt == 0 and curr_cnt > 0:
            rising_extras.append(code)
        elif prev_cnt > 0 and (curr_cnt - prev_cnt) / prev_cnt > 0.5:
            rising_extras.append(code)

    display_codes = top_codes + sorted(rising_extras)[:5]

    rows: list[dict] = []
    for code in display_codes:
        curr_cnt = current[code]
        prev_cnt = previous[code]
        if prev_cnt == 0 and curr_cnt > 0:
            change = "NEW"
        elif prev_cnt == 0:
            change = "—"
        else:
            delta = curr_cnt - prev_cnt
            change = f"+{delta}" if delta >= 0 else str(delta)

        rows.append(
            {
                "code": code,
                "count": curr_cnt,
                "change": change,
                "description": _jel_description(code),
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Analysis: Keyword / topic trends
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split into tokens."""
    text = text.lower()
    text = re.sub(r"[^a-z\s]", " ", text)
    return [t for t in text.split() if len(t) > 2 and t not in _STOPWORDS]


def _extract_ngrams(tokens: list[str], n: int) -> list[str]:
    """Return all n-grams from a token list as space-joined strings."""
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _count_topics(papers: list[dict], min_papers: int = 3) -> Counter[str]:
    """Count bigrams and trigrams appearing in at least min_papers papers."""
    # Per-paper ngram sets to avoid counting repeated terms within one abstract
    paper_ngrams: list[set[str]] = []
    for paper in papers:
        text = (paper.get("title") or "") + " " + (paper.get("abstract") or "")
        tokens = _tokenize(text)
        ngrams: set[str] = set()
        ngrams.update(_extract_ngrams(tokens, 2))
        ngrams.update(_extract_ngrams(tokens, 3))
        paper_ngrams.append(ngrams)

    # Count how many papers contain each ngram
    global_counter: Counter[str] = Counter()
    for ngrams in paper_ngrams:
        global_counter.update(ngrams)

    return Counter(
        {ng: cnt for ng, cnt in global_counter.items() if cnt >= min_papers}
    )


def _topic_trend_rows(
    current: Counter[str],
    previous: Counter[str],
    top_n: int = 15,
) -> list[dict]:
    top = current.most_common(top_n)
    rows: list[dict] = []
    for topic, count in top:
        prev_cnt = previous.get(topic, 0)
        if prev_cnt == 0:
            trend = "NEW"
        else:
            delta = count - prev_cnt
            trend = f"+{delta}" if delta >= 0 else str(delta)
        rows.append({"topic": topic, "count": count, "trend": trend})
    return rows


# ---------------------------------------------------------------------------
# Analysis: Method detection
# ---------------------------------------------------------------------------


def _count_methods(papers: list[dict]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for paper in papers:
        text = (paper.get("abstract") or "") + " " + (paper.get("title") or "")
        for method_name, pattern in _COMPILED_METHODS.items():
            if pattern.search(text):
                counter[method_name] += 1
    return counter


# ---------------------------------------------------------------------------
# Analysis: Geographic focus
# ---------------------------------------------------------------------------


def _count_geo(papers: list[dict]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for paper in papers:
        text = (paper.get("abstract") or "") + " " + (paper.get("title") or "")
        mentioned: set[str] = set()
        for name, pattern in _COMPILED_GEO.items():
            if pattern.search(text) and name not in mentioned:
                mentioned.add(name)
        counter.update(mentioned)
    return counter


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------


def _render_jel_table(rows: list[dict]) -> list[str]:
    if not rows:
        return ["*No JEL codes found in this period.*", ""]

    lines = [
        "| Code | Count | Change | Description |",
        "|-|-|-|-|",
    ]
    for row in rows:
        lines.append(
            f"| {row['code']} | {row['count']} | {row['change']} | {row['description']} |"
        )
    lines.append("")
    return lines


def _render_topic_table(rows: list[dict]) -> list[str]:
    if not rows:
        return ["*No recurring topics found (need 3+ papers per term).*", ""]

    lines = [
        "| Topic | Count | Trend |",
        "|-|-|-|",
    ]
    for row in rows:
        lines.append(f"| {row['topic']} | {row['count']} | {row['trend']} |")
    lines.append("")
    return lines


def _render_methods_table(method_counts: Counter[str], total_papers: int) -> list[str]:
    if not method_counts:
        return ["*No recognized methods detected.*", ""]

    lines = [
        "| Method | Papers | Share |",
        "|-|-|-|",
    ]
    for method, count in method_counts.most_common():
        share = f"{100 * count / total_papers:.0f}%" if total_papers else "—"
        lines.append(f"| {method} | {count} | {share} |")
    lines.append("")
    return lines


def _render_geo_table(geo_counts: Counter[str], top_n: int = 15) -> list[str]:
    top = geo_counts.most_common(top_n)
    if not top:
        return ["*No country or region mentions detected.*", ""]

    lines = [
        "| Region/Country | Mentions |",
        "|-|-|",
    ]
    for name, count in top:
        lines.append(f"| {name} | {count} |")
    lines.append("")
    return lines


def _render_sources_table(source_counts: Counter[str]) -> list[str]:
    if not source_counts:
        return ["*No source data available.*", ""]

    lines = [
        "| Source | Papers |",
        "|-|-|",
    ]
    for source, count in source_counts.most_common(10):
        lines.append(f"| {source} | {count} |")
    lines.append("")
    return lines


def _render_authors_table(author_rows: list[dict]) -> list[str]:
    if not author_rows:
        return ["*No author data available.*", ""]

    lines = [
        "| Author | Papers | Affiliation |",
        "|-|-|-|",
    ]
    for row in author_rows:
        name = row.get("name") or "Unknown"
        count = row.get("paper_count", 0)
        affiliation = row.get("affiliation") or "—"
        lines.append(f"| {name} | {count} | {affiliation} |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_field_pulse(days: int = 30) -> str:
    """Analyze trends across recent papers and generate markdown report.

    Analyses:
    1. JEL Code Trends: Count JEL codes in recent papers, compare to previous
       period. Show rising and falling codes.
    2. Keyword/Topic Trends: Extract frequent bigrams/trigrams from titles and
       abstracts. Identify emerging topics vs older period.
    3. Method Mentions: Scan abstracts for methodology terms and count
       frequency changes.
    4. Geographic Focus: Count country/region mentions in abstracts.
    5. Source Activity: Which sources are producing the most papers.
    6. Top Authors This Period: Authors with most papers in the window.

    Args:
        days: Look-back window in days. The preceding equal-length window
              is used as the comparison baseline.

    Returns:
        Markdown string for the report.
    """
    today = date.today().isoformat()

    # Fetch current and previous windows
    current_window = _fetch_window(days=days, offset_days=0)
    previous_window = _fetch_window(days=days, offset_days=days)

    current_papers = current_window.papers
    previous_papers = previous_window.papers
    n_current = len(current_papers)
    n_previous = len(previous_papers)

    # --- JEL trends ---
    current_jel = _count_jel_codes(current_papers)
    previous_jel = _count_jel_codes(previous_papers)
    jel_rows = _jel_trend_rows(current_jel, previous_jel)

    # --- Topic trends ---
    min_papers = max(2, n_current // 20)  # scale threshold with corpus size
    current_topics = _count_topics(current_papers, min_papers=min_papers)
    previous_topics = _count_topics(previous_papers, min_papers=1)
    topic_rows = _topic_trend_rows(current_topics, previous_topics)

    # --- Methods ---
    method_counts = _count_methods(current_papers)

    # --- Geographic focus ---
    geo_counts = _count_geo(current_papers)

    # --- Source activity ---
    source_counts = _fetch_sources_for_window(days=days, offset_days=0)

    # --- Top authors ---
    top_authors = _fetch_top_authors(days=days)

    # ---------------------------------------------------------------------------
    # Assemble report
    # ---------------------------------------------------------------------------

    lines: list[str] = []

    # Header
    lines.append(f"# Field Pulse - {today}")
    lines.append("")
    lines.append(
        f"*Analysis of {n_current} papers from the last {days} days"
        f" (vs {n_previous} papers in the preceding {days}-day period)*"
    )
    lines.append("")

    # JEL Code Trends
    lines.append("## JEL Code Trends")
    lines.append("")
    lines.extend(_render_jel_table(jel_rows))

    # Emerging Topics
    lines.append("## Emerging Topics")
    lines.append("")
    lines.extend(_render_topic_table(topic_rows))

    # Methods in Use
    lines.append("## Methods in Use")
    lines.append("")
    lines.extend(_render_methods_table(method_counts, n_current))

    # Geographic Focus
    lines.append("## Geographic Focus")
    lines.append("")
    lines.extend(_render_geo_table(geo_counts))

    # Source Activity
    lines.append("## Source Activity")
    lines.append("")
    lines.extend(_render_sources_table(source_counts))

    # Most Active Authors
    lines.append("## Most Active Authors")
    lines.append("")
    lines.extend(_render_authors_table(top_authors))

    return "\n".join(lines)


def write_field_pulse(days: int = 30) -> Path:
    """Generate and write field pulse report to reports/{date}/field_pulse.md.

    Prints a JSON summary to stdout for Claude to consume.

    Args:
        days: Look-back window in days.

    Returns:
        Path to the written report file.
    """
    today = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today
    report_dir.mkdir(parents=True, exist_ok=True)

    report = generate_field_pulse(days=days)
    out_path = report_dir / "field_pulse.md"
    out_path.write_text(report, encoding="utf-8")

    print(json.dumps({"status": "ok", "path": str(out_path), "date": today, "days": days}))
    return out_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate EconSignals field pulse report.")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look-back window in days (default: 30)",
    )
    args = parser.parse_args()

    init_db()
    write_field_pulse(days=args.days)
