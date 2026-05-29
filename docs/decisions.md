# EconSignals Decisions Log

Append-only audit trail. Newest entries at top. See `~/claude-config/rules/decisions-log.md`.

---

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
