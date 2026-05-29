"""Parse-only tests for the World Bank PRWP sensor.

Feeds an embedded WDS JSON sample (captured from the live
https://search.worldbank.org/api/v3/wds endpoint, 2026-05-28) to the pure
``_parse_response`` helper -- no network, no DB. The sample exercises every
branch the parser must handle:

  * a normal multi-author document with abstract and DOI,
  * a document whose ``abstracts`` block is absent,
  * a document with no ``display_title`` (title falls back to ``docna``),
  * the non-document ``facets`` entry, which must be skipped.
"""

from __future__ import annotations

import json

from econsignals.sensors.worldbank import _parse_response

# ---------------------------------------------------------------------------
# Embedded sample: real document ids / DOIs from the live WDS API, trimmed to
# the fields the parser reads, plus the synthetic edge cases described above.
# ---------------------------------------------------------------------------

_SAMPLE = json.loads(
    """
{
  "rows": 4,
  "os": 0,
  "page": 1,
  "total": 4,
  "documents": {
    "D40105739": {
      "id": "40105739",
      "display_title": "Who Gains from Cybersecurity Preparedness ? Evidence from Sectoral Growth",
      "docna": {"0": {"docna": "Who Gains from Cybersecurity Preparedness ?"}},
      "authors": {
        "0": {"author": "Vergara Cobos, Estefania Belen"},
        "1": {"author": "Cakir, Selcen"},
        "2": {"author": "Mei Zahav, Hagai"}
      },
      "abstracts": {"cdata!": "Does cybersecurity preparedness matter for growth, and if so, where?"},
      "docdt": "2026-05-28T04:00:00Z",
      "dois": "10.1596/1813-9450-11398",
      "repnb": "WPS11398",
      "url": "http://documents.worldbank.org/curated/en/099826505282615159",
      "pdfurl": "https://documents.worldbank.org/curated/en/099826505282615159/pdf/IDU.pdf",
      "guid": "099826505282615159"
    },
    "D40105435": {
      "id": "40105435",
      "display_title": "Evaluating Alternative Approaches to Small Area Estimation of Poverty with Survey \\nand Census Data",
      "authors": {
        "0": {"author": "Dang, Hai-Anh H."},
        "1": {"author": "Newhouse, David"}
      },
      "abstracts": {"cdata!": "This paper uses five rounds of Mexican and Brazilian census\\nextracts."},
      "docdt": "2026-05-27T04:00:00Z",
      "dois": "10.1596/1813-9450-11396",
      "repnb": "WPS11396",
      "url": "http://documents.worldbank.org/curated/en/099304305272691631"
    },
    "D40000001": {
      "id": "40000001",
      "docna": {"0": {"docna": "Title Only In Docna, No Display Title"}},
      "authors": {"0": {"author": "Solo, Author"}},
      "docdt": "2026-05-25T04:00:00Z",
      "url": "http://documents.worldbank.org/curated/en/000000000000000001"
    },
    "facets": {
      "count": [],
      "docty": []
    }
  }
}
"""
)


def test_facets_entry_is_skipped():
    papers = _parse_response(_SAMPLE)
    # Three real documents; the "facets" entry has no id and must be dropped.
    assert len(papers) == 3
    assert all(p["source_id"] != "facets" for p in papers)


def test_source_id_is_clean_numeric_id():
    papers = _parse_response(_SAMPLE)
    by_doi = {p["doi"]: p for p in papers if p["doi"]}
    # source_id is the numeric "id", not the "D"-prefixed dict key.
    assert by_doi["10.1596/1813-9450-11398"]["source_id"] == "40105739"


def test_doi_populated_from_dois_field():
    papers = _parse_response(_SAMPLE)
    dois = {p["doi"] for p in papers}
    # DOI carries through so the paper can dedup against OpenAlex/Crossref.
    assert "10.1596/1813-9450-11398" in dois
    assert "10.1596/1813-9450-11396" in dois


def test_authors_ordered_by_integer_key():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40105739")
    assert paper["authors"] == [
        "Vergara Cobos, Estefania Belen",
        "Cakir, Selcen",
        "Mei Zahav, Hagai",
    ]


def test_abstract_read_from_cdata_key():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40105739")
    assert paper["abstract"].startswith("Does cybersecurity preparedness")


def test_embedded_newlines_collapsed_in_title_and_abstract():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40105435")
    assert "\n" not in paper["title"]
    assert "Survey and Census" in paper["title"]
    assert "\n" not in paper["abstract"]
    assert "census extracts" in paper["abstract"]


def test_missing_abstract_defaults_to_none():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40000001")
    assert paper["abstract"] is None


def test_missing_doi_defaults_to_none():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40000001")
    assert paper["doi"] is None


def test_title_falls_back_to_docna_when_display_title_absent():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40000001")
    assert paper["title"] == "Title Only In Docna, No Display Title"


def test_published_at_is_sliced_to_date():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40105739")
    assert paper["published_at"] == "2026-05-28"


def test_paper_type_is_working_paper():
    papers = _parse_response(_SAMPLE)
    assert all(p["paper_type"] == "working_paper" for p in papers)


def test_raw_metadata_carries_report_number_and_pdf():
    papers = _parse_response(_SAMPLE)
    paper = next(p for p in papers if p["source_id"] == "40105739")
    assert paper["raw_metadata"]["repnb"] == "WPS11398"
    assert paper["raw_metadata"]["pdfurl"].endswith(".pdf")
    assert paper["raw_metadata"]["source"] == "worldbank_wds"


def test_malformed_payload_returns_empty_list():
    assert _parse_response({}) == []
    assert _parse_response({"documents": None}) == []
