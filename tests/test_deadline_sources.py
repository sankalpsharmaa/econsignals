"""Parse-only tests for the structured deadline sources.

Covers the Grants.gov search2 parser added to the funding sensor. The sample
below is a trimmed capture of a real ``https://api.grants.gov/v1/api/search2``
response (verified 2026-05-28), so the test pins parser behaviour without any
network call.
"""

from __future__ import annotations

import json

from econsignals.sensors.funding import (
    _parse_grants_gov_date,
    parse_grants_gov_hits,
)

# ---------------------------------------------------------------------------
# Captured Grants.gov search2 sample (trimmed to the oppHits we exercise)
# ---------------------------------------------------------------------------

_GRANTS_GOV_SAMPLE = json.dumps(
    {
        "errorcode": 0,
        "msg": "Webservice Succeeds",
        "data": {
            "hitCount": 17,
            "oppHits": [
                {
                    "id": "348164",
                    "number": "PD-23-1320",
                    "title": "Economics",
                    "agency": "U.S. National Science Foundation",
                    "openDate": "05/17/2023",
                    "closeDate": "",
                    "oppStatus": "posted",
                    "cfdaList": ["47.075"],
                },
                {
                    "id": "343980",
                    "number": "PD-22-1397",
                    "title": "SBE Postdoctoral Research Fellowships",
                    "agency": "U.S. National Science Foundation",
                    "openDate": "01/01/2022",
                    "closeDate": "11/04/2026",
                    "oppStatus": "posted",
                    "cfdaList": ["47.075"],
                },
                {
                    "id": "362419",
                    "number": "S-DR860-26-NOGO-0002",
                    "title": "Alumni Engagement Innovation Fund (AEIF) 2026",
                    "agency": "U.S. Mission to the Dominican Republic",
                    "openDate": "05/27/2026",
                    "closeDate": "06/04/2026",
                    "oppStatus": "posted",
                    "cfdaList": ["19.022"],
                },
            ],
        },
    }
)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def test_parse_grants_gov_date_iso_conversion():
    assert _parse_grants_gov_date("11/04/2026") == "2026-11-04"
    assert _parse_grants_gov_date("06/04/2026") == "2026-06-04"


def test_parse_grants_gov_date_empty_and_bad():
    assert _parse_grants_gov_date("") is None
    assert _parse_grants_gov_date(None) is None
    assert _parse_grants_gov_date("not-a-date") is None


# ---------------------------------------------------------------------------
# Hit parsing: dated + relevant only
# ---------------------------------------------------------------------------

def test_parse_hits_keeps_only_dated_relevant_records():
    payload = json.loads(_GRANTS_GOV_SAMPLE)
    records = parse_grants_gov_hits(payload, "NSF SBE", "NSF")

    # "Economics" has no closeDate -> dropped.
    # "Alumni Engagement Innovation Fund" is dated but title is not
    #   research-relevant -> dropped.
    # Only the dated, relevant SBE fellowship survives.
    assert len(records) == 1
    rec = records[0]
    assert rec["deadline_date"] == "2026-11-04"
    assert rec["type"] == "funding"
    assert "SBE Postdoctoral Research Fellowships" in rec["name"]
    assert "Grants.gov: NSF SBE" in rec["name"]
    assert rec["url"].endswith("/343980")
    assert rec["organization"] == "U.S. National Science Foundation"


def test_parse_hits_empty_payload_is_safe():
    assert parse_grants_gov_hits({}, "x", "NSF") == []
    assert parse_grants_gov_hits({"data": {}}, "x", "NSF") == []
    assert parse_grants_gov_hits({"data": {"oppHits": []}}, "x", "NSF") == []


def test_parse_hits_every_record_has_parseable_iso_date():
    payload = json.loads(_GRANTS_GOV_SAMPLE)
    records = parse_grants_gov_hits(payload, "NSF SBE", "NSF")
    # The contract: no date-less deadline ever leaves the parser.
    for rec in records:
        assert rec["deadline_date"]
        # Reparses as an ISO date.
        assert len(rec["deadline_date"]) == 10
        assert rec["deadline_date"][4] == "-" and rec["deadline_date"][7] == "-"
