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

usage() {
  echo "usage: $0 [http(s)://host ...]   (SMOKE_PATH must start with /)" >&2
  exit 2
}

case "$PATH_TO_CHECK" in /*) ;; *) usage ;; esac
for base in "${URLS[@]}"; do
  [[ "$base" =~ ^https?:// ]] || usage
done

rc=0
for base in "${URLS[@]}"; do
  # Never print URL userinfo (user:pass@).
  shown=$(printf '%s' "${base%/}" | sed -E 's#^(https?://)[^/@]*@#\1#')
  code=$(curl -sS -o /dev/null -w '%{http_code}' --proto '=http,https' --connect-timeout 10 --max-time 20 -- "${base%/}${PATH_TO_CHECK}") || code="000"
  case "$code" in
    200) echo "OK   ${shown}${PATH_TO_CHECK} -> 200" ;;
    403) echo "FAIL ${shown}${PATH_TO_CHECK} -> 403: origin lockdown rejects edge traffic (edge not sending the origin header, or wrong value)"; rc=1 ;;
    *)   echo "FAIL ${shown}${PATH_TO_CHECK} -> ${code}: unexpected status"; rc=1 ;;
  esac
done
exit $rc
