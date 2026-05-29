# EconSignals Decisions Log

Append-only audit trail. Newest entries at top. See `~/claude-config/rules/decisions-log.md`.

---

## 2026-05-29 14:32, Decision 21, Daily CI/CD data refresh via GitHub Actions; persist the 17 MB DB as a release asset; merge revitalize-dashboard -> main

Stage: code-choice
Tried:
  - Commit the 17 MB SQLite DB into the repo for CI state — rejected: daily binary commits bloat history (~6 GB/yr)
  - actions/cache for the DB — rejected: best-effort, 7-day eviction would lose the accumulated corpus (the source of truth)
  - GitHub release asset (tag data-snapshot, prerelease) holding econsignals.db + state.json, restored at job start and re-uploaded --clobber at end (kept) — durable, no history bloat, purpose-built for binaries
  - Local launchd cron — rejected: user asked for CI "in econsignals"; Actions runs regardless of laptop state
Kept: .github/workflows/refresh-data.yml (cron daily 11:00 UTC + workflow_dispatch): restore DB asset -> scripts/refresh_data.sh (15 sensors + score_all_papers, NO email) -> scripts/deploy_site.sh (snapshot + vite build + gh-pages force-push) -> re-upload DB asset. New scripts/refresh_data.sh (no-email scan). deploy_site.sh gains a CI-only token-in-URL push (GH_PAGES_TOKEN) so the throwaway-repo force-push authenticates on the runner. Committed the full revitalization working set (5 new sensors, 5 new libs, 13 tests, dashboard rework) and fast-forward merged revitalize-dashboard -> main so the scheduled workflow lives on the default branch.
Dropped: emailing the brief in CI (user chose dashboard-only); committing the DB to git; Zotero in CI (ECONSIGNALS_NO_ZOTERO=1 — CI ranking uses relevance + learned_ranker percentiles; run locally for the Zotero channel)
Files: .github/workflows/refresh-data.yml (new), scripts/refresh_data.sh (new), scripts/deploy_site.sh, docs/decisions.md
Assumptions (the part that can be wrong):
  - The release asset is seeded with the current local 17 MB DB BEFORE the first CI run, else CI deploys a near-empty feed.json and regresses the live dashboard
  - GITHUB_TOKEN with contents:write authorizes the gh-pages push + release upload; the token's workflow scope lets it push the workflow file
  - Sensors degrade gracefully when a secret (EXA/Bluesky) is absent; keyless sensors (crossref/nber/arxiv/nep/worldbank/iza/bread/imf/mastodon/conferences/funding) still populate
  - requirements.txt is slim (certifi/numpy/pytest; torch/transformers optional-off), so CI installs in seconds
Verify: gh workflow run "Refresh data" && gh run watch ; then curl -s -o /dev/null -w '%{http_code}' https://sankalpsharmaa.github.io/econsignals/ -> 200 and feed.json paper count is unchanged-or-higher
Status: accepted

## 2026-05-29 14:18, Decision 20, Serve the dashboard from this private repo's gh-pages (GitHub Pro); retire the separate public site repo

Stage: divergence
Tried:
  - GitHub Actions Pages, building from committed HEAD — rejected: the working tree is dirty in exactly the dashboard files (Decision 19 rework: DeadlinesView.tsx, index.css, types.ts, storage.ts, feed.json), so a CI build from HEAD would regress the live site; also forces a premature revitalize-dashboard -> main merge
  - Local build force-pushed to an orphan gh-pages branch of econsignals; Pages serves it (kept) — reproduces the current working-tree site exactly, no merge/commit reckoning, a surgical edit to one script
