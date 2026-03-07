"""Deadline alert lens for EconSignals.

Generates countdown notifications for upcoming deadlines with alert suppression.
Thresholds: 60, 30, 14, 7, 3, 1 days. Skips thresholds already stored in
the `notified_days` JSON array for each deadline.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from econsignals.lib.db import get_db, get_upcoming_deadlines, init_db

PROJ_ROOT = Path(__file__).resolve().parents[2]

ALERT_THRESHOLDS = [60, 30, 14, 7, 3, 1]
URGENCY_LABELS = {60: "Notice", 30: "Reminder", 14: "Approaching", 7: "URGENT", 3: "CRITICAL", 1: "FINAL"}
URGENCY_EMOJI = {60: "", 30: "", 14: "!", 7: "!!", 3: "!!!", 1: "!!!!"}

# Section ordering: most urgent first
_SECTION_ORDER = [
    (1, "FINAL (1 day)"),
    (3, "CRITICAL (1-3 days)"),
    (7, "URGENT (7 days)"),
    (14, "Approaching (14 days)"),
    (30, "Reminder (30 days)"),
    (60, "Notice (60 days)"),
]

# Map threshold -> section key
_THRESHOLD_TO_SECTION: dict[int, int] = {
    1: 1,
    3: 3,
    7: 7,
    14: 14,
    30: 30,
    60: 60,
}


def _applicable_threshold(days_until: int) -> int | None:
    """Return the smallest threshold that is >= days_until, or None.

    For a deadline 28 days away, the applicable threshold is 30 (the deadline
    has entered the 30-day alert window).  We want the tightest window that
    the deadline currently falls within.

    Examples:
        days_until=60 -> 60
        days_until=28 -> 30
        days_until=5  -> 7
        days_until=0  -> 1
    """
    best: int | None = None
    for t in ALERT_THRESHOLDS:
        if t >= days_until:
            if best is None or t < best:
                best = t
    return best


def get_active_alerts() -> list[dict]:
    """Return deadlines that need alerting now.

    For each upcoming deadline:
    1. Calculate days until deadline.
    2. Find the applicable threshold (highest threshold <= days_until).
    3. Skip if already notified for this threshold.
    4. Include in alerts if not yet notified.

    Returns:
        List of alert dicts with keys:
            deadline (dict), days_until (int), threshold (int),
            urgency (str), marker (str), is_new (bool).
    """
    deadlines = get_upcoming_deadlines(days=60)
    today = date.today()
    alerts: list[dict] = []

    for dl in deadlines:
        deadline_date_str = dl.get("deadline_date")
        if not deadline_date_str:
            continue

        try:
            deadline_date = date.fromisoformat(deadline_date_str)
        except ValueError:
            continue

        days_until = (deadline_date - today).days
        if days_until < 0:
            continue

        threshold = _applicable_threshold(days_until)
        if threshold is None:
            continue

        notified: list[int] = dl.get("notified_days") or []
        if threshold in notified:
            continue

        alerts.append(
            {
                "deadline": dl,
                "days_until": days_until,
                "threshold": threshold,
                "urgency": URGENCY_LABELS[threshold],
                "marker": URGENCY_EMOJI[threshold],
                "is_new": True,
            }
        )

    return alerts


def _action_label(dl_type: str | None) -> str:
    mapping = {"funding": "Apply", "cfp": "Submit", "conference": "Details"}
    return mapping.get((dl_type or "").lower(), "Details")


def generate_deadline_alerts() -> str:
    """Generate markdown with deadline alerts grouped by urgency.

    Sections (most urgent first):
    - FINAL (1 day)
    - CRITICAL (1-3 days)
    - URGENT (7 days)
    - Approaching (14 days)
    - Reminder (30 days)
    - Notice (60 days)

    Each alert shows name, organization, days remaining, due date, and URL.

    Returns:
        Markdown string.
    """
    today_str = date.today().isoformat()
    alerts = get_active_alerts()

    # Bucket by section threshold
    buckets: dict[int, list[dict]] = {t: [] for t, _ in _SECTION_ORDER}
    for alert in alerts:
        section_key = _THRESHOLD_TO_SECTION[alert["threshold"]]
        buckets[section_key].append(alert)

    lines: list[str] = [f"# Deadline Alerts - {today_str}", ""]

    if not alerts:
        lines.append("*No new deadline alerts.*")
        lines.append("")
        return "\n".join(lines)

    for section_threshold, section_label in _SECTION_ORDER:
        section_alerts = buckets[section_threshold]
        lines.append(f"## {section_label}")
        if not section_alerts:
            lines.append("*(none)*")
            lines.append("")
            continue

        for alert in sorted(section_alerts, key=lambda a: a["days_until"]):
            dl = alert["deadline"]
            name = dl.get("name") or "Unnamed"
            org = dl.get("organization") or ""
            days = alert["days_until"]
            deadline_date = dl.get("deadline_date") or ""
            url = dl.get("url") or ""
            dl_type = dl.get("type")
            marker = alert["marker"]

            org_str = f" ({org})" if org else ""
            days_label = "day" if days == 1 else "days"
            action = _action_label(dl_type)

            line = f"- **{name}**{org_str} - {days} {days_label} remaining {marker}"
            lines.append(line)

            detail_parts = [f"Due: {deadline_date}"]
            if url:
                detail_parts.append(f"[{action}]({url})")
            lines.append(f"  {' | '.join(detail_parts)}")

            description = dl.get("description")
            if description:
                snippet = description[:120] + ("..." if len(description) > 120 else "")
                lines.append(f"  *{snippet}*")

        lines.append("")

    return "\n".join(lines)


def mark_notified(deadline_id: int, threshold: int) -> None:
    """Add threshold to the notified_days JSON array for this deadline.

    Args:
        deadline_id: Primary key of the deadline row.
        threshold: Day-count threshold to record (e.g. 7, 30).
    """
    conn = get_db()
    row = conn.execute(
        "SELECT notified_days FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()

    if row is None:
        conn.close()
        return

    raw = row["notified_days"]
    if raw and isinstance(raw, str):
        try:
            notified: list[int] = json.loads(raw)
        except json.JSONDecodeError:
            notified = []
    elif isinstance(raw, list):
        notified = raw
    else:
        notified = []

    if threshold not in notified:
        notified.append(threshold)

    with conn:
        conn.execute(
            "UPDATE deadlines SET notified_days = ? WHERE id = ?",
            (json.dumps(notified), deadline_id),
        )
    conn.close()


def process_alerts() -> tuple[str, int]:
    """Generate alerts and mark each as notified.

    Returns:
        (markdown string, number of new alerts generated).
    """
    alerts = get_active_alerts()
    md = generate_deadline_alerts()

    for alert in alerts:
        dl = alert["deadline"]
        deadline_id = dl.get("id")
        if deadline_id is not None:
            mark_notified(deadline_id, alert["threshold"])

    return md, len(alerts)


def write_alerts() -> tuple[Path, int]:
    """Generate alerts, mark as notified, and write to reports/{YYYY-MM-DD}/deadline_alerts.md.

    Returns:
        Tuple of (path to written file, number of new alerts).
    """
    today_str = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today_str
    report_dir.mkdir(parents=True, exist_ok=True)

    md, count = process_alerts()
    out_path = report_dir / "deadline_alerts.md"
    out_path.write_text(md, encoding="utf-8")
    return out_path, count


if __name__ == "__main__":
    init_db()
    path, count = write_alerts()
    print(json.dumps({"status": "ok", "alerts": count, "path": str(path)}))
