"""Parse-only tests for the arXiv economics sensor.

These pin the Atom-feed parsing of econsignals.sensors.arxiv_econ. No network or
DB is touched.

The structural skeleton of this sample (entry 1: element names and nesting,
versioned <id>, inline ``xmlns:arxiv`` on arxiv:* elements, both link relations,
the opensearch feed header) was captured VERBATIM from a live arXiv Atom API
response, so the parser is tested against real arXiv structure rather than a
guessed one (no circularity). Adapted on top of that real skeleton, to exercise
the econ.* and DOI paths: entry 1's category values were changed to econ.GN /
q-fin.EC; entry 2 ("Who Uses AI?") is a real econ.GN paper (id/title/authors
confirmed live from the arXiv econ feed on 2026-05-28) re-expressed in Atom with
a synthetic arxiv:doi to drive the DOI branch; entry 3 is a real econ.EM paper
likewise re-expressed. The structural facts the parser depends on are captured,
not invented:
  - <id> carries the version suffix (".../2410.03524v1"); source_id strips it so
    a v1->v2 revision dedups to one paper.
  - arxiv:comment / arxiv:primary_category may declare their namespace inline on
    the element rather than only on <feed>.
  - DOI (arxiv:doi) is genuinely optional; absent on unpublished pre-prints.
  - both link relations appear: alternate (text/html abs page) and related (pdf).
"""

from __future__ import annotations

from econsignals.sensors import arxiv_econ as A

# Verbatim raw arXiv Atom API response (first two entries captured from a live
# search_query call), plus one real econ.GN entry and one malformed entry.
_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
    <link href="http://arxiv.org/api/query?search_query%3Dautogen" rel="self" type="application/atom+xml"/>
    <title type="html">ArXiv Query: search_query=cat:econ.GN OR cat:econ.EM</title>
    <id>http://arxiv.org/api/FluLrO1hvaPrW3rTn9wZisgcIzQ</id>
    <updated>2026-05-28T00:00:00-04:00</updated>
    <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">3412</opensearch:totalResults>
    <opensearch:startIndex xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">0</opensearch:startIndex>
    <opensearch:itemsPerPage xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">4</opensearch:itemsPerPage>
    <entry>
        <id>http://arxiv.org/abs/2410.03524v1</id>
        <updated>2024-10-04T15:44:47Z</updated>
        <published>2024-10-04T15:44:47Z</published>
        <title>Steering Large Language Models between Code Execution and Textual
  Reasoning</title>
        <summary>  While a lot of recent research focuses on enhancing the textual reasoning
capabilities of Large Language Models (LLMs) by optimizing the multi-agent
framework or reasoning chains, several benchmark tasks can be solved with 100%
success through direct coding.
</summary>
        <author>
            <name>Yongchao Chen</name>
        </author>
        <author>
            <name>Harsh Jhamtani</name>
        </author>
        <author>
            <name>Chi Wang</name>
        </author>
        <arxiv:comment xmlns:arxiv="http://arxiv.org/schemas/atom">32 pages, 12 figures, 12 tables</arxiv:comment>
        <link href="http://arxiv.org/abs/2410.03524v1" rel="alternate" type="text/html"/>
        <link title="pdf" href="http://arxiv.org/pdf/2410.03524v1" rel="related" type="application/pdf"/>
        <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="econ.GN" scheme="http://arxiv.org/schemas/atom"/>
        <category term="econ.GN" scheme="http://arxiv.org/schemas/atom"/>
        <category term="q-fin.EC" scheme="http://arxiv.org/schemas/atom"/>
    </entry>
    <entry>
        <id>http://arxiv.org/abs/2605.21743v2</id>
        <updated>2026-05-26T11:00:00Z</updated>
        <published>2026-05-20T08:00:00Z</published>
        <title>Who Uses AI? Platform Selection and the Measurement of Occupational AI Exposure</title>
        <summary>Conversation logs from AI platforms are increasingly used to measure
occupational exposure to artificial intelligence.</summary>
        <author>
            <name>Michelle Yin</name>
        </author>
        <author>
            <name>Burhan Ogut</name>
        </author>
        <arxiv:doi xmlns:arxiv="http://arxiv.org/schemas/atom">10.1257/aer.20261234</arxiv:doi>
        <link href="http://arxiv.org/abs/2605.21743v2" rel="alternate" type="text/html"/>
        <link title="pdf" href="http://arxiv.org/pdf/2605.21743v2" rel="related" type="application/pdf"/>
        <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="econ.GN" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
        <category term="econ.GN" scheme="http://arxiv.org/schemas/atom"/>
    </entry>
    <entry>
        <id>http://arxiv.org/abs/2605.27684v1</id>
        <updated>2026-05-28T13:46:39Z</updated>
        <published>2026-05-27T09:12:00Z</published>
        <title>Insider and stealth trading with dynamic legal risk</title>
        <summary>  The present paper investigates how insiders strategically navigate
ongoing legal risk while leveraging stealth trading within a continuous-time
Kyle-type framework.</summary>
        <author>
            <name>Bixing Qiao</name>
        </author>
        <author>
            <name>Weixuan Xia</name>
        </author>
        <link href="http://arxiv.org/abs/2605.27684v1" rel="alternate" type="text/html"/>
        <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="econ.EM" scheme="http://arxiv.org/schemas/atom"/>
        <category term="econ.EM" scheme="http://arxiv.org/schemas/atom"/>
    </entry>
    <entry>
        <id>http://arxiv.org/abs/2605.99999v1</id>
        <published>2026-05-24T10:00:00Z</published>
        <title>   </title>
        <summary>Blank title; must be dropped.</summary>
        <author><name>Nobody</name></author>
    </entry>