Kept: deploy_site.sh now builds snapshot+app and force-pushes webapp/dist (+ .nojekyll) to econsignals' gh-pages; Pages enabled with source gh-pages:/. URL is unchanged (a project Pages site serves at https://sankalpsharmaa.github.io/econsignals/, so base '/econsignals/' stays valid). GitHub Pro enables Pages on a private repo, so code stays private without an external repo.
Dropped: the external public user-site repo sankalpsharmaa/sankalpsharmaa.github.io (held only .nojekyll + /econsignals/; root already 404, so nothing else is lost) — deleted after the gh-pages site verifies live
Files: scripts/deploy_site.sh, README.md, docs/decisions.md
Assumptions (the part that can be wrong):
  - GitHub Pro is active on the account (user asserts) — required for private-repo Pages
  - feed.json regeneration stays local (Pages has no DB); the gh credential helper authorizes the gh-pages force-push to origin
  - Deleting the user-site repo removes only the dashboard (verified: contents = .nojekyll + econsignals/, root 404); the gh token lacks delete_repo, so the user grants the scope or clicks delete
Verify: after delete, curl -s -o /dev/null -w '%{http_code}' https://sankalpsharmaa.github.io/econsignals/ -> 200, and gh api repos/sankalpsharmaa/econsignals/pages --jq .source.branch -> gh-pages
Status: accepted; reverses Decision 10

## 2026-05-29 05:10, Decision 19, Drastically rebuild the funding/grants section (registry + topical scoring + amount/eligibility + projection)

Stage: code-choice
Tried:
  - Keep flat org-based relevance (0.8 dev-funder / 0.5 else) - rejected: NSF-infra / fishing / NIH Grants.gov noise scored 0.5, same as real dev funders
  - Topical score_funding(name, org, scope, tier, eligibility, india_eligible) + tiered curated registry + a Grants.gov score gate (kept)
Kept: (1) Rebuilt FUNDING_SOURCES into a 23-funder primary-source-verified registry (4 research agents; reports/funders_research_2026.json + funding_research_2026.json), each with tier/eligibility/amount/india_eligible/scope + known_deadlines carrying verified explicit dates (Weiss Aug 1, Fulbright-India Oct 6, Russell Sage Jul 15/Oct 28) and recurrence projection. (2) score_funding: tier base (core .88 / relevant .62 / peripheral .42) + topical-fit + India/eligibility adjustment; Grants.gov hits gated at >=0.50 so instrumentation/fishing/biomedical drop. (3) Schema: amount + eligibility columns (db v2 migration), shown as dashboard badges; DeadlinesView sorts dated-soonest then rolling-by-relevance. (4) curated_deadline_dates honors verified future dates else projects the next occurrence; stale-past scraped dates fall back to the projection, not a rolling row. (5) Deadline window 120 -> 270 days.
Dropped: ~18 inapplicable funders (India-ineligible / internal-only / pre-PhD / discontinued: IDRF, NSF-B2, Smith-Richardson, WT-Grant, GLM-LIC, WB-DIME, TCI, IFPRI, OpenPhil, IDRC, Gates-GC, Omidyar, Y-RISE, DigiFI, EGAP, ICTP, AEA-Summer); the cfda 47.075 Grants.gov query (pulled NSF infra); flat _HIGH_RELEVANCE_ORGS
Files: econsignals/sensors/funding.py, econsignals/lib/db.py (deadlines schema + migration v2), econsignals/lib/snapshot.py, webapp/src/types.ts, webapp/src/components/DeadlinesView.tsx, webapp/src/index.css, tests/test_funding_scoring.py, tests/test_sensor_regressions.py
Assumptions (the part that can be wrong):
  - Most 2026 cycles already closed (verified); the registry projects the NEXT occurrence, an estimate (per-funder confidence in the research JSON) until each funder posts its dated call
  - Dropping inapplicable funders (signal over coverage) matches "drastically improve"; user is a USC PhD doing India dev/urban/ag work
  - Live DB funding deadlines were purged (type='funding') and repopulated; cfp conferences untouched
Verify: cd /Users/sankalpsharma/econsignals && PYTHONPATH=. python -m pytest tests/test_funding_scoring.py -q  (10 passed); then python -m econsignals.sensors.funding && python -c "import json;ds=json.load(open('webapp/public/feed.json'))['deadlines'];print(sum(1 for d in ds if d['type']=='funding' and d.get('amount')),'funding w/ amount')"
Status: accepted

## 2026-05-29 04:10, Decision 18, Live scan + rescore + snapshot rebuild (skip newsletter email)

