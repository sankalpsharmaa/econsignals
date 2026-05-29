"""Corpus-level acceptance test for the relevance ranking.

The owner abandoned the tool because output quality was unverifiable ("the feed
still feels off"). This turns that judgement into a pass/fail gate: it scores a
labelled corpus and asserts that on-topic India / development / urban / labour /
public papers rank above off-topic noise, and that the specific junk classes the
audit caught (crypto/coffee with an India token, the Norwegian next-of-kin
paper) fall out of the top.

Two layers:
  - test_topical_separation_* : hermetic, deterministic, always runs. Holds
    venue and recency constant so the test isolates the topical-fit channel,
    which is the part that was broken.
  - test_live_corpus_*        : runs only when the real data/econsignals.db is
    present and populated, reading persisted scores to answer "does the live
    feed pass?" without mutating production data. Skipped in CI.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from econsignals.lib import relevance as R

# ---------------------------------------------------------------------------
# Labelled hermetic corpus
# ---------------------------------------------------------------------------

# On-topic: each title hits at least one distinctive core topic (urban / land /
# India-structural / migration / informal / sanitation / caste), so the topical
# channel should saturate regardless of JEL metadata (which the corpus lacks).
_ON_TOPIC: list[str] = [
    "Slum housing and land tenure reform in urban India",
    "Land use regulation and informal settlements in Mumbai",
    "Rural-urban migration and labor markets in India",
    "Property rights and agricultural land tenure in India",
    "Zoning, housing supply, and urbanization in Indian cities",
    "Sanitation infrastructure and public goods in rural India",
    "Caste, land ownership, and tenure security in India",
    "Slum redevelopment and housing markets in Delhi",
    "Migration, the informal sector, and urban labor in South Asia",
    "Land tenure, zoning, and property rights in developing cities",
    "Urbanization and housing affordability in India",
    "The Smart Cities Mission and urban land use in India",
]

# Off-topic: none hit a profile topic. Two carry an "India" token to verify the
# India signal only amplifies papers that already show field relevance (it must
# not float a crypto/coffee paper on geography alone). Three are the exact junk
# classes the audit flagged.
_OFF_TOPIC: list[str] = [
    "Cryptocurrency regulation and blockchain markets in India",
    "Emerging trends in coffee consumption in India",
    "Next-of-kin decisions and inheritance taxation in Norway",
    "Optimal monetary policy and inflation targeting in the Eurozone",
    "Deep learning architectures for image classification",
    "Ventilator settings for fiberoptic bronchoscopy",
    "The information matrix test for Markov switching models",
    "Corporate finance and capital structure of US firms",
    "Quality upgrading in global coffee supply chains in Colombia",
    "Behavioral biases in stock market trading",
    "Tourism demand and hotel pricing in Spain",
    "Minimum wages and the rise of the robots",
]

# The named junk that must not survive into the top ranks.
_NAMED_JUNK = {
    "Cryptocurrency regulation and blockchain markets in India",
    "Emerging trends in coffee consumption in India",
    "Next-of-kin decisions and inheritance taxation in Norway",
}


@pytest.fixture()
def scored_corpus() -> dict[str, float]:
    """Ingest the labelled corpus at equal venue/recency and score every paper.

    All papers share source='openalex' (equal prestige) and a recent date
    (equal recency), so the only thing that can separate them is topical fit.
    Returns a mapping of title -> relevance score.
    """
    from econsignals.lib.db import get_db
    from econsignals.lib.dedup import ingest_paper

    # Reset process-global caches so the test reads the real profile fresh and
    # is not contaminated by a tracked-author flag set in another test's DB.
    R._interest_kw_cache = None
    R._jel_weights_cache = None
    R._has_tracked_cache = None

    recent = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")

    title_to_id: dict[str, int] = {}
    for title in _ON_TOPIC + _OFF_TOPIC:
        paper_id, _ = ingest_paper(
            paper_data={
                "title": title,
                "authors": [],
                "abstract": "",
                "doi": None,
                "url": None,
                "published_at": recent,
                "paper_type": "working_paper",
                "jel_codes": [],
                "keywords": [],
            },
            source="openalex",
            source_id=f"test:{abs(hash(title))}",
        )
        title_to_id[title] = paper_id

    jel_weights = R.load_jel_weights()
    interest_kw = R.load_interest_keywords()

    db = get_db()
    try:
        return {
            title: R.score_paper(
                {"id": pid, "title": title, "abstract": "", "published_at": recent},
                db,
                jel_weights,
                interest_kw,
            )
            for title, pid in title_to_id.items()
        }
    finally:
        db.close()


def test_topical_separation_at_equal_venue(scored_corpus: dict[str, float]) -> None:
    """Every on-topic paper outranks every off-topic paper when venue is held flat."""
    on = [scored_corpus[t] for t in _ON_TOPIC]
    off = [scored_corpus[t] for t in _OFF_TOPIC]
    assert min(on) > max(off), (
        f"topical channel failed to separate: min on-topic={min(on):.3f} "
        f"is not above max off-topic={max(off):.3f}"
    )


def test_named_junk_falls_out_of_top(scored_corpus: dict[str, float]) -> None:
    """Crypto/coffee-with-India and the Norwegian next-of-kin paper leave the top half."""
    ranked = sorted(scored_corpus, key=scored_corpus.get, reverse=True)
    top_half = set(ranked[: len(_ON_TOPIC)])
    leaked = _NAMED_JUNK & top_half
    assert not leaked, f"named junk ranked in the top {len(_ON_TOPIC)}: {leaked}"


def test_india_token_does_not_rescue_off_topic(scored_corpus: dict[str, float]) -> None:
    """An India token must amplify field-relevant work, not geography alone."""
    crypto_india = scored_corpus["Cryptocurrency regulation and blockchain markets in India"]
    land_india = scored_corpus["Property rights and agricultural land tenure in India"]
    assert crypto_india < land_india


# ---------------------------------------------------------------------------
# Live-corpus diagnostic (skipped unless the real DB is present and populated)
# ---------------------------------------------------------------------------

_LIVE_DB = Path(__file__).resolve().parents[1] / "data" / "econsignals.db"
_MIN_SCORED = 50
_TOP_N = 20
_MIN_ON_TOPIC = 0.6  # at least 12 of the top 20 must be on-topic

# Title/abstract markers and JEL prefixes that indicate profile relevance.
# Leading word-boundary only: these are prefixes ("migrat" must match
# "migration", "agricultur" must match "agricultural"), so a trailing \b would
# wrongly reject the inflected forms.
_TOPIC_RE = re.compile(
    r"\b(?:india|south asia|urban|cit(?:y|ies)|housing|land|tenure|"
    r"slum|migrat|labou?r|informal|sanitation|caste|zoning|property right|"
    r"develop|rural|agricultur|spatial|misalloc|pollution|"
    r"public good|infrastructur|inequalit|povert)",
    re.IGNORECASE,
)
_TOPIC_JEL_PREFIXES = ("O", "R", "I", "J", "H", "Q")


def _live_scored_count() -> int:
    if not _LIVE_DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM papers WHERE relevance_score IS NOT NULL"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def _is_on_topic(title: str, abstract: str, jel_raw: str | None) -> bool:
    if _TOPIC_RE.search(f"{title or ''} {abstract or ''}"):
        return True
    try:
        codes = json.loads(jel_raw) if jel_raw else []
    except (json.JSONDecodeError, TypeError):
        codes = []
    return any(str(c).strip().upper().startswith(_TOPIC_JEL_PREFIXES) for c in codes)


@pytest.mark.skipif(
    _live_scored_count() < _MIN_SCORED,
    reason="live data/econsignals.db absent or has too few scored papers",
)
def test_live_corpus_top20_is_on_topic() -> None:
    """Read persisted scores from the live DB; assert the top 20 are mostly on-topic.

    Pure read (mode=ro): never mutates production data. Prints any off-topic
    leakers so a failure names the offending papers.
    """
    con = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT title, abstract, jel_codes, relevance_score FROM papers "
            "WHERE relevance_score IS NOT NULL "
            "ORDER BY relevance_score DESC, published_at DESC LIMIT ?",
            (_TOP_N,),
        ).fetchall()
    finally:
        con.close()

    on_topic = [r for r in rows if _is_on_topic(r["title"], r["abstract"], r["jel_codes"])]
    leakers = [
        f'  {r["relevance_score"]:.3f}  {r["title"][:80]}'
        for r in rows
        if r not in on_topic
    ]
    frac = len(on_topic) / len(rows)
    msg = (
        f"top-{len(rows)} on-topic fraction = {frac:.0%} "
        f"(need >= {_MIN_ON_TOPIC:.0%}).\nOff-topic in top {len(rows)}:\n"
        + "\n".join(leakers)
    )
    assert frac >= _MIN_ON_TOPIC, msg