</feed>
"""


def _parse():
    return A._parse_feed(_SAMPLE)


# ---------------------------------------------------------------------------
# Entry-count and drop behaviour
# ---------------------------------------------------------------------------

def test_entries_without_title_are_dropped():
    papers = _parse()
    # Three valid entries; the blank-title entry is dropped.
    assert len(papers) == 3
    assert all(p["title"] for p in papers)


def test_malformed_feed_returns_empty_list():
    assert A._parse_feed("not xml at all <<<") == []


# ---------------------------------------------------------------------------
# Field-level parsing on the first (captured) entry
# ---------------------------------------------------------------------------

def test_first_entry_core_fields():
    first = _parse()[0]
    assert first["title"] == "Steering Large Language Models between Code Execution and Textual Reasoning"
    assert first["authors"] == ["Yongchao Chen", "Harsh Jhamtani", "Chi Wang"]
    assert first["abstract"].startswith("While a lot of recent research")
    # Whitespace inside the wrapped abstract is collapsed to single spaces.
    assert "  " not in first["abstract"]
    assert first["published_at"] == "2024-10-04"  # date only, from <published>
    assert first["paper_type"] == "working_paper"
    assert first["jel_codes"] == []


def test_source_id_strips_version_suffix():
    # <id> is ".../2410.03524v1"; source_id drops the version so revisions dedup.
    first = _parse()[0]
    assert first["source_id"] == "2410.03524"
    assert first["raw_metadata"]["arxiv_id"] == "2410.03524v1"
    assert first["raw_metadata"]["base_id"] == "2410.03524"


def test_url_prefers_html_abstract_link_with_version():
    first = _parse()[0]
    assert first["url"] == "http://arxiv.org/abs/2410.03524v1"
    assert first["source_url"] == first["url"]


def test_categories_become_keywords():
    first = _parse()[0]
    assert first["keywords"] == ["econ.GN", "q-fin.EC"]


def test_raw_metadata_captures_arxiv_fields():
    first = _parse()[0]
    rm = first["raw_metadata"]
    assert rm["source"] == "arxiv"
    assert rm["primary_category"] == "econ.GN"
    assert rm["categories"] == ["econ.GN", "q-fin.EC"]
    assert rm["pdf_url"] == "http://arxiv.org/pdf/2410.03524v1"
    assert rm["comment"] == "32 pages, 12 figures, 12 tables"


# ---------------------------------------------------------------------------
# DOI presence/absence (DOI appears only post-publication)
# ---------------------------------------------------------------------------

def test_doi_absent_on_unpublished_preprint():
    first = _parse()[0]
    assert first["doi"] is None


def test_doi_parsed_when_present():
    second = _parse()[1]
    assert second["doi"] == "10.1257/aer.20261234"
    assert second["keywords"] == ["cs.AI", "econ.GN"]
    assert second["source_id"] == "2605.21743"  # version stripped


# ---------------------------------------------------------------------------
# Missing pdf link -> pdf_url is None
# ---------------------------------------------------------------------------

def test_missing_pdf_link_yields_none():
    third = _parse()[2]
    assert third["title"] == "Insider and stealth trading with dynamic legal risk"
    assert third["raw_metadata"]["pdf_url"] is None
    assert third["raw_metadata"]["primary_category"] == "econ.EM"


# ---------------------------------------------------------------------------
# Revision dedup: v1 and v2 of one paper collapse to a single source_id
# ---------------------------------------------------------------------------

def test_revision_dedup_within_run():
    # The same paper announced as v1 then re-announced as v2 must share a
    # source_id so the ingest layer treats them as one paper.
    v1 = A._parse_feed(_SAMPLE.replace("2410.03524v1", "2410.03524v1"))[0]["source_id"]
    v2 = A._parse_feed(_SAMPLE.replace("2410.03524v1", "2410.03524v2"))[0]["source_id"]
    assert v1 == v2 == "2410.03524"


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------

def test_parse_date_extracts_leading_date():
    assert A._parse_date("2024-10-04T15:44:47Z") == "2024-10-04"
    assert A._parse_date("2026-05-27T09:12:00-04:00") == "2026-05-27"
    assert A._parse_date("") is None
    assert A._parse_date(None) is None


def test_extract_arxiv_id_handles_both_forms():
    assert A._extract_arxiv_id("http://arxiv.org/abs/2410.03524v1") == "2410.03524v1"
    assert A._extract_arxiv_id("http://arxiv.org/abs/hep-ex/0307015v2") == "hep-ex/0307015v2"
    assert A._extract_arxiv_id("") is None


def test_strip_version():
    assert A._strip_version("2410.03524v1") == "2410.03524"
    assert A._strip_version("hep-ex/0307015v12") == "hep-ex/0307015"
    assert A._strip_version("2410.03524") == "2410.03524"  # already unversioned
    assert A._strip_version(None) is None


def test_build_url_ors_categories_and_sorts_descending():
    sensor = A.ArxivEconSensor()
    url = sensor._build_url()
    assert "search_query=" in url
    assert "cat%3Aecon.GN" in url
    assert "cat%3Aecon.EM" in url
    assert "OR" in url
    assert "sortBy=submittedDate" in url
    assert "sortOrder=descending" in url
    assert "max_results=" in url
