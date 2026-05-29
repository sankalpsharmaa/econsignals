#!/usr/bin/env bash
#
# Deploy the EconSignals dashboard to GitHub Pages while keeping this code
# repository private. Builds the data snapshot + the Vite app, then publishes
# the static output to the PUBLIC user-site repo under /econsignals/ so it
# serves at https://sankalpsharmaa.github.io/econsignals/ .
#
# Usage:  bash scripts/deploy_site.sh
# Requires: gh (authenticated), node/npm, python3.

set -euo pipefail

SITE_REPO="sankalpsharmaa/sankalpsharmaa.github.io"
SUBDIR="econsignals"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$REPO_ROOT"

# 1. Build fresh data snapshot + production bundle.
echo "[deploy] building snapshot + app..."
python3 -m econsignals.lib.snapshot
( cd webapp && npm run build )

# 2. Ensure the public site repo exists, then clone it.
if ! gh repo view "$SITE_REPO" >/dev/null 2>&1; then
    echo "[deploy] creating public repo $SITE_REPO ..."
    gh repo create "$SITE_REPO" --public \
        --description "Static sites served from sankalpsharmaa.github.io" >/dev/null
fi
gh repo clone "$SITE_REPO" "$WORK/site" -- --quiet 2>/dev/null || \
    git clone "https://github.com/$SITE_REPO.git" "$WORK/site"

# 3. Replace the /econsignals/ subtree with the fresh build.
rm -rf "${WORK:?}/site/$SUBDIR"
mkdir -p "$WORK/site/$SUBDIR"
cp -R webapp/dist/. "$WORK/site/$SUBDIR/"
touch "$WORK/site/.nojekyll"   # skip Jekyll; serve assets/ verbatim

# 4. Commit and push (no-op if nothing changed).
cd "$WORK/site"
git add -A
if git diff --cached --quiet; then
    echo "[deploy] no changes to publish."
else
    git commit -q -m "Deploy EconSignals dashboard"
    git branch -M main
    git push -u origin main
fi

echo "[deploy] done -> https://sankalpsharmaa.github.io/$SUBDIR/"
