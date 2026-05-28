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

## Phase 4 — Deploy
- [ ] Integrate sensor fixes + frontend; refresh data; rebuild snapshot
- [ ] Confirm repo->public mechanics (code public vs assets-only public)
- [ ] Verify: pytest green, vite build, site loads + filters with no backend, live URL

## Review
(to be filled after completion)
