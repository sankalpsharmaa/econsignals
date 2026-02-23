# EconSignals

Personal economics research intelligence agent. Claude Code is the runtime. Python sensors collect data into SQLite. Claude analyzes via lenses and generates markdown reports.

**Audience:** Applied micro / development / urban economist focused on India. JEL weights in `profile/jel_weights.json`. Research identity in `profile/identity.md`.

---

## Commands

| Command | Description |
|-|-|
| `/scan [watch]` | Run sensors for a named watch, or all watches if omitted |
| `/brief` | Generate today's daily brief |
| `/track <author>` | Add author to tracked list |
| `/deep <paper_id_or_doi>` | Deep analysis of a single paper |
| `/deadlines` | Show upcoming deadlines sorted by date |
| `/status` | Show sensor health, last run times, DB stats |

---

## `/scan [watch]`

Run all sensors in a watch. If no watch is given, run all watches sequentially.

**Execution protocol:**
1. Identify sensors for the watch (see table below).
2. For each sensor: `python .claude/skills/econsignals/sensors/<sensor>.py`
3. Capture stdout as JSON. On error, log and continue.
4. Insert results into DB via `lib/db.py`.
5. Record run in `sensor_runs` table: sensor name, timestamp, status, row count.

**Watch-to-sensor mapping:**

| Watch | Sensors |
|-|-|
| `papers` | openalex, nber, ssrn, crossref, repec_nep, worldbank, imf, iza, bread |
| `india` | igc, india_think_tanks, rbi, mospi, jpal_sa, openalex (India filter) |
| `social` | bluesky, mastodon, twitter_bridge |
| `deadlines` | funding, conferences |
| `authors` | openalex (author filter), semantic_scholar, bluesky (handle filter) |

Watch definitions live in `watches/`.

---

## `/brief`

Generate the daily brief.

1. Run: `python .claude/skills/econsignals/lenses/daily_brief.py`
2. Lens reads from DB, applies JEL weights, ranks items.
3. Write output to `reports/{YYYY-MM-DD}/daily_brief.md`.

---

## `/track <author_name>`

Add an author to the tracked list.

1. Look up author in `authors` table.
2. Set `is_tracked = 1`. Insert row if not present.
3. Confirm: "Now tracking: {author_name}."

---

## `/deep <paper_id_or_doi>`

Deep analysis of a single paper.

1. Fetch paper record from DB by ID or DOI.
2. Run: `python .claude/skills/econsignals/lenses/deep_paper.py --id <id>`
3. Write output to `reports/{YYYY-MM-DD}/deep_{id}.md`.

---

## `/deadlines`

Query `deadlines` table. Print sorted by `due_date` ascending. Show: name, type (funding/conference), due date, URL.

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
| `social_posts` | Posts from social watch |

---

## File Layout

```
data/econsignals.db          SQLite database
profile/identity.md          Research identity and interests
profile/jel_weights.json     JEL code relevance weights
watches/                     Watch configuration files
reports/{YYYY-MM-DD}/        Generated markdown reports
.claude/skills/econsignals/
  sensors/                   One .py file per sensor
  lenses/                    Analysis and report generators
  lib/db.py                  DB helpers (insert, query, upsert)
```

---

## Conventions

- Sensors output JSON to stdout. Errors to stderr. Exit 0 on partial success.
- All DB writes go through `lib/db.py`. Never write raw SQL in sensors.
- Reports are plain markdown. No HTML. No front matter.
- Dates: ISO 8601 (`YYYY-MM-DD`). Timestamps: UTC.
- Never delete rows from `papers` or `sensor_runs`. Soft-delete only.
