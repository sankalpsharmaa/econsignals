# Copilot instructions for EconSignals

EconSignals is a Python research-intelligence pipeline. Sensors collect papers, social posts, and deadlines into SQLite, then lenses generate local reports and an email newsletter.

## Where to work

- Runtime code lives in `/home/runner/work/econsignals/econsignals/econsignals/`.
- Sensor entry points are `python -m econsignals.sensors.<name>`.
- Report generation entry points are `python -m econsignals.lenses.newsletter` and `python -m econsignals.lenses.deadline_alert`.
- Database access belongs in `econsignals/lib/db.py`; sensors should not write raw SQL directly.

## Project conventions

- Sensors print JSON to stdout, errors to stderr, and should exit `0` on partial success.
- Dates use ISO 8601 (`YYYY-MM-DD`); timestamps should be UTC.
- Preserve rows in `papers` and `sensor_runs`; prefer soft-delete behavior.
- Keep changes minimal and reliability-focused. Fix concrete runtime problems, not style.

## Configuration expectations

- Full runs expect `OPENALEX_EMAIL` and `ECONSIGNALS_EMAIL_TO`.
- Email delivery can use:
  - `RESEND_API_KEY` with `RESEND_EMAIL_FROM` (or `ECONSIGNALS_EMAIL_FROM`)
  - `GMAIL_APP_PASSWORD` with `GMAIL_EMAIL` (or `ECONSIGNALS_EMAIL_FROM`)
- Optional integrations include `EXA_API_KEY`, `ZOTERO_DB_PATH`, `OLLAMA_HOST`, and `ECONSIGNALS_DB`.

## Validation

- Install dependencies with `python -m pip install -r requirements.txt`.
- Run tests with `python -m pytest tests/ -v`.
- Prefer targeted tests for the files you touch before running the broader suite.