Stage: divergence
Tried:
  - Run scripts/run_econsignals.sh as-is — rejected: it sends the newsletter via live SMTP (GMAIL_APP_PASSWORD / RESEND_API_KEY / ECONSIGNALS_EMAIL_TO all set), but the user asked for scan + rebuild, not a send
  - Run sensors + score_all_papers(days=3650) + econsignals-snapshot directly, no email (kept)
Kept: /tmp/live_scan_rebuild.sh runs all 15 sensors in parallel against the live data/econsignals.db, rescores every paper, rebuilds webapp/public/feed.json. ECONSIGNALS_RATIONALE unset so the rebuild makes no paid LLM calls.
Dropped: newsletter email send (outward-facing, not authorized by "scan + rebuild"; user can /brief separately)
Files: data/econsignals.db (additive ingest), webapp/public/feed.json (regenerated)
Assumptions (the part that can be wrong):
  - Sending email is outward-facing and was not authorized by "scan + rebuild", so skipping it is correct
  - New sensors degrade gracefully on flaky endpoints (arxiv 429) — logged, run continues
Verify: cd /Users/sankalpsharma/econsignals && python3 -c "import sqlite3;[print(r) for r in sqlite3.connect('data/econsignals.db').execute('SELECT sensor,status,items_found FROM sensor_runs WHERE sensor IN (\"repec_nep\",\"nber\",\"arxiv\",\"worldbank\",\"semantic_scholar\") ORDER BY id DESC LIMIT 5')]"
Status: accepted

---

## 2026-05-29 03:50, Decision 17, Harden rationale gate (explicit flag + certifi SSL) and centrally register the new sensors

Stage: code-choice
Tried:
  - Gate rationale on ANTHROPIC_API_KEY presence alone (Decision 14) — rejected after integration: a globally-set key (the user has one) made the "default-off" feature fire 11 live calls during the first snapshot build
  - Require an explicit ECONSIGNALS_RATIONALE flag IN ADDITION to the key (kept)
Kept: rationale._enabled_api_key() requires ECONSIGNALS_RATIONALE in {1,true,yes,on} AND a key; added a certifi SSL context (the call was failing CERTIFICATE_VERIFY_FAILED on macOS). Registered the 5 new sensors in scripts/run_econsignals.sh, snapshot._SENSORS, CLAUDE.md /scan table, and _PRESTIGE_SOURCES (arxiv=0.45). Isolated _scrape_grants_gov in the pre-existing funding regression test so it stays hermetic.
Dropped: key-presence-only gating (unsafe default on a machine with a global key)
Files: econsignals/lib/rationale.py, econsignals/lib/relevance.py, econsignals/lib/snapshot.py, scripts/run_econsignals.sh, CLAUDE.md, tests/test_sensor_regressions.py, .gitignore, requirements.txt
Assumptions (the part that can be wrong):
  - A bare ANTHROPIC_API_KEY in the environment should never incur spend without an explicit per-feature opt-in
Verify: cd /Users/sankalpsharma/econsignals && PYTHONPATH=. python -m pytest tests/ -q  (239 passed); with ECONSIGNALS_RATIONALE unset, a snapshot build makes 0 Anthropic calls
Status: accepted

---

## 2026-05-29 03:30, Decision 16, Combine feed signals in per-batch percentile space; wire novelty/rationale/SPECTER2 into the snapshot ranking path

Stage: code-choice
Tried:
  - Equal-weight mean of per-batch percentile ranks across relevance, Zotero similarity, learned_ranker (KEPT) — no single channel dominates and daily cohorts are comparable
  - Keeping the raw 0.45 zotero / 0.55 relevance weighted sum (DROPPED) — raw-score sum lets one channel's scale dominate; not cohort-comparable
  - Harvesting yesterday's feed.json as a "previously surfaced" suppression store (DROPPED) — no durable shown-store exists in the DB (only first_seen_at = ingest time); harvesting the feed would empty the dashboard and break default behavior
