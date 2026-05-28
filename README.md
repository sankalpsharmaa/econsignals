# EconSignals

Personal research-intelligence agent for an applied-micro / development / urban economist focused on **India**. Python sensors collect new papers, funding deadlines, and econ social chatter into SQLite; a relevance engine ranks them against your profile and Zotero library; and a static dashboard (plus an optional daily email) surfaces what matters.

**Live dashboard:** https://sankalpsharmaa.github.io/econsignals/

---

## What it does

1. **Collects** papers (OpenAlex, Crossref, IZA, BREAD, IMF), funding/conference deadlines (PEDL, STEG, IGC, J-PAL, NSF, …), and econ social posts (Mastodon, Bluesky, Twitter/Exa).
2. **Deduplicates** records across sources by canonical title + author identity.
3. **Ranks** papers by topical fit (JEL + interest keywords), India relevance, source prestige, recency, and social signal, personalized with your Zotero corpus via local embeddings.
4. **Surfaces** the result as an interactive dashboard and an optional daily HTML email.

Architecture details: [`ARCHITECTURE.txt`](ARCHITECTURE.txt). Decision log: [`docs/decisions.md`](docs/decisions.md).

---

## Quickstart

```bash
# 1. Install (editable). Core is stdlib + numpy; add the web backend extra optionally.
python3 -m pip install -e ".[web,dev]"

# 2. Create the database schema (also runs one-time migrations).
python3 -m econsignals.lib.db

# 3. Collect data (set OPENALEX_EMAIL first for the polite pool — see below).
python3 -m econsignals.sensors.openalex
python3 -m econsignals.sensors.crossref
python3 -m econsignals.sensors.funding
# ...or run every sensor via the pipeline script:
bash scripts/run_econsignals.sh

# 4. Score relevance and build the dashboard data.
econsignals-score          # (re)score every paper
econsignals-snapshot       # write webapp/public/feed.json

# 5a. View the dashboard locally (static):
cd webapp && npm install && npm run dev      # http://localhost:5173

# 5b. ...or run the live backend (serves feed from the DB + records feedback):
econsignals-web            # http://127.0.0.1:8000  (API at /api/feed)
```

---

## The dashboard

A React + Vite single-page app in [`webapp/`](webapp/). It reads a static snapshot (`webapp/public/feed.json`) so it can deploy to GitHub Pages with no backend, and falls back to a live API when `VITE_API_BASE` is set.

| Task | Command |
|-|-|
| Refresh the data snapshot | `econsignals-snapshot` (writes `webapp/public/feed.json`) |
| Dev server | `cd webapp && npm run dev` |
| Production build | `cd webapp && npm run build` → `webapp/dist/` |
| Live backend (optional) | `econsignals-web` |

**Deploy:** pushing to `main` runs [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml), which builds the SPA and publishes it to GitHub Pages. Refresh the data by running `econsignals-snapshot` and committing the updated `feed.json` before you push. (Enable Pages once under repo Settings → Pages → Source: GitHub Actions.)

---

## Daily email brief (optional)

```bash
python3 -m econsignals.lenses.newsletter     # writes reports/{date}/ and emails if configured
```

Delivery tries Resend, then Gmail SMTP. Without either, the newsletter is generated locally only.

---

## Configuration (environment variables)

| Variable | Purpose |
|-|-|
| `OPENALEX_EMAIL` | Polite-pool identifier for reliable OpenAlex access (recommended) |
| `EXA_API_KEY` | Enables the Twitter/Exa bridge and IMF fallback search |
| `ZOTERO_DB_PATH` | Override the Zotero DB path (default `~/Zotero/zotero.sqlite`) |
| `OLLAMA_HOST` | Ollama URL for embeddings (default `localhost:11434`; needs `nomic-embed-text`) |
| `ECONSIGNALS_DB` | Override the SQLite path (default `data/econsignals.db`) |
| `ECONSIGNALS_EMAIL_TO` | Newsletter recipient |
| `RESEND_API_KEY` / `RESEND_EMAIL_FROM` | Resend delivery (preferred) |
| `GMAIL_EMAIL` / `GMAIL_APP_PASSWORD` | Gmail SMTP fallback |
| `ECONSIGNALS_EMAIL_FROM` | Shared sender fallback for either provider |

Personalization needs a local [Ollama](https://ollama.com) with the embedding model: `ollama pull nomic-embed-text`. Without it, ranking falls back to lexical Zotero overlap.

---

## Your profile

Tune what EconSignals considers relevant:

- [`profile/identity.md`](profile/identity.md) — fields, geographic focus, **Topics of Interest** (keyword phrases), tracked authors.
- [`profile/jel_weights.json`](profile/jel_weights.json) — per-JEL-code weights (auto-nudged by dashboard 👍/👎 feedback).

---

## Tests

```bash
python3 -m pytest tests/ -v
```
