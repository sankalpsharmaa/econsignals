"""Tests for econsignals.lib.feedback_import.

Round-trips a dashboard-exported FeedbackStore JSON into data/feedback.jsonl
and asserts the line format, vote->interesting mapping, vote:null skipping, and
idempotency on re-import. All file IO targets pytest's tmp_path; nothing touches
the repo's real data/feedback.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path

from econsignals.lib.feedback_import import (
    import_feedback_export,
    records_from_export,
)

# An exported FeedbackStore: one up vote, one down vote, one vote:null (skip),
# and one entry with no votedAt-relevant vote at all.
_EXPORT = {
    "101": {
        "starred": True,
        "vote": "up",
        "hidden": False,
        "votedAt": "2026-05-28T10:00:00Z",
    },
    "202": {
        "starred": False,
        "vote": "down",
        "hidden": False,
        "votedAt": "2026-05-28T11:30:00Z",
    },
    "303": {
        "starred": True,
        "vote": None,
        "hidden": False,
    },
}


def _read_lines(log_path: Path) -> list[dict]:
    """Parse every jsonl line in the log into a dict."""
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_export(tmp_path: Path, store: dict) -> Path:
    """Write a FeedbackStore dict to an export JSON file under tmp_path."""
    export_path = tmp_path / "econsignals-feedback-2026-05-28.json"
    export_path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    return export_path


def test_records_skip_null_votes() -> None:
    """vote:null entries produce no record; up/down map to interesting bools."""
    records = records_from_export(_EXPORT)

    assert len(records) == 2
    by_id = {r["paper_id"]: r for r in records}
    assert by_id[101]["interesting"] is True
    assert by_id[202]["interesting"] is False
    assert 303 not in by_id


def test_import_writes_matching_jsonl(tmp_path: Path) -> None:
    """Imported lines carry exactly paper_id (int), interesting (bool), at (str)."""
    export_path = _write_export(tmp_path, _EXPORT)
    log_path = tmp_path / "feedback.jsonl"

    added = import_feedback_export(export_path, log_path)

    assert added == 2
    lines = _read_lines(log_path)
    assert len(lines) == 2
    for line in lines:
        assert set(line.keys()) == {"paper_id", "interesting", "at"}
        assert isinstance(line["paper_id"], int)
        assert isinstance(line["interesting"], bool)
        assert isinstance(line["at"], str)

    by_id = {line["paper_id"]: line for line in lines}
    assert by_id[101]["interesting"] is True
    assert by_id[101]["at"] == "2026-05-28T10:00:00Z"
    assert by_id[202]["interesting"] is False


def test_reimport_is_idempotent(tmp_path: Path) -> None:
    """Re-importing the same export appends nothing and leaves line count fixed."""
    export_path = _write_export(tmp_path, _EXPORT)
    log_path = tmp_path / "feedback.jsonl"

    first = import_feedback_export(export_path, log_path)
    second = import_feedback_export(export_path, log_path)

    assert first == 2
    assert second == 0
    assert len(_read_lines(log_path)) == 2


def test_later_export_appends_only_new_votes(tmp_path: Path) -> None:
    """A later export that adds one new vote appends exactly that one line."""
    log_path = tmp_path / "feedback.jsonl"
    import_feedback_export(_write_export(tmp_path, _EXPORT), log_path)

    # User casts a new vote on paper 404; 101/202 keys are unchanged.
    later = dict(_EXPORT)
    later["404"] = {
        "starred": False,
        "vote": "up",
        "hidden": False,
        "votedAt": "2026-05-29T09:15:00Z",
    }
    later_path = tmp_path / "econsignals-feedback-2026-05-29.json"
    later_path.write_text(json.dumps(later), encoding="utf-8")

    added = import_feedback_export(later_path, log_path)

    assert added == 1
    lines = _read_lines(log_path)
    assert len(lines) == 3
    assert {line["paper_id"] for line in lines} == {101, 202, 404}


def test_changed_vote_appends_new_line(tmp_path: Path) -> None:
    """Flipping a vote (new votedAt) records a fresh line; latest-wins is the
    reader's job (learned_ranker keeps the last vote per paper_id)."""
    log_path = tmp_path / "feedback.jsonl"
    import_feedback_export(_write_export(tmp_path, _EXPORT), log_path)

    flipped = dict(_EXPORT)
    flipped["101"] = {
        "starred": True,
        "vote": "down",
        "hidden": False,
        "votedAt": "2026-05-30T08:00:00Z",
    }
    flipped_path = tmp_path / "econsignals-feedback-2026-05-30.json"
    flipped_path.write_text(json.dumps(flipped), encoding="utf-8")

    added = import_feedback_export(flipped_path, log_path)

    assert added == 1
    rows_101 = [line for line in _read_lines(log_path) if line["paper_id"] == 101]
    assert len(rows_101) == 2
    assert {r["interesting"] for r in rows_101} == {True, False}
