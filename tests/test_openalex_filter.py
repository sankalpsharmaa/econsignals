"""Tests for the tightened OpenAlex /works filter construction.

These are parse/construction-only tests: they assert on the filter string the
sensor builds, never hitting the network. They lock in the constraints added
2026-05-28 (work-type gate, venue-container gate, and the opt-in flagship-journal
venue allowlist) so a future edit cannot silently re-widen the feed.

The default gates cut non-paper container types (datasets, proceedings,
ebook-platform items), not regional/predatory journals (those are type=journal
and survive every default gate). Predatory/regional suppression is handled by
the relevance-ranking layer and by the opt-in venue allowlist. The live result
counts were verified with curl over a 7-day lookback before commit (field gate
alone: 2925 works; field + type + source-type gates: 2807), so the gates are
known to preserve a healthy collection volume.
"""

from __future__ import annotations

import pytest

from econsignals.sensors.openalex import (
    _PRIMARY_FIELD_ID,
    _SOURCE_TYPES,
    _TYPES,
    _VENUE_ALLOWLIST,
    _VENUE_ALLOWLIST_ENV,
    OpenAlexSensor,
)

_FROM_DATE = "2026-05-22"


@pytest.fixture
def sensor() -> OpenAlexSensor:
    """Return a sensor instance for filter-construction tests."""
    return OpenAlexSensor()


def test_base_filter_keeps_primary_field_gate(sensor: OpenAlexSensor) -> None:
    """The primary-field gate (Economics) must remain in the base filter."""
    filt = sensor._build_filter(_FROM_DATE)
    assert f"primary_topic.field.id:{_PRIMARY_FIELD_ID}" in filt


def test_base_filter_has_work_type_gate(sensor: OpenAlexSensor) -> None:
    """The work-type gate restricts to articles and preprints/working papers."""
    filt = sensor._build_filter(_FROM_DATE)
    assert f"type:{_TYPES}" in filt
    # Preprints/working papers stay in alongside journal articles.
    assert "article" in _TYPES
    assert "preprint" in _TYPES


def test_base_filter_has_source_type_gate(sensor: OpenAlexSensor) -> None:
    """The venue-container gate restricts to journals and repositories.

    journal|repository keeps genuine journal articles and NBER/IZA-style
    working-paper repositories while dropping dataset, proceedings, and
    ebook-platform containers.
    """
    filt = sensor._build_filter(_FROM_DATE)
    assert f"primary_location.source.type:{_SOURCE_TYPES}" in filt
    assert "journal" in _SOURCE_TYPES
    assert "repository" in _SOURCE_TYPES


def test_base_filter_carries_lookback_date(sensor: OpenAlexSensor) -> None:
    """The from_publication_date lower bound must be present and verbatim."""
    filt = sensor._build_filter(_FROM_DATE)
    assert f"from_publication_date:{_FROM_DATE}" in filt


def test_base_filter_omits_venue_allowlist_by_default(sensor: OpenAlexSensor) -> None:
    """The venue allowlist must NOT appear in the default (base) filter.

    The allowlist is additive/opt-in; if it leaked into the base filter it would
    AND-restrict the whole feed to five journals.
    """
    filt = sensor._build_filter(_FROM_DATE, venue_only=False)
    assert "primary_location.source.id" not in filt


def test_allowlist_filter_adds_venue_or_group(sensor: OpenAlexSensor) -> None:
    """The supplementary pass appends the venue allowlist as an OR-group.

    All constant source IDs must be pipe-joined under a single
    primary_location.source.id key (pipe = OR in OpenAlex filter syntax).
    """
    filt = sensor._build_filter(_FROM_DATE, venue_only=True)
    expected = "primary_location.source.id:" + "|".join(_VENUE_ALLOWLIST)
    assert expected in filt
    # The allowlist pass still carries the base gates (it is additive within
    # the economics feed, not a separate uncurated query).
    assert f"primary_topic.field.id:{_PRIMARY_FIELD_ID}" in filt
    assert f"type:{_TYPES}" in filt
    assert f"primary_location.source.type:{_SOURCE_TYPES}" in filt


def test_venue_allowlist_is_constant_and_nonempty() -> None:
    """The allowlist is a non-empty constant of OpenAlex source IDs (S-prefixed)."""
    assert isinstance(_VENUE_ALLOWLIST, tuple)
    assert len(_VENUE_ALLOWLIST) >= 1
    assert all(sid.startswith("S") for sid in _VENUE_ALLOWLIST)


def test_built_url_encodes_filter_and_mailto(sensor: OpenAlexSensor) -> None:
    """The full URL keeps the polite-pool mailto and the encoded filter key."""
    url = sensor._build_url(_FROM_DATE, page=1)
    assert "mailto=" in url
    assert "filter=" in url
    # urlencode escapes the field gate's slash; confirm it survives encoding.
    assert "fields%2F20" in url


def test_country_scope_opt_in_only(sensor: OpenAlexSensor, monkeypatch) -> None:
    """authorships.countries appears only when the country env var is set."""
    monkeypatch.delenv("ECONSIGNALS_OPENALEX_COUNTRIES", raising=False)
    assert "authorships.countries" not in sensor._build_filter(_FROM_DATE)

    monkeypatch.setenv("ECONSIGNALS_OPENALEX_COUNTRIES", "IN|BD")
    assert "authorships.countries:IN|BD" in sensor._build_filter(_FROM_DATE)


def test_venue_allowlist_env_name_stable() -> None:
    """Lock the opt-in env var name used by collect() to toggle the allowlist."""
    assert _VENUE_ALLOWLIST_ENV == "ECONSIGNALS_OPENALEX_VENUE_ALLOWLIST"
