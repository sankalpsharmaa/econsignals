"""Import dashboard-exported feedback into data/feedback.jsonl.

The static GitHub Pages deployment has no FastAPI backend, so thumbs up/down
votes never reach ``POST /api/feedback``; they live only in the browser's
localStorage and can be downloaded as ``econsignals-feedback-*.json`` via the
dashboard's export button (see ``webapp/src/lib/storage.ts``). This module
closes that loop: it ingests such an exported JSON file into the same
``data/feedback.jsonl`` audit trail that ``/api/feedback`` appends to, so the
learned ranker (``econsignals/lib/learned_ranker.py``) can train on votes
collected from the static site.

Exported shape (``FeedbackStore`` in ``webapp/src/types.ts``)::

    {
      "<paper_id>": {
        "starred": bool,
        "vote": "up" | "down" | null,
        "hidden": bool,
        "votedAt": "YYYY-MM-DDTHH:MM:SSZ"   # present once voted
      },
      ...
    }

Emitted jsonl line (byte-compatible with ``/api/feedback``)::

    {"paper_id": <int>, "interesting": <bool>, "at": "<iso>"}

The ``vote`` field maps to ``interesting``: ``up`` -> True, ``down`` -> False;
``null`` (or missing) votes are skipped. ``starred`` and ``hidden`` are ignored
because the learned ranker reads only ``paper_id`` and ``interesting``.

The append is idempotent: a record is keyed by ``(paper_id, at)`` and is written
only if that pair is absent from the log, so re-importing the same export (or an
overlapping later export) adds nothing. Unlike ``/api/feedback`` this does NOT
call ``update_jel_weights``: that is a stateful EMA nudge and replaying it on
every re-import would corrupt the weights. Static-site votes therefore feed the
learned ranker but do not nudge ``profile/jel_weights.json`` by design.

Usage::

    python -m econsignals.lib.feedback_import econsignals-feedback-2026-05-28.json

or programmatically::

    from econsignals.lib.feedback_import import import_feedback_export
    n = import_feedback_export(Path("econsignals-feedback-2026-05-28.json"))
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from econsignals.lib.db import PROJ_ROOT

# Default audit trail; the same file /api/feedback and learned_ranker use.
_FEEDBACK_LOG: Path = PROJ_ROOT / "data" / "feedback.jsonl"

# Maps the dashboard's tri-state vote to the jsonl `interesting` boolean.
_VOTE_TO_INTERESTING: dict[str, bool] = {"up": True, "down": False}


def _utc_stamp() -> str:
    """Return a second-precision UTC timestamp matching /api/feedback's format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _existing_keys(log_path: Path) -> set[tuple[int, str]]:
    """Read the (paper_id, at) keys already present in the jsonl log.

    Skips malformed or incomplete lines so a partly-corrupt log still dedups
    against its readable records.
    """
    keys: set[tuple[int, str]] = set()
    if not log_path.exists():
        return keys
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = rec.get("paper_id")
            at = rec.get("at")
            if pid is None or at is None:
                continue
            keys.add((int(pid), str(at)))
    return keys


def records_from_export(store: dict) -> list[dict]:
    """Convert an exported FeedbackStore dict into jsonl records.

    Each voted entry becomes ``{"paper_id": int, "interesting": bool, "at": iso}``.
    Entries with no up/down vote are skipped. Entries missing ``votedAt`` (older
    exports made before the dashboard stamped votes) fall back to the current
    UTC time; this keeps their key stable within a single import but means such
    a legacy record can re-append on a later import (no timestamp to dedup on).
    """
    records: list[dict] = []
    for paper_id_str, entry in store.items():
        if not isinstance(entry, dict):
            continue
        vote = entry.get("vote")
        if vote not in _VOTE_TO_INTERESTING:
            continue
        try:
            paper_id = int(paper_id_str)
        except (TypeError, ValueError):
            continue
        at = entry.get("votedAt") or _utc_stamp()
        records.append(
            {
                "paper_id": paper_id,
                "interesting": _VOTE_TO_INTERESTING[vote],
                "at": str(at),
            }
        )
    return records


def import_feedback_export(
    export_path: Path,
    log_path: Path = _FEEDBACK_LOG,
) -> int:
    """Append novel votes from an exported feedback JSON into the jsonl log.

    Idempotent: a record already present (matched on ``(paper_id, at)``) is not
    re-written, and duplicates within the export itself collapse to one line.
    Returns the number of new lines appended.
    """
    raw = export_path.read_text(encoding="utf-8")
    store = json.loads(raw)
    if not isinstance(store, dict):
        raise ValueError(
            f"Expected a FeedbackStore object at top level, got {type(store).__name__}"
        )

    seen = _existing_keys(log_path)
    new_records: list[dict] = []
    for record in records_from_export(store):
        key = (record["paper_id"], record["at"])
        if key in seen:
            continue
        seen.add(key)
        new_records.append(record)

    if not new_records:
        return 0

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        for record in new_records:
            fh.write(json.dumps(record) + "\n")
    return len(new_records)


def main(argv: list[str] | None = None) -> int:
    """CLI: import one exported feedback JSON into data/feedback.jsonl."""
    parser = argparse.ArgumentParser(
        description="Import a dashboard-exported econsignals-feedback-*.json "
        "into data/feedback.jsonl for the learned ranker.",
    )
    parser.add_argument(
        "export",
        type=Path,
        help="Path to an exported econsignals-feedback-*.json file.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=_FEEDBACK_LOG,
        help="Target jsonl log (default: data/feedback.jsonl).",
    )
    args = parser.parse_args(argv)

    try:
        added = import_feedback_export(args.export, args.log)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"feedback_import: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"imported": added, "log": str(args.log)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
