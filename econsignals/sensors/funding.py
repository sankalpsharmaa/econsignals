"""Funding deadline sensor for EconSignals.

Inserts economics research funding deadlines into the deadlines table from two
layers:

1. Grants.gov search2 (primary structured layer). A public, no-auth JSON
   endpoint returning federal opportunities with structured MM/DD/YYYY
   close dates. Date-less / unrelated hits are dropped, so this yields real
   dated records rather than scraped guesses.
2. A curated registry of field-specific funders (PEDL, STEG, IGC, J-PAL,
   Weiss, ...) that are not listed on Grants.gov. These pages are
   JavaScript-rendered SPAs whose deadline tables never appear in the initial
   HTML, so regex-on-HTML scraping captures zero real dates; the curated
   known_deadlines recurrences are the reliable source. Live HTML scraping is
   demoted to an override that only supplements a curated entry when a real
   date is parsed from the page.

Usage:
    python -m econsignals.sensors.funding
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

from econsignals.sensors._base import BaseSensor

# ---------------------------------------------------------------------------
# Grants.gov structured source (primary layer)
# ---------------------------------------------------------------------------

# Grants.gov exposes a public, no-auth JSON search endpoint (search2) that
# returns every federal funding opportunity with a structured, dated
# ``closeDate``. Verified live 2026-05-28: a POST of {"keyword", "rows",
# "oppStatuses", optional "cfda"} returns {"data": {"oppHits": [...]}} where
# each hit carries id, number, title, agency, oppStatus, openDate, closeDate
# (all MM/DD/YYYY, closeDate empty for rolling/standing programs).
#
# This is the structured replacement for HTML scraping: it yields real dated
# records, so date-less hits are dropped. The curated FUNDING_SOURCES registry
# remains the primary layer for the field-specific funders (PEDL, STEG, IGC,
# J-PAL, ...) that are not on Grants.gov; Grants.gov is additive on top.
_GRANTS_GOV_URL = "https://api.grants.gov/v1/api/search2"

# Each probe is a targeted query. A bare "economics" keyword is mostly noise
# (embassy programs, unrelated agency calls), so we scope by the NSF Social,
# Behavioral & Economic Sciences CFDA (47.075) and by research-oriented keyword
# phrases. ``label`` is appended to the opportunity name for provenance.
_GRANTS_GOV_QUERIES: list[dict] = [
    {
        "label": "development economics",
        "body": {"keyword": "development economics", "rows": 40},
        "org": "US Federal (Grants.gov)",
    },
    {
        "label": "economics social science",
        "body": {"keyword": "economics social science research", "rows": 40},
        "org": "US Federal (Grants.gov)",
    },
]

# Statuses worth surfacing: open ("posted") and announced-but-not-yet-open
# ("forecasted"). Closed/archived calls are excluded at the query level.
_GRANTS_GOV_STATUSES = "posted|forecasted"

# Dev-econ / research-relevant signal words. A Grants.gov hit must match one to
# be surfaced, so unrelated agency calls (embassy events, infrastructure
# assistance) returned by a broad keyword are filtered out.
_GRANTS_GOV_RELEVANT = re.compile(
    r"\beconom|\bdevelopment\b|\bpoverty\b|\bresearch\b|\bsocial\s+science"
    r"|\bbehavioral\b|\blabor\b|\blabour\b|\burban\b|\bfellowship",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Funding sources
# ---------------------------------------------------------------------------

# Each source carries an optional ``known_deadlines`` list of curated,
# human-verified recurring calls. The target funder pages are JavaScript-
# rendered SPAs whose deadline tables never appear in the initial HTML, so
# regex-on-HTML scraping captures zero real dates. The curated registry is the
# primary data source; live scraping is demoted to an override that only
# supplements it when a real date is parsed from the page.
#
# A curated entry has:
#   label:         short call name, appended to the source name
#   month:         typical deadline month (1-12)
#   recurrence:    "annual" or "biannual" (biannual fires twice a year)
#   second_month:  the second month for biannual calls (1-12)
#   last_verified: ISO date the recurrence was last checked by a human
#   date:          an explicit verified ISO deadline (used when today-or-future,
#                  otherwise the recurrence projection is used as a fallback)
#   eligibility:   "phd_student" | "faculty" | "both"
#   india_eligible: whether India-focused work can apply
#   amount:        funded amount (free text)
#   tier:          "core" | "relevant" | "peripheral" (sets the relevance base)
#   scope:         one-line description shown on the deadline card
#
# Verified against each funder's own page on 2026-05-29 (see
# reports/funders_research_2026.json and reports/funding_research_2026.json).
# Clearly-inapplicable funders (India-ineligible, internal-only, impact
# investors, pre-PhD, discontinued) are intentionally excluded to keep the feed
# high-signal; conference CFPs live in the conferences sensor.
FUNDING_SOURCES: dict[str, dict] = {
    # ---- Core: development / urban / India econ, student-applicable ----
    "steg": {
        "name": "STEG Research Grants",
        "org": "CEPR / FCDO",
        "url": "https://steg.cepr.org/funding",
        "scope": "Structural transformation, growth, agriculture and firms in LMICs; dedicated PhD-student and Small Research Grant tracks. India eligible.",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "PhD/Small £15-25k · Larger up to £100k",
        "known_deadlines": [
            {"label": "PhD & Small Research Grants", "month": 1, "recurrence": "biannual", "second_month": 7},
            {"label": "Larger Research Grants", "month": 2, "recurrence": "annual"},
        ],
    },
    "pedl": {
        "name": "PEDL Research Grants",
        "org": "CEPR / FCDO",
        "url": "https://pedl.cepr.org/funding",
        "scope": "Private enterprise and firm development in low-income countries, incl. a dedicated PhD-student Exploratory Grant window. India is lower-priority (must justify).",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "Exploratory £10-40k · Major up to £300k",
        "known_deadlines": [
            {"label": "Exploratory Research Grants (incl. PhD window)", "month": 1, "recurrence": "biannual", "second_month": 7},
        ],
    },
    "igc": {
        "name": "IGC Research Grants",
        "org": "International Growth Centre",
        "url": "https://www.theigc.org/funding/call-for-proposals",
        "scope": "Growth, firms, cities, state and energy in LMICs; PhD students may lead small grants. India is a long-standing IGC country. New FCDO phase from Sep 2026.",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "Small up to £30k · Full up to £125k",
        "known_deadlines": [
            {"label": "Call for proposals", "month": 9, "recurrence": "annual"},
        ],
    },
    "weiss_travel": {
        "name": "Weiss Fund Travel & Piloting Grants",
        "org": "Weiss Fund (UChicago)",
        "url": "https://weissfund.uchicago.edu/applying-for-funding/",
        "scope": "Development-economics fieldwork, piloting and RCT travel; explicitly funds PhD students. Any country with GDP/capita < $13,750 (incl. India).",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "up to $15k",
        "known_deadlines": [
            {"label": "Rolling cycle", "date": "2026-08-01"},
            {"label": "Rolling cycle", "date": "2026-11-01", "month": 11, "recurrence": "annual"},
        ],
    },
    "weiss_research": {
        "name": "Weiss Fund Research & Implementation Grants",
        "org": "Weiss Fund (UChicago)",
        "url": "https://weissfund.uchicago.edu/applying-for-funding/",
        "scope": "Larger development-economics research and implementation grants, reviewed biannually (Spring/Fall). India eligible; PhD students eligible.",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "up to $50k (PhD)",
        "known_deadlines": [
            {"label": "Full research cycle", "month": 8, "recurrence": "biannual", "second_month": 2},
        ],
    },
    "fulbright_india": {
        "name": "Fulbright US Student Study/Research Award - India",
        "org": "US DoS / IIE / USIEF",
        "url": "https://us.fulbrightonline.org/fulbright-us-student-program",
        "scope": "9-month funded research in India (the Fulbright-Nehru pathway for US-based students). Directly funds India dissertation fieldwork. US citizens only.",
        "tier": "core",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "~$1,500/mo + travel + research allowance (9 mo)",
        "known_deadlines": [
            {"label": "2027-28 national competition", "date": "2026-10-06", "month": 10, "recurrence": "annual"},
        ],
    },
    "aiis_jrf": {
        "name": "AIIS Junior Research Fellowship",
        "org": "American Institute of Indian Studies",
        "url": "https://www.indiastudies.org/research-fellowship-programs/",
        "scope": "Up to 11 months of dissertation research in India for US-university doctoral candidates; non-US citizens at US institutions are eligible.",
        "tier": "core",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "$7,000",
        "known_deadlines": [
            {"label": "Annual competition", "month": 12, "recurrence": "annual"},
        ],
    },
    "jpal_joi": {
        "name": "J-PAL Jobs and Opportunity Initiative (JOI)",
        "org": "J-PAL (MIT)",
        "url": "https://www.povertyactionlab.org/initiative/jobs-and-opportunity-initiative-rfp",
        "scope": "Labor-market RCTs in low- and middle-income countries, incl. India. PhD-student window; usually needs a J-PAL affiliate co-PI.",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "Full up to $350k · PhD up to $50k",
        "known_deadlines": [
            {"label": "Annual RFP (LOI ~Mar, full ~Apr)", "month": 4, "recurrence": "annual"},
        ],
    },
    "jpal_atai": {
        "name": "Agricultural Technology Adoption Initiative (ATAI)",
        "org": "J-PAL & CEGA",
        "url": "https://www.povertyactionlab.org/initiative/atai-request-proposals",
        "scope": "RCTs on agricultural technology adoption in South Asia and Sub-Saharan Africa, incl. India - directly relevant to agricultural misallocation. Reopens irregularly; no open RFP right now.",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "up to $500k",
        "known_deadlines": [],
    },
    "igidr_fellowships": {
        "name": "IGIDR Visiting Doctoral & Post-Doctoral Fellowships",
        "org": "Indira Gandhi Institute of Development Research, Mumbai",
        "url": "http://www.igidr.ac.in/academic-outreach/post-doctoral-fellowship-programme/",
        "scope": "India-based development / energy-environment economics research residencies; visiting-doctoral open to Asian (incl. Indian) PhD students. Apply anytime.",
        "tier": "core",
        "eligibility": "both",
        "india_eligible": True,
        "amount": "Doctoral ~Rs.26k/mo · Post-doc ~Rs.70k/mo",
        "known_deadlines": [],
    },
    "cega_challenge": {
        "name": "CEGA Development Economics Challenge",
        "org": "CEGA, UC Berkeley",
        "url": "https://cega.berkeley.edu/collection/cega-graduate-student-research/",
        "scope": "Biannual seed and travel grants for PhD-student development-economics fieldwork in LMICs, incl. India.",
        "tier": "core",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "Travel $5k · Seed $20k",
        "known_deadlines": [
            {"label": "Fall / Spring Challenge", "month": 11, "recurrence": "biannual", "second_month": 3},
        ],
    },
    # ---- Relevant: general econ / social-science student funding ----
    "nsf_grfp": {
        "name": "NSF Graduate Research Fellowship (GRFP)",
        "org": "National Science Foundation",
        "url": "https://www.nsf.gov/funding/opportunities/grfp-nsf-graduate-research-fellowship-program",
        "scope": "Three-year fellowship for early-stage PhD students; SBE covers economics. US citizens/nationals/PR, <=1 year of grad study.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "$37,000/yr stipend + tuition, 3 of 5 yrs",
        "known_deadlines": [
            {"label": "SBE (economics) deadline", "month": 11, "recurrence": "annual"},
        ],
    },
    "nsf_ddrig": {
        "name": "NSF Economics DDRIG",
        "org": "NSF, Economics Program",
        "url": "https://www.nsf.gov/funding/opportunities/economics",
        "scope": "Dissertation Research Improvement Grant: field data collection and research costs for econ PhDs (no stipend); submitted by the faculty advisor. Target dates suspended (PD 23-1320); contact the program officer.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "up to ~$20k",
        "known_deadlines": [],
    },
    "nsf_econ": {
        "name": "NSF Economics Program (regular grants)",
        "org": "NSF SBE/SES",
        "url": "https://www.nsf.gov/funding/opportunities/economics",
        "scope": "Core US federal funding for economics research, incl. development and urban. PI-driven. Target dates currently suspended - awaiting republication.",
        "tier": "relevant",
        "eligibility": "faculty",
        "india_eligible": True,
        "amount": "~$100k-500k",
        "known_deadlines": [],
    },
    "ssrc_dpd": {
        "name": "SSRC Dissertation Proposal Development (DPD)",
        "org": "Social Science Research Council",
        "url": "https://www.ssrc.org/programs/dissertation-proposal-development-dpd-program/",
        "scope": "Early-PhD proposal development plus summer pre-dissertation research across the social sciences. Cohort-based.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "up to ~$5k",
        "known_deadlines": [],
    },
    "nber_gender": {
        "name": "NBER Dissertation Fellowship - Gender in the Economy",
        "org": "National Bureau of Economic Research",
        "url": "https://www.nber.org/calls-papers-and-proposals/dissertation-fellow-gender-economy",
        "scope": "Dissertation funding for econ PhDs working on gender and the economy (Gates-funded). India-relevant topics qualify.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "$42.5k stipend + $3k research + $13k tuition",
        "known_deadlines": [
            {"label": "Annual competition", "month": 1, "recurrence": "annual"},
        ],
    },
    "nber_predoc": {
        "name": "NBER Pre-Doctoral & Dissertation Fellowships",
        "org": "National Bureau of Economic Research",
        "url": "https://www.nber.org/career-resources/calls-fellowship-applications",
        "scope": "Topical pre-doctoral and dissertation fellowships (innovation, energy, aging) for economics PhDs; calls post in fall and close around December.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "~$34-42k stipend + tuition",
        "known_deadlines": [
            {"label": "Fall fellowship calls", "month": 12, "recurrence": "annual"},
        ],
    },
    "fulbright_hays": {
        "name": "Fulbright-Hays DDRA",
        "org": "US Department of Education",
        "url": "https://www.ed.gov/grants-and-programs/grants-higher-education/international-and-foreign-language-education/fulbright-hays-doctoral-dissertation-research-abroad",
        "scope": "6-12 months of dissertation research abroad (India qualifies) in area studies; supports fieldwork + language. US citizens; apply via your campus office.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "travel + maintenance stipend",
        "known_deadlines": [
            {"label": "Annual cycle (via campus)", "month": 1, "recurrence": "annual"},
        ],
    },
    "flas": {
        "name": "FLAS Fellowship (Hindi / South Asian language)",
        "org": "US Dept of Education (via universities)",
        "url": "https://www.ed.gov/grants-and-programs/grants-higher-education/international-and-foreign-language-education/foreign-language-and-area-studies-program",
        "scope": "Funds Hindi / South Asian language study (summer + academic year) to support India fieldwork. US citizens/PR; applied through your home institution's area-studies center.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "summer up to $5k fees + $3.5k stipend",
        "known_deadlines": [
            {"label": "Campus deadlines", "month": 2, "recurrence": "annual"},
        ],
    },
    "russell_sage": {
        "name": "Russell Sage Foundation Research Grants",
        "org": "Russell Sage Foundation",
        "url": "https://www.russellsage.org/apply/application-deadlines",
        "scope": "US social and economic inequality, behavioral economics, and future-of-work research. US study population (weak fit for India-based work).",
        "tier": "relevant",
        "eligibility": "faculty",
        "india_eligible": False,
        "amount": "Presidential < $50k · regular larger",
        "known_deadlines": [
            {"label": "Letters of inquiry", "date": "2026-07-15"},
            {"label": "Letters of inquiry", "date": "2026-10-28", "month": 10, "recurrence": "biannual", "second_month": 3},
        ],
    },
    "sloan_econ": {
        "name": "Sloan Economics Research",
        "org": "Alfred P. Sloan Foundation",
        "url": "https://sloan.org/programs/research/economics",
        "scope": "Economics of science, technology, digitization and AI. Geography-agnostic (not development-specific). Brief LOI by email anytime.",
        "tier": "peripheral",
        "eligibility": "faculty",
        "india_eligible": True,
        "amount": "often $100k+",
        "known_deadlines": [],
    },
    "gcloud_credits": {
        "name": "Google Cloud Research Credits",
        "org": "Google Cloud",
        "url": "https://edu.google.com/programs/credits/research/",
        "scope": "Compute credits for graduate research (large admin/satellite datasets, ML on India data). Apply once a year; rolling review.",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "up to $1,000/yr (PhD students)",
        "known_deadlines": [],
    },
    "aws_credits": {
        "name": "AWS Cloud Credit for Research",
        "org": "Amazon Web Services",
        "url": "https://aws.amazon.com/government-education/research-and-technical-computing/cloud-credit-for-research/",
        "scope": "Compute credits for research workloads; useful for large-scale India data processing. Rolling review (~90-120 days).",
        "tier": "relevant",
        "eligibility": "phd_student",
        "india_eligible": True,
        "amount": "student awards up to $5,000",
        "known_deadlines": [],
    },
}

# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

# Base score by curated tier. "core" = dev/urban/India-econ-specific funders;
# "relevant" = general econ/social-science a development economist uses;
# "peripheral" = adjacent or eligibility-restricted. Grants.gov hits carry no
# tier and start from a neutral base, so their topical fit decides their score.
_TIER_BASE: dict[str, float] = {"core": 0.88, "relevant": 0.62, "peripheral": 0.42}
_UNTIERED_BASE = 0.50

# A Grants.gov hit must clear this score to be ingested, so off-profile federal
# calls (instrumentation, biomedical, fishing) never reach the feed.
_GRANTS_GOV_MIN_SCORE = 0.50

# Topical signals that a call fits the applied-micro / development / urban /
# India profile (substring-matched against name + scope + org, lowercased).
_PROFILE_TERMS: tuple[str, ...] = (
    "develop", "developing countr", "low-income", "low income", "lmic",
    "poverty", "global south", "urban", "cities", "slum", "india",
    "south asia", "agricultur", "smallholder", "rural", "land use", "tenure",
    "labor market", "labour market", "employment", "jobs", "informal",
    "microfinance", "micro-enterprise", "enterprise", "firms", "randomi",
    "impact evaluation", "field experiment", "structural transformation",
    "economic growth", "human capital", "migration", "housing", "governance",
    "public economic", "political economy", "social protection",
    "cash transfer", "financial inclusion", "gender", "sanitation",
    "energy access", "fieldwork",
)

# Strong off-profile signals (hard science, biomedical, infrastructure,
# US-domestic-only) that should sink an untiered Grants.gov hit out of the feed.
_OFFTOPIC_TERMS: tuple[str, ...] = (
    "instrumentation", "cyberinfrastructure", "infrastructure improvement",
    "equipment", "telescope", "spectromet", "materials science", "chemistry",
    "physics", "astronom", "quantum", "semiconductor", "genom", "biomedical",
    "clinical", "cancer", "nursing", "occupational safety", "fishing",
    "fishery", "marine", "vaccine", "molecular", "workforce development",
    "manufacturing", "mentored", "career development award", "k-12",
    "stem education", "defense", "aerospace", "seismic",
)


def score_funding(
    name: str,
    org: str,
    scope: str = "",
    *,
    tier: str | None = None,
    eligibility: str | None = None,
    india_eligible: bool | None = None,
) -> float:
    """Score a funding record's relevance to the research profile.

    Combines a curated-tier base with topical-fit evidence from the call text,
    so an off-profile Grants.gov hit (e.g. "Major Research Instrumentation")
    sinks below the feed threshold while a development-economics call rises.

    Args:
        name: Call/program name.
        org: Funding organization.
        scope: Scope/description text.
        tier: Curated tier ("core"/"relevant"/"peripheral") or None (untiered).
        eligibility: "phd_student" | "faculty" | "both" | None.
        india_eligible: Whether India-focused work can apply (None = unknown).

    Returns:
        Float relevance in [0, 1].
    """
    text = f"{name} {scope} {org}".lower()
    score = _TIER_BASE.get(tier or "", _UNTIERED_BASE)

    pos = sum(1 for term in _PROFILE_TERMS if term in text)
    neg = sum(1 for term in _OFFTOPIC_TERMS if term in text)

    # Topical fit nudges an untiered/peripheral record up; one off-profile hit
    # is enough to drop a Grants.gov result below the gate.
    score += min(0.12, 0.03 * pos)
    if neg:
        score -= 0.35 * min(neg, 2)

    # The user is a PhD student doing India fieldwork: prefer student- and
    # India-eligible calls; penalize India-ineligible ones.
    if eligibility in ("phd_student", "both"):
        score += 0.03
    if india_eligible is False:
        score -= 0.12

    return max(0.0, min(1.0, round(score, 3)))

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

# Month name variants: "January" / "Jan" / "01"
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
)
_MONTH_ABBR = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
)

# Patterns ordered by specificity (most specific first)
_DATE_PATTERNS: list[re.Pattern[str]] = [
    # "January 15, 2025" or "January 15 2025"
    re.compile(
        rf"({_MONTH_NAMES})\s+(\d{{1,2}})[,\s]+(\d{{4}})",
        re.IGNORECASE,
    ),
    # "Jan 15, 2025" or "Jan. 15, 2025"
    re.compile(
        rf"({_MONTH_ABBR})\.?\s+(\d{{1,2}})[,\s]+(\d{{4}})",
        re.IGNORECASE,
    ),
    # "15 January 2025"
    re.compile(
        rf"(\d{{1,2}})\s+({_MONTH_NAMES})\s+(\d{{4}})",
        re.IGNORECASE,
    ),
    # "15 Jan 2025"
    re.compile(
        rf"(\d{{1,2}})\s+({_MONTH_ABBR})\.?\s+(\d{{4}})",
        re.IGNORECASE,
    ),
    # ISO: "2025-01-15"
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    # US format: "01/15/2025"
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
]

# Context words that signal a nearby date is a deadline
_DEADLINE_CONTEXT: re.Pattern[str] = re.compile(
    r"deadline|due\s+date|submit\s+by|submission\s+deadline|apply\s+by|"
    r"applications?\s+due|proposals?\s+due|closes?|closing\s+date|"
    r"letter\s+of\s+intent|loi\s+due|full\s+proposal",
    re.IGNORECASE,
)

_MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


class _ParsedDate(NamedTuple):
    iso: str       # "YYYY-MM-DD"
    raw: str       # matched text for debugging


def _try_parse_date(text: str) -> _ParsedDate | None:
    """Attempt to parse a date string into ISO format.

    Tries multiple pattern formats in order of specificity. Returns the
    first valid parse, or None if nothing matched.

    Args:
        text: Raw string fragment to parse.

    Returns:
        _ParsedDate with iso and raw fields, or None.
    """
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(0)
        groups = m.groups()

        try:
            if len(groups) == 3:
                g0, g1, g2 = groups
                # ISO: YYYY-MM-DD
                if re.match(r"^\d{4}$", g0):
                    year, month_str, day = int(g0), g1, int(g2)
                    month = int(month_str) if month_str.isdigit() else _MONTH_MAP.get(month_str.lower(), 0)
                # Day-first: DD Month YYYY
                elif re.match(r"^\d{1,2}$", g0) and not g0.isdigit() or (g0.isdigit() and int(g0) <= 31):
                    # Check if g1 is a month name
                    month = _MONTH_MAP.get(g1.lower().rstrip("."), 0)
                    if month:
                        day, year = int(g0), int(g2)
                    else:
                        # US format MM/DD/YYYY
                        month, day, year = int(g0), int(g1), int(g2)
                else:
                    # Month name first: "January 15 2025"
                    month = _MONTH_MAP.get(g0.lower().rstrip("."), 0)
                    if not month:
                        continue
                    day, year = int(g1), int(g2)

                if not (1 <= month <= 12 and 1 <= day <= 31 and 2020 <= year <= 2035):
                    continue

                dt = datetime(year, month, day)
                return _ParsedDate(iso=dt.strftime("%Y-%m-%d"), raw=raw)
        except (ValueError, AttributeError):
            continue

    return None


def _next_occurrence(month: int, today: date) -> str:
    """Return the next 15th-of-month ISO date on or after today for a month.

    Uses the 15th as a generic mid-month placeholder. If the 15th of the given
    month has already passed this year, roll to next year.

    Args:
        month: Target month 1-12.
        today: Reference date (the current date).

    Returns:
        ISO date string "YYYY-MM-15" for the next occurrence.
    """
    candidate = date(today.year, month, 15)
    if candidate < today:
        candidate = date(today.year + 1, month, 15)
    return candidate.isoformat()


def curated_deadline_dates(entry: dict, today: date) -> list[str]:
    """Project a curated recurrence entry into upcoming ISO deadline dates.

    Args:
        entry: A ``known_deadlines`` dict with month, recurrence, and
            optionally second_month.
        today: Reference date (the current date).

    Returns:
        Sorted list of unique upcoming ISO date strings (one for an annual
        call, up to two for a biannual call).
    """
    # An explicit verified date wins when it is today-or-future; once it passes,
    # fall through to the recurrence projection (if the entry carries one).
    explicit = entry.get("date")
    if explicit:
        try:
            if date.fromisoformat(explicit) >= today:
                return [explicit]
        except ValueError:
            pass

    month = entry.get("month")
    if not month:
        return []
    months = [month]
    if entry.get("recurrence") == "biannual" and entry.get("second_month"):
        months.append(entry["second_month"])
    dates = {_next_occurrence(m, today) for m in months}
    return sorted(dates)


def _parse_grants_gov_date(raw: str | None) -> str | None:
    """Convert a Grants.gov MM/DD/YYYY date string to ISO YYYY-MM-DD.

    Args:
        raw: A ``closeDate`` value such as "09/29/2026". Empty / None for
            rolling or standing programs.

    Returns:
        ISO date string, or None when the value is absent or unparseable.
    """
    if not raw or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_grants_gov_hits(payload: dict, label: str, org: str) -> list[dict]:
    """Convert a Grants.gov search2 response into dated deadline records.

    Only hits with a parseable ``closeDate`` and a research-relevant title are
    kept; date-less programs and unrelated agency calls are dropped. Each kept
    hit becomes a deadline dict ready for ``upsert_deadline``.

    Args:
        payload: The decoded search2 JSON (the full ``{"data": {...}}`` body).
        label: Short provenance tag appended to each opportunity name.
        org: Organization string used for relevance scoring.

    Returns:
        List of deadline dicts (may be empty).
    """
    hits = (payload.get("data") or {}).get("oppHits") or []

    records: list[dict] = []
    for hit in hits:
        title = (hit.get("title") or "").strip()
        if not title:
            continue

        iso = _parse_grants_gov_date(hit.get("closeDate"))
        if iso is None:
            # Drop rolling / standing programs: no parseable deadline date.
            continue

        # Drop opportunities whose title has no research-relevant signal word.
        if not _GRANTS_GOV_RELEVANT.search(title):
            continue

        opp_id = str(hit.get("id") or "").strip()
        url = (
            f"https://www.grants.gov/search-results-detail/{opp_id}"
            if opp_id
            else _GRANTS_GOV_URL
        )
        agency = (hit.get("agency") or org).strip()

        # Score by topical fit (untiered): off-profile federal calls
        # (instrumentation, biomedical, fishing, ...) sink below the gate.
        score = score_funding(title, agency, "")
        if score < _GRANTS_GOV_MIN_SCORE:
            continue

        records.append(
            {
                "type": "funding",
                "name": f"{title} (Grants.gov: {label})",
                "organization": agency,
                "deadline_date": iso,
                "event_date": None,
                "url": url,
                "description": (
                    f"{agency} funding opportunity "
                    f"{hit.get('number') or ''}".strip()
                ),
                "relevance_score": score,
                "amount": None,
                "eligibility": None,
            }
        )

    return records


def _extract_deadline_dates(html: str) -> list[_ParsedDate]:
    """Find dates that appear near deadline-signalling context words.

    Splits the HTML into a sliding window of text chunks anchored on
    deadline keywords. Extracts and deduplicates all candidate dates.

    Args:
        html: Raw HTML content as a string.

    Returns:
        List of unique _ParsedDate objects, sorted chronologically.
    """
    # Strip most tags for cleaner text matching, preserving whitespace structure
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)

    found: dict[str, _ParsedDate] = {}  # iso -> _ParsedDate for dedup

    # Find all positions of deadline context keywords
    for context_match in _DEADLINE_CONTEXT.finditer(text):
        # Examine ±200 characters around the keyword
        start = max(0, context_match.start() - 50)
        end = min(len(text), context_match.end() + 200)
        window = text[start:end]

        parsed = _try_parse_date(window)
        if parsed and parsed.iso not in found:
            found[parsed.iso] = parsed

    return sorted(found.values(), key=lambda d: d.iso)


def _strip_html(html: str, max_chars: int = 300) -> str:
    """Remove HTML tags and return a plain-text snippet.

    Args:
        html: Raw HTML string.
        max_chars: Maximum length of the returned string.

    Returns:
        Cleaned text, truncated to max_chars.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _extract_description(html: str, program_name: str) -> str:
    """Extract a brief description from the page.

    Looks for paragraphs near the program name or near deadline keywords.
    Falls back to the first substantive paragraph on the page.

    Args:
        html: Raw HTML content.
        program_name: Name of the funding program (used to anchor the search).

    Returns:
        A plain-text description up to 300 characters.
    """
    # Try to find a <p> or <div> near a deadline keyword
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    for para in paragraphs:
        stripped = _strip_html(para, max_chars=300)
        if len(stripped) > 60 and (
            _DEADLINE_CONTEXT.search(stripped)
            or any(kw in stripped.lower() for kw in ("grant", "funding", "research", "award", "apply"))
        ):
            return stripped

    # Fall back to first non-trivial paragraph
    for para in paragraphs:
        stripped = _strip_html(para, max_chars=300)
        if len(stripped) > 60:
            return stripped

    return f"{program_name} funding opportunity."


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class FundingSensor(BaseSensor):
    """Scrape economics research funding pages and insert deadline records.

    Fetches each page in FUNDING_SOURCES, applies regex heuristics to
    locate deadline dates and descriptions, and upserts a deadline record
    for each date found. Pages with no extractable date produce a single
    record with deadline_date=None (rolling deadline).

    Attributes:
        name: Sensor identifier used in DB logging.
        watch: Watch category ("deadlines").
        rate_limit: 0.3 requests per second; funding pages are slow CDNs.
    """

    name = "funding"
    watch = "deadlines"
    rate_limit = 0.3

    def _relevance_score(self, source: dict) -> float:
        """Score a curated source by tier + topical fit + eligibility.

        Delegates to the module-level ``score_funding``, passing the source's
        curated tier, eligibility, and India-eligibility so dev/urban/India
        funders rank above adjacent or US-only ones.

        Args:
            source: A FUNDING_SOURCES entry dict.

        Returns:
            Float relevance score in [0, 1].
        """
        return score_funding(
            source.get("name", ""),
            source.get("org", ""),
            source.get("scope", ""),
            tier=source.get("tier"),
            eligibility=source.get("eligibility"),
            india_eligible=source.get("india_eligible"),
        )

    def _curated_records(
        self, key: str, source: dict, description: str, rolling_fallback: bool = True
    ) -> list[dict]:
        """Build deadline records from a source's curated known_deadlines.

        Projects each curated recurrence into upcoming dated records. When a
        source has no curated calls and rolling_fallback is True, returns a
        single rolling record (deadline_date='') so a confirmed-open call still
        surfaces. On a fetch/decode failure the caller passes rolling_fallback=
        False: we emit only real curated data, never fabricate a rolling
        deadline for a page we could not even load.

        Args:
            key: Source key, used only for logging.
            source: Source dict with name, url, org, known_deadlines.
            description: Description text to attach to each record.
            rolling_fallback: Emit a dateless rolling record when there are no
                curated calls (default True; pass False on fetch failure).

        Returns:
            List of deadline dicts ready for upsert_deadline().
        """
        name = source["name"]
        url = source["url"]
        org = source["org"]
        score = self._relevance_score(source)
        scope = source.get("scope") or description
        amount = source.get("amount") or None
        eligibility = source.get("eligibility") or None
        today = datetime.now(timezone.utc).date()

        records: list[dict] = []
        for entry in source.get("known_deadlines", []):
            for iso in curated_deadline_dates(entry, today):
                records.append(
                    {
                        "type": "funding",
                        "name": f"{name} ({entry['label']})",
                        "organization": org,
                        "deadline_date": iso,
                        "event_date": None,
                        "url": url,
                        "description": scope,
                        "relevance_score": score,
                        "amount": amount,
                        "eligibility": eligibility,
                    }
                )

        # No curated calls: surface as a single rolling (dateless) opportunity,
        # unless the caller suppressed it (fetch failed — nothing real to assert)
        if not records and rolling_fallback:
            records.append(
                {
                    "type": "funding",
                    "name": name,
                    "organization": org,
                    "deadline_date": None,
                    "event_date": None,
                    "url": url,
                    "description": scope,
                    "relevance_score": score,
                    "amount": amount,
                    "eligibility": eligibility,
                }
            )
        return records

    def _scrape_grants_gov(self) -> list[dict]:
        """Query the Grants.gov search2 endpoint for dated funding deadlines.

        Runs each targeted query in ``_GRANTS_GOV_QUERIES`` via a JSON POST,
        parses the dated hits, and de-duplicates within the run by opportunity
        name. A failed query is logged and skipped; it never aborts the run.

        Returns:
            List of deadline dicts with parseable deadline dates.
        """
        from urllib.request import Request, urlopen
        from econsignals.sensors._base import _SSL_CTX

        records: list[dict] = []
        seen: set[str] = set()

        for query in _GRANTS_GOV_QUERIES:
            body = dict(query["body"])
            body.setdefault("oppStatuses", _GRANTS_GOV_STATUSES)
            data = json.dumps(body).encode("utf-8")

            try:
                self.limiter.wait()
                req = Request(
                    _GRANTS_GOV_URL,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "EconSignals/1.0",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=45, context=_SSL_CTX) as resp:
                    payload = json.loads(resp.read())
            except Exception as exc:
                self.stats["errors"] = int(self.stats["errors"]) + 1
                self.stats.setdefault("failed_sources", [])
                self.stats["failed_sources"].append(f"grants_gov:{query['label']}")
                print(
                    f"[funding] grants.gov query {query['label']!r} failed: {exc}",
                    file=sys.stderr,
                )
                continue

            parsed = parse_grants_gov_hits(payload, query["label"], query["org"])
            for record in parsed:
                if record["name"] in seen:
                    continue
                seen.add(record["name"])
                records.append(record)

            print(
                f"[funding] grants.gov {query['label']}: {len(parsed)} dated record(s)",
                file=sys.stderr,
            )

        return records

    def _scrape_source(self, key: str, source: dict) -> list[dict]:
        """Fetch and parse a single funding source page.

        Live scraping is an override on top of the curated registry. When the
        page yields a real parsed date it is used; otherwise the source's
        curated known_deadlines (or a rolling record) are emitted. A fetch or
        decode failure emits a health-marker record so /status can surface a
        persistently-failing source rather than treating it as "no calls".

        Args:
            key: Source key (e.g. "nsf_ses"), used only for logging.
            source: Dict with name, url, org, known_deadlines keys.

        Returns:
            List of deadline dicts ready for upsert_deadline().
        """
        name = source["name"]
        url = source["url"]
        org = source["org"]
        score = self._relevance_score(source)
        amount = source.get("amount") or None
        eligibility = source.get("eligibility") or None

        try:
            raw = self.fetch_url(url, timeout=45)
        except Exception as exc:
            self.stats["errors"] = int(self.stats["errors"]) + 1
            print(f"[funding] fetch failed for {key} ({url}): {exc}", file=sys.stderr)
            # Emit the curated registry anyway (it does not depend on the page),
            # and mark the source unhealthy so /status can see the failure
            self.stats.setdefault("failed_sources", [])
            self.stats["failed_sources"].append(key)
            return self._curated_records(
                key, source, "FETCH FAILED (using curated deadlines)", rolling_fallback=False
            )

        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            self.stats["errors"] = int(self.stats["errors"]) + 1
            print(f"[funding] decode error for {key}: {exc}", file=sys.stderr)
            self.stats.setdefault("failed_sources", [])
            self.stats["failed_sources"].append(key)
            return self._curated_records(
                key, source, "DECODE FAILED (using curated deadlines)", rolling_fallback=False
            )

        dates = _extract_deadline_dates(html)
        description = source.get("scope") or _extract_description(html, name)

        print(
            f"[funding] {key}: found {len(dates)} deadline date(s)",
            file=sys.stderr,
        )

        if not dates:
            # Scraping yielded nothing (JS-rendered page): fall back to the
            # curated known_deadlines registry as the primary data source
            return self._curated_records(key, source, description)

        records = []
        for parsed_date in dates:
            # Skip dates clearly in the past (more than 30 days ago)
            try:
                dt = datetime.strptime(parsed_date.iso, "%Y-%m-%d")
                today = datetime.now(timezone.utc).date()
                if dt.date() < today:
                    # Only skip if well in the past; borderline dates kept
                    if (today - dt.date()).days > 30:
                        continue
            except ValueError:
                pass

            records.append(
                {
                    "type": "funding",
                    "name": name,
                    "organization": org,
                    "deadline_date": parsed_date.iso,
                    "event_date": None,
                    "url": url,
                    "description": description,
                    "relevance_score": score,
                    "amount": amount,
                    "eligibility": eligibility,
                }
            )

        # If every scraped date was in the past (a stale page still showing last
        # cycle's date), fall back to the curated next-occurrence projection
        # rather than emitting a dateless rolling row.
        if not records:
            return self._curated_records(key, source, description)

        return records

    def collect(self) -> list[dict]:
        """Scrape all configured funding sources and return deadline dicts.

        Iterates over FUNDING_SOURCES. Each source may yield zero or more
        deadline records. Errors on individual sources are logged to stderr
        and skipped (partial success is acceptable).

        Returns:
            List of deadline dicts with keys: type, name, organization,
            deadline_date, event_date, url, description, relevance_score.
        """
        all_deadlines: list[dict] = []

        # Primary structured layer: Grants.gov dated federal opportunities.
        grants = self._scrape_grants_gov()
        print(
            f"[funding] grants.gov: {len(grants)} dated record(s) total",
            file=sys.stderr,
        )
        all_deadlines.extend(grants)

        # Curated registry: field-specific funders not listed on Grants.gov.
        for key, source in FUNDING_SOURCES.items():
            print(f"[funding] scraping {key}: {source['url']}", file=sys.stderr)
            records = self._scrape_source(key, source)
            all_deadlines.extend(records)

        print(
            f"[funding] collected {len(all_deadlines)} deadline record(s) total",
            file=sys.stderr,
        )
        return all_deadlines

    def run(self) -> dict:
        """Execute the sensor: collect deadlines, upsert to DB, log run.

        Overrides BaseSensor.run() because deadlines bypass the paper
        deduplication pipeline and are inserted via upsert_deadline().

        Returns:
            Stats dict with keys: sensor, watch, status, found, new, errors,
            and optionally error_message on failure.
        """
        from econsignals.lib.db import log_sensor_start, log_sensor_end, upsert_deadline

        run_id = log_sensor_start(self.name, self.watch)

        try:
            items = self.collect()
            self.stats["found"] = len(items)

            for item in items:
                try:
                    deadline_id = upsert_deadline(item)
                    if deadline_id > 0:
                        self.stats["new"] = int(self.stats["new"]) + 1
                except Exception as exc:
                    self.stats["errors"] = int(self.stats["errors"]) + 1
                    print(f"[funding] upsert error: {exc}", file=sys.stderr)

            run_status = "partial_success" if int(self.stats["errors"]) > 0 else "success"
            log_sensor_end(
                run_id,
                run_status,
                int(self.stats["found"]),
                int(self.stats["new"]),
            )
        except Exception as exc:
            log_sensor_end(run_id, "error", 0, 0, str(exc))
            self.stats["error_message"] = str(exc)
            print(f"[funding] sensor failed: {exc}", file=sys.stderr)

        result = {
            "sensor": self.name,
            "watch": self.watch,
            "status": (
                "error"
                if "error_message" in self.stats
                else "partial_success" if int(self.stats["errors"]) > 0 else "success"
            ),
            **self.stats,
        }
        print(json.dumps(result))
        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from econsignals.lib.db import init_db

    init_db()
    sensor = FundingSensor()
    sensor.run()
