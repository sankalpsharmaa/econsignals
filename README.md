# EconSignals

EconSignals is a Python research-intelligence pipeline that collects papers, social posts, and deadlines into SQLite, then generates local HTML reports and an email newsletter.

## Run locally

```bash
python -m pip install -r requirements.txt
python -m pytest tests/ -v
python -m econsignals.lib.db
python -m econsignals.sensors.openalex
python -m econsignals.lenses.newsletter --days 1 --no-send
```

You can also run the full workflow with:

```bash
./scripts/run_econsignals.sh
```

## Run on cloud

EconSignals can run on a VM, container, or scheduled job runner as long as you provide a persistent working directory for the database and reports.

1. Deploy the repository to the host.
2. Set `ECONSIGNALS_ROOT` to the directory that should hold `.env`, `data/`, `reports/`, `profile/`, and `watches/`.
3. Set `ECONSIGNALS_DB` if you want the SQLite file somewhere other than `${ECONSIGNALS_ROOT}/data/econsignals.db`.
4. Provide the required environment variables for your sensors and email delivery.
5. Run `./scripts/run_econsignals.sh` from a scheduler such as cron, GitHub Actions, Railway cron, or a cloud VM timer.

Example:

```bash
export ECONSIGNALS_ROOT=/srv/econsignals
export ECONSIGNALS_DB=/srv/econsignals/data/econsignals.db
export OPENALEX_EMAIL=you@example.com
export ECONSIGNALS_EMAIL_TO=you@example.com

./scripts/run_econsignals.sh
```

### Cloud deployment notes

- Use a persistent disk or mounted volume for SQLite and generated reports.
- Keep `.env` in `ECONSIGNALS_ROOT` if you want the newsletter job to auto-load it.
- Resend email uses `RESEND_API_KEY` with `RESEND_EMAIL_FROM` (or `ECONSIGNALS_EMAIL_FROM`).
- Gmail fallback uses `GMAIL_APP_PASSWORD` with `GMAIL_EMAIL` (or `ECONSIGNALS_EMAIL_FROM`).
- If no email credentials are configured, reports are still generated locally in `reports/{YYYY-MM-DD}/`.
