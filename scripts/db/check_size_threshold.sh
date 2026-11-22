#!/usr/bin/env bash
# Database size alert (#673, G8): exit 1 when the database size is at or above
# a percentage of its limit. Used by the `size-check` job of
# .github/workflows/db-backup.yml; see docs/backups.md and docs/RENDER.md.
#
# Usage: scripts/db/check_size_threshold.sh <size_bytes> <limit_gb> [threshold_pct]
#   size_bytes     output of `SELECT pg_database_size(current_database())`
#   limit_gb       the plan limit in GB (GB = 1024^3 bytes)
#   threshold_pct  integer 1-100, default 70
# Exit: 0 below the threshold, 1 at or above it, 2 bad arguments.
# The repository is public and so are its job logs: no byte size or percentage
# is printed, only the verdict.
set -u

size="${1:-}"
limit_gb="${2:-}"
pct="${3:-70}"

is_uint() { case "$1" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }

if ! is_uint "$size" || ! is_uint "$limit_gb" || ! is_uint "$pct" \
  || [ "$limit_gb" -lt 1 ] || [ "$pct" -lt 1 ] || [ "$pct" -gt 100 ]; then
  echo "usage: $0 <size_bytes> <limit_gb> [threshold_pct 1-100]" >&2
  exit 2
fi

limit_bytes=$((limit_gb * 1024 * 1024 * 1024))
# size >= limit * pct / 100, in integer arithmetic (no rounding).
if [ $((size * 100)) -ge $((limit_bytes * pct)) ]; then
  echo "Database size is at or above ${pct}% of its limit."
  exit 1
fi
echo "Database size is below ${pct}% of its limit."
exit 0
