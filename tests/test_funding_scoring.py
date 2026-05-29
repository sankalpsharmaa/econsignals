"""Tests for the funding relevance scorer and curated-deadline projection.

The grants section was rebuilt to rank by topical fit (so off-profile federal
calls sink out of the feed) and to project the next occurrence of recurring
calls. These pin both behaviours and the registry's structural invariants.
"""

from __future__ import annotations

from datetime import date

from econsignals.sensors import funding as F


# ---------------------------------------------------------------------------
# Relevance scoring: tier base + topical fit + eligibility
# ---------------------------------------------------------------------------

def test_tier_orders_core_above_relevant_above_peripheral():
    core = F.score_funding("X", "Y", "development economics in India", tier="core")
    relevant = F.score_funding("X", "Y", "economics research", tier="relevant")
    peripheral = F.score_funding("X", "Y", "economics of technology", tier="peripheral")
    assert core > relevant > peripheral


def test_offtopic_grants_gov_hits_sink_below_gate():
    # Untiered Grants.gov hits: hard-science / biomedical / fishing must fall
    # below the ingest gate, while a dev-econ call clears it.
    infra = F.score_funding("Major Research Instrumentation Program", "NSF", "")
    fishing = F.score_funding("Commercial Fishing Occupational Safety Research", "CDC", "")
    dev = F.score_funding("Economic Development Research", "US Federal", "")
    assert infra < F._GRANTS_GOV_MIN_SCORE
    assert fishing < F._GRANTS_GOV_MIN_SCORE
    assert dev >= F._GRANTS_GOV_MIN_SCORE


def test_india_ineligible_is_penalized():
    base = F.score_funding("Inequality study", "RSF", "US inequality", tier="relevant")
    penalized = F.score_funding(
        "Inequality study", "RSF", "US inequality", tier="relevant", india_eligible=False
    )
    assert penalized < base


def test_scores_stay_in_unit_interval():
    s = F.score_funding(
        "STEG development urban India agriculture growth firms",
        "CEPR",
        "development urban India agriculture growth labor migration housing",
        tier="core",
        eligibility="both",
        india_eligible=True,
    )
    assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# Curated deadline projection: explicit verified dates + recurrence
# ---------------------------------------------------------------------------

_TODAY = date(2026, 5, 29)


def test_future_explicit_date_is_used_verbatim():
    assert F.curated_deadline_dates({"date": "2026-08-01"}, _TODAY) == ["2026-08-01"]


def test_past_explicit_date_falls_through_to_recurrence():
    # An explicit date in the past projects the next recurrence instead.
    out = F.curated_deadline_dates(
        {"date": "2026-02-02", "month": 2, "recurrence": "annual"}, _TODAY
    )
    assert out == ["2027-02-15"]


def test_past_explicit_date_without_recurrence_is_dropped():
    assert F.curated_deadline_dates({"date": "2020-01-01"}, _TODAY) == []


def test_biannual_projects_two_dates():
    out = F.curated_deadline_dates(
        {"month": 1, "recurrence": "biannual", "second_month": 7}, _TODAY
    )
    assert out == ["2026-07-15", "2027-01-15"]


# ---------------------------------------------------------------------------
# Registry structural invariants
# ---------------------------------------------------------------------------

def test_registry_entries_are_well_formed():
    valid_tiers = {"core", "relevant", "peripheral"}
    valid_elig = {"phd_student", "faculty", "both"}
    assert len(F.FUNDING_SOURCES) >= 18
    for key, src in F.FUNDING_SOURCES.items():
        assert src["tier"] in valid_tiers, key
        assert src["eligibility"] in valid_elig, key
        assert src["url"].startswith("http"), key
        assert src.get("scope"), key
        assert isinstance(src.get("india_eligible"), bool), key
