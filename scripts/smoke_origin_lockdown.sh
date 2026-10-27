#!/usr/bin/env bash
# Smoke check for the origin lockdown (#721): call a NON-exempt, unauthenticated
# route through the PUBLIC hostname (i.e. through the edge) and fail on 403.
#
# /health is exempt from the lockdown, so CI and Render's health check stay
# green even when the edge does not send the origin header. /health/db is not
# exempt: it only returns 200 when the edge adds the header with the right value.
#
# Usage: scripts/smoke_origin_lockdown.sh [base-url ...]
#   default: https://bookshelfapi.buffingchi.com
# Run it from a machine the edge lets through (not GitHub-hosted runners:
# Cloudflare Bot Fight Mode blocks them, see .github/workflows/ci.yml).
# Run it right after setting ORIGIN_AUTH_SECRET on a service, and after deploys.
set -u

PATH_TO_CHECK="${SMOKE_PATH:-/health/db}"
URLS=("$@")
[ ${#URLS[@]} -eq 0 ] && URLS=("https://bookshelfapi.buffingchi.com")

rc=0
for base in "${URLS[@]}"; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "${base%/}${PATH_TO_CHECK}") || code="000"
  case "$code" in
    200) echo "OK   ${base}${PATH_TO_CHECK} -> 200" ;;
    403) echo "FAIL ${base}${PATH_TO_CHECK} -> 403: origin lockdown rejects edge traffic (edge not sending the origin header, or wrong value)"; rc=1 ;;
    *)   echo "FAIL ${base}${PATH_TO_CHECK} -> ${code}: unexpected status"; rc=1 ;;
  esac
done
exit $rc
