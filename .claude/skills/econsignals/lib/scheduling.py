"""Sensor run frequency management for EconSignals.

Tracks when each sensor last ran and determines whether it is due to run again
based on a fixed interval schedule. Sensors that errored on their last run are
always treated as due (retry semantics).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

SKILL_ROOT = Path(__file__).resolve().parent.parent
PROJ_ROOT = SKILL_ROOT.parent.parent.parent

SENSOR_SCHEDULE: dict[str, dict] = {
    # papers watch - high frequency
    "openalex":      {"interval_hours": 12,  "watch": "papers"},
    "nber":          {"interval_hours": 24,  "watch": "papers"},
    "ssrn":          {"interval_hours": 24,  "watch": "papers"},
    "crossref":      {"interval_hours": 24,  "watch": "papers"},
    "repec_nep":     {"interval_hours": 24,  "watch": "papers"},
    "worldbank":     {"interval_hours": 24,  "watch": "papers"},
    "imf":           {"interval_hours": 168, "watch": "papers"},  # weekly
    "iza":           {"interval_hours": 168, "watch": "papers"},
    "bread":         {"interval_hours": 168, "watch": "papers"},
    "rss_feeds":     {"interval_hours": 12,  "watch": "papers"},
    # india watch
    "igc":               {"interval_hours": 168, "watch": "india"},
    "india_think_tanks": {"interval_hours": 168, "watch": "india"},
    "rbi":               {"interval_hours": 168, "watch": "india"},
    "mospi":             {"interval_hours": 168, "watch": "india"},
    "jpal_sa":           {"interval_hours": 168, "watch": "india"},
    # social watch - high frequency
    "bluesky":         {"interval_hours": 8, "watch": "social"},
    "mastodon":        {"interval_hours": 8, "watch": "social"},
    "twitter_bridge":  {"interval_hours": 8, "watch": "social"},
    # deadlines watch
    "funding":     {"interval_hours": 168, "watch": "deadlines"},
    "conferences": {"interval_hours": 168, "watch": "deadlines"},
    # authors watch
    "semantic_scholar": {"interval_hours": 24, "watch": "authors"},
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp string to an aware UTC datetime."""
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def should_run(sensor: str) -> bool:
    """Check whether a sensor is due to run.

    Returns True when any of the following hold:
    - The sensor has never run before.
    - The elapsed time since the last completed run exceeds the configured interval.
    - The last run finished with status='error' (always retry).

    Args:
        sensor: Name of the sensor to check.

    Returns:
        True if the sensor should run now, False otherwise.

    Raises:
        KeyError: If the sensor is not in SENSOR_SCHEDULE.
    """
    if sensor not in SENSOR_SCHEDULE:
        raise KeyError(f"Unknown sensor: {sensor!r}")

    from .db import get_last_sensor_run  # noqa: PLC0415

    last = get_last_sensor_run(sensor)

    if last is None:
        return True

    if last.get("status") == "error":
        return True

    finished_at = _parse_ts(last.get("finished_at"))
    if finished_at is None:
        # Run started but never finished - treat as due
        return True

    interval = timedelta(hours=SENSOR_SCHEDULE[sensor]["interval_hours"])
    return (_now_utc() - finished_at) >= interval


def get_due_sensors(watch: str | None = None) -> list[str]:
    """Return sensor names that are due to run, ordered by urgency.

    Sensors that are overdue appear first, sorted by how many hours past their
    scheduled time they are (most overdue first). Sensors not yet due are
    excluded entirely.

    Args:
        watch: Optional watch filter (e.g. 'papers', 'social'). When provided,
               only sensors belonging to that watch are considered.

    Returns:
        List of sensor names sorted by overdue_hours descending.
    """
    candidates = [
        name
        for name, cfg in SENSOR_SCHEDULE.items()
        if watch is None or cfg["watch"] == watch
    ]

    due: list[tuple[str, float]] = []
    for name in candidates:
        if should_run(name):
            oh = _overdue_hours(name)
            due.append((name, oh))

    due.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in due]