Kept: new pure helper relevance.combine_percentile_ranks(channels) returns equal-weight mean-percentile per item; snapshot._personalize sets _final from it; novelty.collapse_duplicates + suppress_seen run on BUILT dicts over the full pool BEFORE truncating to _MAX_PAPERS; rationale.why_it_matters attached to top items (None by default, no API key). SPECTER2 consulted only when ECONSIGNALS_EMBED_BACKEND=specter2.
Dropped: previously-surfaced suppression (no store; left to integrator — see register_steps); editing zotero_embeddings.py (SPECTER2 branch lives in snapshot.py, importing its private top-k helpers).
Files: econsignals/lib/relevance.py, econsignals/lib/snapshot.py, econsignals/lenses/newsletter.py, tests/test_snapshot_ranking.py
Assumptions (the part that can be wrong):
  - get_top_papers orders by relevance_score DESC, published_at DESC, so percentile(relevance) under a stable sort reproduces the default order exactly when no optional backend is on (verified: db.py:915)
  - Equal-weight percentile mean is the desired default blend (supersedes _ZOTERO_WEIGHT=0.45); user retunes from here
  - Zotero seen_keys are normalized TITLES only (load_zotero_corpus carries no DOI)
Verify: PYTHONPATH=/Users/sankalpsharma/econsignals python -m pytest tests/test_snapshot_ranking.py -q
Status: accepted

---

## 2026-05-29 02:40, Decision 15, Add Grants.gov as the structured funding deadline source; keep curated registry; no structured conference feed found

Stage: code-choice
Tried:
  - Grants.gov search2 JSON POST as primary structured funding layer (kept) — verified live: returns federal opps with MM/DD/YYYY closeDate
  - NSF awards.json as a funding source — rejected: returns PAST awards (startDate), not future deadlines
  - Matteo Courthoud econ-conference list (named in task) — rejected: does not exist; no econ-conference repo, and his only conference table (awesome-causal-inference/src/conferences.md) is causal-inference, last updated 2024-12, dates mostly year-less, unparseable
  - AEA RFE conference listing — rejected: serves empty HTML to non-browser clients
Kept: funding.py gains parse_grants_gov_hits + _scrape_grants_gov; two targeted queries (NSF SBE CFDA 47.075; "economic development research"), oppStatuses=posted|forecasted. Drops date-less hits and titles failing a research-relevance regex. Additive: curated FUNDING_SOURCES stays primary for PEDL/STEG/IGC/J-PAL/etc.
Dropped: HTML-scraping rewrite (kept as a curated-entry override only); any conference structured feed (curated CONFERENCES registry unchanged, documented in conferences.py docstring)
Files: econsignals/sensors/funding.py, econsignals/sensors/conferences.py, tests/test_deadline_sources.py
Assumptions (the part that can be wrong):
  - search2 body {keyword, rows, oppStatuses, optional cfda} -> {data:{oppHits:[{id,title,agency,closeDate(MM/DD/YYYY),...}]}} (verified live 2026-05-28)
  - closeDate empty == rolling/standing program, correctly dropped (task requires dated records only)
  - NSF CFDA 47.075 hits sharing the code (cyberinfra, instrumentation) are real dated NSF research opps, acceptable to surface ranked by relevance_score
Verify: cd /Users/sankalpsharma/econsignals && PYTHONPATH=. python -m pytest tests/test_deadline_sources.py -q
Status: accepted

## 2026-05-29 02:10, Decision 14, Grounded "why this matters" rationale via flag-gated Haiku, default no-op

Stage: code-choice
Tried:
  - Always-on LLM rationale for every paper — rejected: paid API cost on the free/local default, ongoing per-paper spend
  - Template/string-stitch rationale from matched terms (no LLM) — rejected: reads as boilerplate, no real synthesis of overlap
  - Flag-gated Haiku call, returns None unless ANTHROPIC_API_KEY set, per-paper JSON cache so each paper is paid for once ever (kept)
