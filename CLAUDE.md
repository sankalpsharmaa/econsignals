# EconSignals

Personal economics research intelligence agent. Claude Code is the runtime. Python sensors collect data into SQLite. Lenses generate a daily email newsletter.

**Audience:** Applied micro / development / urban economist focused on India. JEL weights in `profile/jel_weights.json`. Research identity in `profile/identity.md`.

---

## Commands

| Command | Description |
|-|-|
| `/scan [watch]` | Run sensors for a named watch, or all watches if omitted |
| `/brief` | Generate and email today's newsletter |
| `/deadlines` | Show upcoming deadlines sorted by date |
| `/status` | Show sensor health, last run times, DB stats |

---

## `/scan [watch]`

Run all sensors in a watch. If no watch is given, run all watches sequentially.

**Execution protocol:**
1. Identify sensors for the watch (see table below).
2. For each sensor: `python -m econsignals.sensors.<sensor>`
3. Capture stdout as JSON. On error, log and continue.
4. Insert results into DB via `lib/db.py`.
5. Record run in `sensor_runs` table: sensor name, timestamp, status, row count.

**Sensors (run in parallel):**

| Category | Sensors |
|-|-|
| Papers | openalex, crossref, iza, bread, imf |
| Social | mastodon, bluesky, twitter_bridge |
| Deadlines | funding, conferences |

State for paper sensors lives in `watches/papers/state.json`.

---

## `/brief`

Generate and email the daily newsletter.

1. Run: `python -m econsignals.lenses.newsletter`
2. Newsletter gathers: deadlines/funding, top papers, social highlights.
3. Sends HTML email via SMTP (if configured).
4. Saves local copy to `reports/{YYYY-MM-DD}/newsletter.html`.

**Email delivery (set in `~/.zshrc`):**

| Variable | Description |
|-|-|
| `ECONSIGNALS_EMAIL_TO` | Recipient email |
| `GMAIL_APP_PASSWORD` | Gmail app password (recommended — [generate here](https://myaccount.google.com/apppasswords)) |
| `RESEND_API_KEY` | *Alternative:* Resend API key (needs domain verification) |

Tries Resend first, falls back to Gmail SMTP. Without either, newsletter is generated locally only.

---

## `/deadlines`

Query `deadlines` table. Print sorted by `due_date` ascending. Show: name, type (funding/conference), due date, URL.

**Priority funding sources tracked daily:**
- PEDL (Private Enterprise Development in Low-Income Countries)
- STEG (Structural Transformation and Economic Growth)
- IGC (International Growth Centre)
- J-PAL Research Initiatives
- NSF Social & Economic Sciences
- Weiss Fund, Russell Sage, Sloan Foundation
- USC Dornsife Economics

---

## `/status`

Print:
- Last run time and status for each sensor (from `sensor_runs`).
- DB row counts per table.
- Any sensors that have not run in >48 hours (flag as stale).

---

## Database

**Path:** `data/econsignals.db`
**Schema managed by:** `lib/db.py`

Key tables:

| Table | Purpose |
|-|-|
| `papers` | Deduplicated papers (DOI as primary key) |
| `authors` | Author records; `is_tracked` flag |
| `sensor_runs` | Audit log of every sensor execution |
| `deadlines` | Conferences and funding deadlines |
| `social_items` | Posts from social watch |

---

## File Layout

```
data/econsignals.db          SQLite database
profile/identity.md          Research identity and interests
profile/jel_weights.json     JEL code relevance weights
watches/                     Watch configuration files
reports/{YYYY-MM-DD}/        Generated reports and newsletters
econsignals/                 Python package
  sensors/                   One .py file per sensor
  lenses/                    Newsletter, daily brief, deadline alerts
  lib/                       DB helpers, dedup, relevance, zotero_profile
tests/                       Unit tests (pytest)
```

---

## Social Sensors

| Platform | Sensor | Method |
|-|-|-|
| Mastodon | `mastodon.py` | econtwitter.net public API |
| Bluesky | `bluesky.py` | AT Protocol firehose |
| Twitter/X | `twitter_bridge.py` | Exa API search (needs `EXA_API_KEY` env var) |

---

## Testing

Run unit tests: `python -m pytest tests/ -v`

Tests cover: `lib/normalize` (title/author normalization), `lib/dedup` (merge logic), `lib/db` (insert, find, upsert).

---

## Conventions

- Sensors output JSON to stdout. Errors to stderr. Exit 0 on partial success.
- All DB writes go through `lib/db.py`. Never write raw SQL in sensors.
- Reports are plain markdown or HTML. No front matter.
- Dates: ISO 8601 (`YYYY-MM-DD`). Timestamps: UTC.
- Never delete rows from `papers` or `sensor_runs`. Soft-delete only.