def next_run_at(sensor: str) -> datetime | None:
    """Return the UTC datetime when this sensor should next run.

    Returns None if the sensor has never run (it is already due immediately).

    Args:
        sensor: Name of the sensor.

    Returns:
        Aware UTC datetime of the next scheduled run, or None if no prior run.

    Raises:
        KeyError: If the sensor is not in SENSOR_SCHEDULE.
    """
    if sensor not in SENSOR_SCHEDULE:
        raise KeyError(f"Unknown sensor: {sensor!r}")

    from .db import get_last_sensor_run  # noqa: PLC0415

    last = get_last_sensor_run(sensor)
    if last is None:
        return None

    finished_at = _parse_ts(last.get("finished_at"))
    if finished_at is None:
        return None

    interval = timedelta(hours=SENSOR_SCHEDULE[sensor]["interval_hours"])
    return finished_at + interval


def _overdue_hours(sensor: str) -> float:
    """Hours past the scheduled run time. Negative means not yet due."""
    nxt = next_run_at(sensor)
    if nxt is None:
        # Never run - treat as infinitely overdue so it sorts first
        return float("inf")
    delta = _now_utc() - nxt
    return delta.total_seconds() / 3600


def get_schedule_status() -> list[dict]:
    """Return a status record for every sensor in the schedule.

    Each record contains:
        sensor        - sensor name
        watch         - watch category
        interval_hours - configured run interval
        last_run      - ISO timestamp of last finished_at, or None
        last_status   - status string from last run, or None
        next_run      - ISO timestamp of next scheduled run (now if never run)
        is_due        - whether the sensor should run now
        overdue_hours - hours past schedule; negative = not yet due

    Returns:
        List of dicts sorted by next_run ascending.
    """
    from .db import get_last_sensor_run  # noqa: PLC0415

    now = _now_utc()
    records: list[dict] = []

    for name, cfg in SENSOR_SCHEDULE.items():
        last = get_last_sensor_run(name)
        last_run_ts: str | None = None
        last_status: str | None = None
        nxt: datetime = now  # default: due immediately

        if last is not None:
            last_run_ts = last.get("finished_at")
            last_status = last.get("status")
            finished_at = _parse_ts(last_run_ts)
            if finished_at is not None:
                nxt = finished_at + timedelta(hours=cfg["interval_hours"])

        overdue = (now - nxt).total_seconds() / 3600

        # is_due mirrors should_run logic without a second DB call
        is_due = (
            last is None
            or last_status == "error"
            or _parse_ts(last.get("finished_at") if last else None) is None
            or overdue >= 0
        )

        records.append(
            {
                "sensor": name,
                "watch": cfg["watch"],
                "interval_hours": cfg["interval_hours"],
                "last_run": last_run_ts,
                "last_status": last_status,
                "next_run": nxt.isoformat(),
                "is_due": is_due,
                "overdue_hours": round(overdue, 2),
            }
        )

    records.sort(key=lambda r: r["next_run"])
    return records


def get_watch_sensors(watch: str) -> list[str]:
    """Return all sensor names that belong to the given watch.

    Args:
        watch: Watch category name (e.g. 'papers', 'india', 'social').

    Returns:
        List of sensor names in definition order.
    """
    return [name for name, cfg in SENSOR_SCHEDULE.items() if cfg["watch"] == watch]


def sensor_exists(sensor: str) -> bool:
    """Return True if the sensor name is present in SENSOR_SCHEDULE."""
    return sensor in SENSOR_SCHEDULE


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(SKILL_ROOT))
    from lib.db import init_db  # type: ignore[import]

    init_db()

    status = get_schedule_status()
    print(f"{'Sensor':<20} {'Watch':<12} {'Interval':<10} {'Last Run':<22} {'Due?':<6}")
    print("-" * 72)
    for s in status:
        last = s["last_run"][:19] if s["last_run"] else "never"
        due = "YES" if s["is_due"] else "no"
        print(
            f"{s['sensor']:<20} {s['watch']:<12} {s['interval_hours']:<10}h"
            f" {last:<22} {due:<6}"
        )