Kept: econsignals/lib/rationale.py. rationale_for(paper, matched_terms, neighbors) -> str|None. No key => None (no-op). With key: POST https://api.anthropic.com/v1/messages (claude-haiku-4-5) via stdlib urllib; prompt grounds on title+abstract + matched interest keywords (relevance.load_interest_keywords) + Zotero neighbor titles, instructs the model to cite the ACTUAL overlap and emit "" when none (=> None, cached). Cache: data/rationale_cache.json keyed by DOI else source_id else title-hash.
Dropped: anthropic SDK hard dep (use urllib); writing rationale into the DB (caller wires display; module is pure side-effect-to-cache only)
Files: econsignals/lib/rationale.py, tests/test_rationale.py
Assumptions (the part that can be wrong):
  - Messages API contract: header x-api-key + anthropic-version 2023-06-01, body {model,max_tokens,system,messages:[{role:user,content}]}, text at content[0].text (verified against platform.claude.com/docs Messages API 2026-05-29)
  - An empty model reply means "no real overlap" and should surface nothing rather than a hedge
  - Cache key collisions are acceptable: same DOI == same paper
Verify: cd /Users/sankalpsharma/econsignals && PYTHONPATH=. python -m pytest tests/test_rationale.py -q
Status: accepted; gating refined by Decision 17 (now requires an explicit ECONSIGNALS_RATIONALE flag, not key presence alone)

## 2026-05-29 01:30, Decision 13, Orchestrate platform improvements #2-#11 via a tested-package workflow

Stage: plan
Tried:
  - Big-bang parallel agents each editing shared files (relevance/snapshot/newsletter) — rejected: write conflicts, unreviewable
  - Build additive new modules+sensors in parallel (unique paths, self-tested), then disjoint-file integration agents, integrator re-runs full suite behind the #0 gate (kept)
