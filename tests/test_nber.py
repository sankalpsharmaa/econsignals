"""Parse-only unit tests for the NBER working-papers sensor.

All inputs are captured real samples (RSS feed body and landing-page meta
tags from back.nber.org / www.nber.org, 2026-05). No network calls.
"""

from __future__ import annotations

import datetime as dt

from econsignals.sensors.nber import (
    _extract_doi,
    _extract_paper_number,
    _parse_citation_date,
    _parse_feed,
    _split_title_authors,
    _within_lookback,
)

# Captured RSS 2.0 sample from https://back.nber.org/rss/new.xml (trimmed to
# three items; abstracts shortened). Includes the " -- by " author encoding and
# the "#fromrss" link fragment exactly as served.
_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<atom:link href="http://back.nber.org/rss/new.xml" rel="self" type="application/rss+xml" />
<title>National Bureau of Economic Research Working Papers</title>
<description>The Latest NBER Working Papers</description>
<link>http://www.nber.org/new.html</link>
<item>
<title>How Should Central Banks Respond to Commodity Price Shocks? -- by Thomas Drechsel, Michael McLeay, Silvana Tenreyro, Enrico D. Turri</title>
<description>We show that the optimal monetary policy framework depends on commodity exposure.</description>
<link>https://www.nber.org/papers/w35164#fromrss</link>
<guid>https://www.nber.org/papers/w35164#fromrss</guid>
</item>
<item>
<title>California Billionaires: Wealth, Taxes, and Wealth Tax Revenue Estimates -- by Jasper Boll, Emmanuel Saez and Gabriel Zucman</title>
<description>This paper documents the wealth of California's billionaires and the taxes they pay.</description>
<link>https://www.nber.org/papers/w35218#fromrss</link>
<guid>https://www.nber.org/papers/w35218#fromrss</guid>
</item>
<item>
<title>A Working Paper With No Byline Information</title>
<description>An abstract.</description>
<link>https://www.nber.org/papers/w35222#fromrss</link>
<guid>https://www.nber.org/papers/w35222#fromrss</guid>
</item>
</channel>
</rss>"""

# Captured citation meta tags from a www.nber.org/papers/<n> landing page.
_SAMPLE_PAGE = (
    '<meta name="citation_title" content="How Should Central Banks Respond" />\n'
    '<meta name="citation_publication_date" content="2026/05/25" />\n'
    '<meta name="citation_doi" content="10.3386/w35164" />\n'
)


# ---------------------------------------------------------------------------
# Title / author splitting
# ---------------------------------------------------------------------------


def test_split_title_authors_comma_list():
    title, authors = _split_title_authors(
        "Some Title -- by Alice Adams, Bob Brown, Carol Clark"
    )
    assert title == "Some Title"
    assert authors == ["Alice Adams", "Bob Brown", "Carol Clark"]


def test_split_title_authors_oxford_and():
    _, authors = _split_title_authors(
        "T -- by Jasper Boll, Emmanuel Saez and Gabriel Zucman"
    )
    assert authors == ["Jasper Boll", "Emmanuel Saez", "Gabriel Zucman"]


def test_split_title_authors_no_byline():
    title, authors = _split_title_authors("A Paper With No Byline")
    assert title == "A Paper With No Byline"
    assert authors == []


# ---------------------------------------------------------------------------
# Paper-number extraction
# ---------------------------------------------------------------------------


def test_extract_paper_number():
    assert _extract_paper_number("https://www.nber.org/papers/w35164#fromrss") == "w35164"
    assert _extract_paper_number("") is None
    assert _extract_paper_number("https://www.nber.org/about") is None


# ---------------------------------------------------------------------------
# Landing-page meta extraction
# ---------------------------------------------------------------------------


def test_parse_citation_date_full():
    assert _parse_citation_date(_SAMPLE_PAGE) == "2026-05-25"


def test_parse_citation_date_year_month():
    html = '<meta name="citation_publication_date" content="2026/05" />'
    assert _parse_citation_date(html) == "2026-05-01"


def test_parse_citation_date_absent():
    assert _parse_citation_date("<html>no meta here</html>") is None


def test_extract_doi():
    assert _extract_doi(_SAMPLE_PAGE) == "10.3386/w35164"
    assert _extract_doi("<html>no doi</html>") is None


# ---------------------------------------------------------------------------
# Lookback window
# ---------------------------------------------------------------------------


def test_within_lookback_keeps_recent():
    recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    assert _within_lookback(recent, 30) is True


def test_within_lookback_drops_old():
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    assert _within_lookback(old, 30) is False


def test_within_lookback_keeps_undated():
    # An undated paper cannot be proven old, so it is kept.
    assert _within_lookback(None, 30) is True


def test_within_lookback_zero_disables_filter():
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    assert _within_lookback(old, 0) is True


# ---------------------------------------------------------------------------
# Full feed parse
# ---------------------------------------------------------------------------


def test_parse_feed_emits_standard_dicts():
    papers = _parse_feed(_SAMPLE_RSS)
    assert len(papers) == 3

    first = papers[0]
    assert first["title"] == "How Should Central Banks Respond to Commodity Price Shocks?"
    assert first["authors"] == [
        "Thomas Drechsel",
        "Michael McLeay",
        "Silvana Tenreyro",
        "Enrico D. Turri",
    ]
    assert first["abstract"].startswith("We show that")
    assert first["source_id"] == "w35164"
    # The "#fromrss" tracking fragment is stripped from the stored URL.
    assert first["url"] == "https://www.nber.org/papers/w35164"
    assert first["source_url"] == "https://www.nber.org/papers/w35164"
    assert first["paper_type"] == "working_paper"
    # Feed carries no date, DOI, JEL, or keywords; enrichment fills date/DOI.
    assert first["published_at"] is None
    assert first["doi"] is None
    assert first["jel_codes"] == []
    assert first["keywords"] == []
    assert first["raw_metadata"]["nber_number"] == "w35164"


def test_parse_feed_required_keys_present():
    required = {"title", "authors", "source_id"}
    for paper in _parse_feed(_SAMPLE_RSS):
        assert required.issubset(paper), f"missing required keys in {paper}"


def test_parse_feed_handles_no_byline_item():
    papers = _parse_feed(_SAMPLE_RSS)
    no_byline = [p for p in papers if p["source_id"] == "w35222"][0]
    assert no_byline["title"] == "A Working Paper With No Byline Information"
    assert no_byline["authors"] == []


def test_parse_feed_bad_xml_returns_empty():
    assert _parse_feed("<rss><not closed") == []
