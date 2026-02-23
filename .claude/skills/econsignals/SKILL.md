---
name: econsignals
description: Economics research intelligence - monitors papers, authors, deadlines across 20+ sources. Run /scan to collect, /brief for daily digest, /track to follow authors.
---

EconSignals collects and surfaces economics research signals: new papers, author activity, conference deadlines, and topic trends across 20+ sources. It stores everything in a local SQLite database and generates markdown reports on demand.

## Commands

| Command | Action |
|-|-|
| `/scan` | Run all sensors, collect new signals, store to DB |
| `/brief` | Generate daily digest from recent signals (last 24-48h) |
| `/track <author>` | Follow an author; surface their new papers and activity |
| `/deep <topic>` | Deep scan on a topic across all sources |
| `/deadlines` | List upcoming conference/journal submission deadlines |
| `/status` | Show DB stats, last scan time, active watches |

## Architecture

**Sensors** (`sensors/`): Python scripts that fetch from a single source and write JSON to stdout. Run with `python sensors/<name>.py`. Each outputs a list of signal objects.

**Lenses** (`lenses/`): Python scripts that query the DB and output markdown reports to stdout. Run with `python lenses/<name>.py`.

**Database**: `data/econsignals.db` (SQLite). All sensors write here; all lenses read from here.

**Profile**: `profile/identity.md` defines research interests, tracked authors, and relevance filters used by lenses.

**Watches**: `watches/` contains YAML configs for persistent tracking rules (authors, topics, journals).

## Running

```bash
# Collect signals
python sensors/ssrn.py | python lib/ingest.py
python sensors/nber.py | python lib/ingest.py

# Generate reports
python lenses/brief.py
python lenses/deadlines.py
```
