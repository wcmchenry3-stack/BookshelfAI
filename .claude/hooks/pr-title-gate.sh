#!/usr/bin/env bash
# pr-title-gate.sh — PreToolUse hook for Claude Code
# Matchers: Bash (`gh pr create`) and mcp__github__create_pull_request.
# Blocks a non-Conventional PR title and any base other than `dev`
# (`main` is allowed only when the head is `dev` or `release-please--*`).
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/output.sh
source "$HOOK_DIR/lib/output.sh"
# shellcheck source=lib/conventional.sh
source "$HOOK_DIR/lib/conventional.sh"

hook_read_input
COMMAND=$(hook_jq '.tool_input.command // empty')
TOOL=$(hook_jq '.tool_name // empty')

# check_pr <title> <base> <head> — blocks (exit 2) on the first problem found.
check_pr() {
  local TITLE="$1" BASE="$2" HEAD="$3" PROBLEMS="" HEAD_BRANCH ERR

  # ── Base branch ────────────────────────────────────────────────
  if [ -n "$BASE" ]; then
    HEAD_BRANCH="${HEAD#*:}" # strip optional "owner:" prefix
    case "$BASE" in
      dev) ;;
      main)
        # gh's default head is the current branch at run time, which an
        # earlier `cd`/`git switch` in the same command line can change, so
        # a PR into main must name its head.
        case "$HEAD_BRANCH" in
          "") PROBLEMS+="  - base 'main': a PR into main must name its head explicitly (--head dev or --head release-please--...)\n" ;;
          dev | release-please--*) ;;
          *) PROBLEMS+="  - base 'main' is only allowed from 'dev' or 'release-please--*' (head: '${HEAD_BRANCH:-unknown}'); use --base dev\n" ;;
        esac
        ;;
      *) PROBLEMS+="  - base '$BASE' is not allowed; PRs must target 'dev'\n" ;;
    esac
  fi

  # ── Title ──────────────────────────────────────────────────────
  # Unevaluated shell substitutions: cannot be judged, skip the title check.
  case "$TITLE" in *'$('* | *'`'*) TITLE="" ;; esac
  if [ -n "$TITLE" ]; then
    if ! ERR=$(conventional_header_error "$TITLE"); then
      PROBLEMS+="  - title \"$TITLE\": $ERR\n"
    fi
  fi

  if [ -n "$PROBLEMS" ]; then
    hook_block "PR title/base rejected before it reaches GitHub." \
      "$PROBLEMS\nTitle must be 'type(scope): subject' (types: $CC_TYPES); base must be 'dev'."
  fi
}

if [ "$TOOL" = "mcp__github__create_pull_request" ]; then
  check_pr "$(hook_jq '.tool_input.title // empty')" "$(hook_jq '.tool_input.base // empty')" "$(hook_jq '.tool_input.head // empty')"
elif [ -n "$COMMAND" ]; then
  printf '%s' "$COMMAND" | grep -Eq 'pr[[:space:]](.*[[:space:]])?(create|new)' || hook_allow
  command -v python3 &>/dev/null || hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
  PARSED=$(printf '%s' "$COMMAND" | python3 "$HOOK_DIR/lib/parse_cmd.py" pr 2>/dev/null) || hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
  if [ "$(printf '%s' "$PARSED" | jq -r '.unparseable // false')" = "true" ]; then
    hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
  fi
  [ "$(printf '%s' "$PARSED" | jq -r '.match // false')" = "true" ] || hook_allow
  # Every `gh pr create` in the command line is checked; first failure blocks.
  while IFS= read -r ITEM; do
    # Without --base, gh picks gh-merge-base config or the repo default: unknowable here.
    if [ -z "$(printf '%s' "$ITEM" | jq -r '.base // empty')" ]; then
      hook_block "pass the PR base explicitly (gh pr create --base dev ...); without --base gh may target another branch"
    fi
    check_pr "$(printf '%s' "$ITEM" | jq -r '.title // empty')" \
      "$(printf '%s' "$ITEM" | jq -r '.base // empty')" \
      "$(printf '%s' "$ITEM" | jq -r '.head // empty')"
  done < <(printf '%s' "$PARSED" | jq -c '.items[]')
fi
hook_allow
