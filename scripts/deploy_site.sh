#!/usr/bin/env bash
#
# Deploy the EconSignals dashboard to GitHub Pages, served directly from THIS
# (private) repository's `gh-pages` branch. Requires GitHub Pro, which allows
# Pages on a private repo so the code never goes public. Builds the data
# snapshot + the Vite app, then force-pushes the static output to `gh-pages`,
# where GitHub Pages serves it as a project site at
# https://sankalpsharmaa.github.io/econsignals/ .
#
# Usage:  bash scripts/deploy_site.sh
# Requires: gh (authenticated), node/npm, python3.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$REPO_ROOT"

# 1. Build fresh data snapshot + production bundle (base stays /econsignals/).
echo "[deploy] building snapshot + app..."
python3 -m econsignals.lib.snapshot
( cd webapp && npm run build )

# 2. Assemble the publish tree: the built site at the branch root + .nojekyll.
cp -R webapp/dist/. "$WORK/"
touch "$WORK/.nojekyll"   # skip Jekyll; serve assets/ verbatim

# 3. Force-push it as a one-commit orphan to gh-pages on this repo's origin.
#    Locally the gh credential helper authenticates the push. On a CI runner
#    that helper is absent, so embed the provided token in the push URL.
ORIGIN="$(git remote get-url origin)"
if [[ -n "${GH_PAGES_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" ]]; then
    ORIGIN="https://x-access-token:${GH_PAGES_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
fi
cd "$WORK"
git init -q
git add -A
git -c user.name="econsignals-deploy" -c user.email="deploy@econsignals.local" \
    commit -q -m "Deploy EconSignals dashboard"
git push -q --force "$ORIGIN" HEAD:gh-pages

echo "[deploy] done -> https://sankalpsharmaa.github.io/econsignals/"