Kept: build phase writes new sensors (#3 nber/arxiv/worldbank/epw, #6 semantic_scholar) and new lib modules (#4 learned_ranker, #5 specter2, #10 novelty, #11 rationale), each self-tested on its own test file; integration phase edits disjoint files (#2 openalex.py, #9 funding/conferences, #7+wiring snapshot/newsletter, #8 web+storage). #5 (SPECTER2) and #11 (LLM rationale) ship flag-gated, default OFF, to preserve the free/local default.
Dropped: worktree isolation (uncommitted #0/#1 invisible from HEAD; merging 10 overlapping worktrees infeasible)
Files: (planning) econsignals/sensors/*, econsignals/lib/*, econsignals/lenses/newsletter.py, econsignals/web/app.py, webapp/src/lib/storage.ts
Assumptions (the part that can be wrong):
  - Agents writing distinct new paths concurrently do not conflict; integration agents touch disjoint files
  - Priority is curated breadth + a working feedback loop, NOT a scorer rewrite (Decision 12: live top-20 already 20/20 on-topic)
Verify: python -m pytest tests/ -q  (must stay green after each integrated package)
Status: accepted

## 2026-05-29 01:00, Decision 12, Add relevance acceptance gate (#0) and RePEc NEP curated-feed sensor (#1)

Stage: code-choice
Tried:
  - Live-DB-only acceptance test (the audit spec) — rejected as the committed gate (depends on a gitignored 1061-row DB and would mutate it to score)
  - Hermetic labelled-corpus gate + a read-only live diagnostic that skips when the DB is absent (kept)
  - For new sources: broaden OpenAlex further (rejected, adds noise) vs add NEP human-curated weekly reports (kept)
Kept: tests/test_relevance_ranking.py (equal-venue topical separation + named-junk exclusion + India-gate, plus a read-only live top-20 on-topic check). econsignals/sensors/nep.py (RePEc NEP RSS 1.0 sensor; codes nep-dev/uep/lab/pbe/agr/geo; name 'repec_nep' inherits prestige 0.50; defusedxml-hardened parse).
Dropped: nep-ure (404 — the correct urban code is nep-uep); nep-ind (= Industrial Organization, not India)
Files: tests/test_relevance_ranking.py (new), econsignals/sensors/nep.py (new)
Assumptions (the part that can be wrong):
  - NEP feed URL pattern https://nep.repec.org/rss/nep-<code>.rss.xml is stable (verified live: 6 feeds 200 OK)
  - The pre-existing "feed feels off" judgement predates Decision 11: corrected live top-20 is now 20/20 on-topic, so the gate passes on the current corpus
Verify: python -m pytest tests/test_relevance_ranking.py -v  (4 passed); python -m econsignals.sensors.nep  (ingests ~100 curated papers, 0 errors)
Status: accepted

## 2026-05-28 21:30, Decision 11, Rank feed by venue quality + Zotero similarity (user said feed was irrelevant)

Stage: divergence
Tried:
  - India-floor-dominant scoring (Decision 6): surfaced low-tier regional-journal India papers (health-insurance KAP, financial-inclusion surveys)
  - Prestige-dominant + Zotero-library similarity (kept)
Kept: (1) Fixed inverted prestige — crossref (curated elite-journal TOCs) + NBER high, openalex (uncurated Economics field) low unless a known journal is detected; prestige is now the dominant weight (0.35). (2) Dropped the +0.30 India floor; India amplifies topical fit by 0.05 only. (3) Dashboard ranks by a blend of venue quality and similarity to the user's Zotero library (top-k embedding); Zotero demotes off-topic work (it saturates ~7.0 for econ, ~2.5 off-topic), prestige ranks the on-topic frontier.
Dropped: India-as-quality-gate (floated junk); min-max Zotero normalization (collapsed on a homogeneous pool -> fixed scale 3.0-7.0)
Files: econsignals/lib/relevance.py (weights, prestige table, journals, India), econsignals/lib/snapshot.py (_personalize)
Assumptions (can be wrong):
  - User reads elite-journal / NBER frontier work, not uncurated regional journals (confirmed by Zotero: urban/land/housing/migration)
  - crossref ISSN list = elite journals, so source=crossref is a reliable quality proxy
Verify: python -m econsignals.lib.relevance && python -m econsignals.lib.snapshot; top-22 of feed.json are crossref frontier econ (migration/firms/urban/housing), no regional-journal KAP papers
Status: accepted

## 2026-05-28 20:30, Decision 10, Publish site via separate public repo; keep code private

Stage: divergence
Tried:
  - Make econsignals repo public + in-repo Pages Action (exact URL, simplest, but exposes code+history)
  - Keep code private, publish built assets to a public sankalpsharmaa.github.io user-site repo (user's choice, kept)
Kept: code repo stays private; scripts/deploy_site.sh builds snapshot+app and pushes webapp/dist into sankalpsharmaa.github.io/econsignals/. Removed the in-repo Pages workflow (wrong mechanism, would fail on a private repo).
Dropped: in-repo GitHub Pages Action (.github/workflows/deploy.yml deleted)
Files: scripts/deploy_site.sh (new), README.md, removed .github/workflows/deploy.yml
Assumptions (can be wrong):
  - Publishing the personalized feed.json (rankings, social handles) is acceptable to the user (confirmed: chose "publish site only")
  - User-site repo Pages auto-enables and serves /econsignals/ subpath
Verify: curl -s -o /dev/null -w '%{http_code}' https://sankalpsharmaa.github.io/econsignals/ -> 200; feed.json -> 200 (verified live)
Status: reversed by Decision 20

## 2026-05-28 19:30, Decision 9, OpenAlex collects broadly; India is a ranking preference, not a collection filter

Stage: code-choice
Tried:
  - Keep hard South-Asia country gate (Decision 8): tool can never surface a relevant non-India dev/urban paper
  - Field-only gate + optional opt-in country env var (kept)
Kept: primary_topic.field.id:fields/20 only; country gate added solely when ECONSIGNALS_OPENALEX_COUNTRIES is set. Discriminating test confirmed the field gate alone keeps out the crypto/coffee/CompSci noise (their primary field is not Economics), so the country gate was over-correction. India-first ordering comes from the relevance India floor, not from dropping non-India papers at collection.
Dropped: collection-wide country gate (architecture error: conflated ranking preference with collection scope; the user asked to monitor econ broadly)
Files: econsignals/sensors/openalex.py
Assumptions: field gate is sufficient noise control; residual AI-spam papers sink via low relevance
Verify: curl OpenAlex with primary_topic.field.id:fields/20 (no country) -> econ titles, no crypto/coffee/CS; top-20 stays India-dominated via ranking
Status: accepted

## 2026-05-28 18:30, Decision 8, OpenAlex hard South-Asia country filter (precision over breadth)

Stage: divergence
Tried:
  - Field-only filter (primary_topic.field.id:fields/20), let the scorer rank breadth
  - Field + hard country gate authorships.countries:IN|BD|PK|LK|NP (sensor agent's choice, kept)
Kept: field + country gate. Pulls only South-Asia-authored economics (104 fresh papers, 1742 available); precision fix for the crypto/coffee/telemedicine pollution.
Dropped: field-only (re-admits global off-topic papers the broad query caused)
Files: econsignals/sensors/openalex.py
Assumptions (can be wrong):
  - OpenAlex should be the India/SA-precision source; crossref (journals) + iza/bread cover global breadth
  - User wants India-authored work surfaced; a brilliant non-SA urban paper from OpenAlex is now missed (mitigated: crossref/iza still global)
Verify: OPENALEX_EMAIL=... python -m econsignals.sensors.openalex -> stderr "collected N papers"; top-20 now India-dominated
Status: reversed by Decision 9

## 2026-05-28 18:00, Decision 7, Dashboard denoise at presentation layer, not by deleting rows

Stage: code-choice
Tried: delete junk social/deadline rows from DB vs filter at snapshot/newsletter
Kept: snapshot.py filters (econ-only social via handle/DOI/core-topic; deadlines dated-or-curated; drop Exa AI image-captions; decode HTML entities). Reversible, preserves the audit trail; sensor fixes prevent future junk.
Dropped: hard-deleting rows (irreversible; social_items not in the soft-delete-only set but filtering is safer)
Files: econsignals/lib/snapshot.py
Assumptions: filtering at read time is enough for a clean dashboard; raw rows kept for re-processing
Verify: python -m econsignals.lib.snapshot -> feed.json has 9-12 real funder deadlines, 33 econ social, 0 captions
Status: accepted

## 2026-05-28 16:30, Decision 6, Relevance scoring overhaul (India-primary tiered scorer)

Stage: code-choice
Tried:
  - Keep linear weighted-sum, only fix author-noise (insufficient: generic health/edu papers still topped)
  - Learned/feedback ranker (too large; feedback capture not wired)
  - Tiered keyword scorer + strong India floor + multiplicative amplification (kept)
Kept: _W_JEL .22, _W_KW .33, _W_AUTHOR .08, _W_PRESTIGE .10, _W_RECENCY .07, _W_SOCIAL .05; auto_discovered authors score 0 unless a tracked author exists; JEL rides on keyword evidence when JEL codes absent (96% of corpus); core topics (urban/land/India-structural) count full, secondary (health/edu/inequality) 0.25; India = +0.30 floor + 0.15*topical; future dates -> recency 0.5 not 1.0.
Dropped: flat additive India boost (lifted crypto/coffee-in-India); JEL=0 for missing codes (zeroed 96% of papers); author 0.7 tier (every OpenAlex paper carries auto-discovered authors -> pure noise with 0 tracked).
Files: econsignals/lib/relevance.py, econsignals/lib/normalize.py (accent transliteration)
Assumptions (the part that can be wrong):
  - India-primary researcher wants India papers near the top even over strong non-India work (per profile "India (primary)")
  - Core vs secondary topic split reflects the user's distinctive interests; user can retune via feedback
Verify: sqlite3 data/econsignals.db "SELECT relevance_score,title FROM papers ORDER BY relevance_score DESC LIMIT 30" -> 25/30 India-or-core; python -m pytest tests/ -q -> 103 passed
Status: accepted

## 2026-05-28 15:00, Decision 5, db.py schema migration to collapse duplicates

Stage: merge
Tried:
  - affiliation NOT NULL DEFAULT '' keeping (name_normalized, affiliation) key (would not merge NULL-vs-realaffil dups, e.g. Tyskø)
  - UNIQUE(name_normalized) only, affiliation as mergeable attribute (kept)
Kept: dedup-first migration (guarded by PRAGMA user_version): collapse 6046->1208 authors by name_normalized, repoint paper_authors with UPDATE OR IGNORE, preserve longest affiliation, add UNIQUE INDEX; rolling deadlines store '' sentinel (441->101).
Dropped: rewriting the authors table (heavyweight); collapsing before dedup (hit legacy composite UNIQUE)
Files: econsignals/lib/db.py
Assumptions:
  - One author = one identity regardless of per-source affiliation variation
  - Losing a duplicate's openalex_id/orcid is immaterial (auto_discovered authors, now neutralized in scoring)
Verify: sqlite3 data/econsignals.db "SELECT COUNT(*),COUNT(DISTINCT name_normalized) FROM authors" -> 1208,1208; re-run `python -m econsignals.lib.db` is a no-op (user_version=1)
Status: accepted

## 2026-05-28 14:00, Decision 4, Deploy mechanics deferred but exposure-cleared

Stage: plan
Tried:
  - (a) Make existing `econsignals` repo public + Pages via Actions (exact URL, simplest)
  - (b) Keep code repo private, publish built assets to a new public `sankalpsharmaa.github.io` user-site repo under /econsignals (exact URL, code stays private)
Kept: decision deferred to Phase 4; exposure scan run now to clear the path
Dropped: nothing yet
Files: (none)
Assumptions (the part that can be wrong):
  - The only sensitive published artifact is the JSON snapshot (personalized rankings, social handles); code repo has no secrets/data
Verify: `git ls-files | grep -iE 'econsignals\.db|\.env|\.pkl'` returns no data files; `cat .gitignore` shows data/reports ignored
Status: accepted

## 2026-05-28 13:55, Decision 3, Reconcile "interactive web app" with GitHub Pages

Stage: code-choice
Tried:
  - Static dashboard generated from DB (works on Pages, less interactive)
  - Interactive FastAPI+React app (user's choice, but Pages cannot run Python)
  - Hybrid: React/Vite SPA with live-API + static-snapshot fallback
Kept: Hybrid. SPA is the public deliverable (client-side search/filter/sort/star via localStorage, fed by a baked JSON snapshot). FastAPI is a local power-layer built AFTER the SPA so it cannot block the deploy.
Dropped: Pure static (user explicitly wanted interactive); backend-first (Pages can't host it, and it would block the critical path)
Files: webapp/ (SPA), econsignals/web/ (FastAPI), data snapshot builder
Assumptions:
  - One-user tool: client-side interactivity satisfies "interactive web app" for the public surface
  - vite build with base=/econsignals/ produces Pages-deployable assets
Verify: after build, `vite build` emits to webapp/dist/ and the site loads + filters with no backend running
Status: accepted

## 2026-05-28 13:50, Decision 2, Relevance + denoise is the primary usability fix

Stage: plan
Tried: treating the request as a frontend/UX job vs. a data-quality job
Kept: data-quality first. The tool was abandoned because it ranks a Norwegian-municipality paper #1 (0.34) and fills the social feed with wikihow/earthquake bots and the deadline list with Twitter AI image-captions. A polished UI over broken data advertises the breakage.
Dropped: frontend-first sequencing
Files: econsignals/lib/relevance.py, dedup.py, sensors/{mastodon,bluesky,twitter_bridge,_exa}.py
Assumptions:
  - Reweighting relevance + adding econ/India filters will lift relevant papers into the top ranks
Verify: after fix, `sqlite3 data/econsignals.db "SELECT title,relevance_score FROM papers ORDER BY relevance_score DESC LIMIT 20"` skews India/dev/urban/applied-micro
Status: accepted

## 2026-05-28 13:45, Decision 1, Revitalize EconSignals via 15-agent audit then build

Stage: plan
Tried: single-pass manual fix vs. parallel multi-agent audit then targeted implementation
Kept: Phase 1 read-only 15-agent audit (fix-ready findings) -> Phase 2 triage -> Phase 3 implement (data fixes + SPA + FastAPI + README + Pages) -> Phase 4 deploy
Dropped: blind 15-agent parallel mutation (file conflicts; unsafe)
Files: docs/decisions.md, tasks/todo.md, then broad
Assumptions:
  - The dormant tool's core logic is sound enough to fix rather than rewrite
Verify: `python -m pytest tests/ -v` passes after changes; site builds; top-20 papers relevant
Status: accepted
