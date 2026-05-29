#!/usr/bin/env bash
#
# Scan every sensor + rescore papers. No email, no newsletter — this is the
# data-pull half of the daily CI refresh (.github/workflows/refresh-data.yml),
# and a handy "just collect, don't email me" run locally. For the full local
# run that also emails the brief, use run_econsignals.sh.

set -uo pipefail

ROOT="${ECONSIGNALS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" || exit 1
echo "[refresh] root=$ROOT"

# Local convenience: load .env if present. CI passes secrets via the job env.
if [[ -f "$ROOT/.env" ]]; then
  set -a; source "$ROOT/.env"; set +a
fi

python3 -m econsignals.lib.db || exit 1

run_sensor() {
  echo "[refresh] sensor=$1"
  if python3 -m econsignals.sensors."$1" 2>&1; then
    echo "[refresh] sensor=$1 ok"
  else
    echo "[refresh] sensor=$1 failed (continuing)" >&2
  fi
}

echo "[refresh] running all sensors in parallel..."
for s in openalex crossref iza bread imf nep nber arxiv_econ worldbank \
         semantic_scholar funding conferences mastodon bluesky twitter_bridge; do
  run_sensor "$s" &
done
wait
echo "[refresh] all sensors done"

python3 -c "from econsignals.lib.relevance import score_all_papers; print('[refresh] scored', score_all_papers(days=30), 'papers')" || exit 1
echo "[refresh] done"
