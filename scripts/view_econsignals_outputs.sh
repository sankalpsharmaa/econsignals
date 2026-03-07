#!/usr/bin/env bash
set -uo pipefail

ROOT="${ECONSIGNALS_ROOT:-/Users/sankalpsharma/econsignals}"
TARGET_DATE="${1:-}"

if [[ ! -d "$ROOT/reports" ]]; then
  echo "[econsignals-view] reports directory not found: $ROOT/reports" >&2
  exit 1
fi

if [[ -n "$TARGET_DATE" ]]; then
  REPORT_DIR="$ROOT/reports/$TARGET_DATE"
else
  REPORT_DIR="$(ls -1d "$ROOT"/reports/* 2>/dev/null | sort | tail -1)"
fi

if [[ -z "$REPORT_DIR" || ! -d "$REPORT_DIR" ]]; then
  echo "[econsignals-view] no report directory found" >&2
  exit 1
fi

echo "[econsignals-view] report_dir=$REPORT_DIR"
echo

echo "== Files =="
ls -1 "$REPORT_DIR"
echo

show_file() {
  local file="$1"
  local lines="${2:-120}"
  if [[ -f "$file" ]]; then
    echo "== $(basename "$file") =="
    sed -n "1,${lines}p" "$file"
    echo
  fi
}

show_file "$REPORT_DIR/newsletter.txt" 80
show_file "$REPORT_DIR/daily_brief.md" 140
show_file "$REPORT_DIR/deadline_alerts.md" 80

latest_rag_md="$(ls -1 "$REPORT_DIR"/zotero_rag_*.md 2>/dev/null | sort | tail -1)"

if [[ -n "$latest_rag_md" ]]; then
  show_file "$latest_rag_md" 180
fi
