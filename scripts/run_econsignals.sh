#!/usr/bin/env bash
set -uo pipefail

ROOT="${ECONSIGNALS_ROOT:-/Users/sankalpsharma/econsignals}"

if [[ ! -d "$ROOT" ]]; then
  echo "[econsignals] repo not found at: $ROOT" >&2
  exit 1
fi

cd "$ROOT" || exit 1
echo "[econsignals] root=$ROOT"

# Load .env if present
if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

python3 -m econsignals.lib.db || exit 1

run_sensor() {
  local sensor="$1"
  echo "[econsignals] sensor=$sensor"
  if python3 -m econsignals.sensors.${sensor} 2>&1; then
    echo "[econsignals] sensor=$sensor ok"
  else
    echo "[econsignals] sensor=$sensor failed (continuing)" >&2
  fi
}

echo "[econsignals] running all sensors in parallel..."

# Run all sensors concurrently
run_sensor openalex &
run_sensor crossref &
run_sensor iza &
run_sensor bread &
run_sensor imf &
run_sensor funding &
run_sensor conferences &
run_sensor mastodon &
run_sensor bluesky &
run_sensor twitter_bridge &

# Wait for all to finish
wait
echo "[econsignals] all sensors done"

# Score papers
python3 -c "
from econsignals.lib.relevance import score_all_papers
print('[econsignals] scored', score_all_papers(days=30), 'papers')
" || exit 1

# Generate newsletter (and email if SMTP configured)
python3 -m econsignals.lenses.newsletter --days 1 || exit 1

# Also generate deadline alerts file
python3 -m econsignals.lenses.deadline_alert || exit 1

TODAY="$(date +%Y-%m-%d)"
echo "[econsignals] done — reports/$TODAY"
ls -1 "$ROOT/reports/$TODAY" 2>/dev/null || true
