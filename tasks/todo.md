# EconSignals Revitalization — Task Plan

Goal: make the dormant tool drastically more usable, fix data quality, ship an interactive
dashboard to sankalpsharmaa.github.io/econsignals. Decisions logged in `docs/decisions.md`.

## Phase 1 — Audit (15 parallel agents) — IN PROGRESS
- [ ] Fix-ready findings for every component + comparable-tool research

## Phase 2 — Triage
- [ ] Synthesize findings into a prioritized fix list (critical data-quality first)
- [ ] Update decisions log + this plan

## Phase 3 — Implement (auto-fix high-confidence)
- [x] Data-quality: relevance reweighting (top-30 now 25/30 India-or-core)
- [x] Data-quality: dedup duplicate-author bug (db migration 6046->1208)
- [x] Data-quality: social feed econ-relevance gate (snapshot filter)
- [x] Data-quality: deadline/paper Twitter-junk filter (snapshot filter)
- [x] Resend email double-wrap bug (suite green, 103 passed)
- [~] Sensor fixes (8 agents, background) — improves future ingestion
- [x] Snapshot builder (econsignals/lib/snapshot.py -> feed.json)
- [~] React/Vite SPA dashboard (frontend agent, background)
- [x] FastAPI backend (econsignals/web/app.py; /api/feed wired, feedback loop)
- [x] Real README + pyproject editable install + console scripts
- [ ] Redesigned email newsletter (data fixed; cosmetics later)
- [x] GitHub Action: build + deploy to Pages
- [~] Core regression tests (relevance/denoise)

## Phase 4 — Deploy ✓
- [x] Integrate sensor fixes (110 tests green) + frontend; refresh data (1025 papers); rebuild snapshot
- [x] Decision: keep code private, publish site to separate public repo
- [x] Verify: 110 tests green; clean npm ci build; interactions work (search/India/star persist); LIVE at https://sankalpsharmaa.github.io/econsignals/

## Review
- Live dashboard: https://sankalpsharmaa.github.io/econsignals/ (deploy: `bash scripts/deploy_site.sh`)
- Code committed locally on branch `revitalize-dashboard` (NOT pushed — code repo stays private). Push/merge when ready.
- Remaining/optional: redesign email newsletter cosmetics; broaden curated funder calendar; add IZA/BREAD markup canary tests; wire dashboard 👍/👎 export back into jel_weights as a batch.

## Review
(to be filled after completion)
