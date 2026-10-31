#!/usr/bin/env bash
# commit-gate.sh — PreToolUse hook (matcher: Bash) for Claude Code
# Blocks `git commit` when the current branch is dev/main or when the commit
# message is not a Conventional Commit (mirrors the CI commitlint rules).
# Strict isolation: a commit may share its command only with git add/status/
# diff/log (enforced by lib/parse_cmd.py), so the repo and branch are exact.
# If a message cannot be determined (editor, -C, unparseable) it is allowed.
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/output.sh
source "$HOOK_DIR/lib/output.sh"
# shellcheck source=lib/conventional.sh
source "$HOOK_DIR/lib/conventional.sh"

hook_read_input
COMMAND=$(hook_jq '.tool_input.command // empty')
[ -z "$COMMAND" ] && hook_allow
case "$COMMAND" in *commit*) ;; *) hook_allow ;; esac

command -v python3 &>/dev/null || hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
SESSION_CWD=$(hook_jq '.cwd // empty')
[ -z "$SESSION_CWD" ] || [ ! -d "$SESSION_CWD" ] && SESSION_CWD=$(pwd)

PARSED=$(printf '%s' "$COMMAND" | python3 "$HOOK_DIR/lib/parse_cmd.py" commit "$SESSION_CWD" 2>/dev/null) || hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
if [ "$(printf '%s' "$PARSED" | jq -r '.unparseable // false')" = "true" ]; then
  hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
fi
[ "$(printf '%s' "$PARSED" | jq -r '.match // false')" = "true" ] || hook_allow

if [ "$(printf '%s' "$PARSED" | jq -r '.violation')" = "true" ]; then
  hook_block "run git commit / git push on its own (only git add/status/diff/log may share the command), so the gate checks the exact repo and branch"
fi

ITEM=$(printf '%s' "$PARSED" | jq -c '.items[0]')
# ── Branch protection ────────────────────────────────────────────
# Effective directory honours `git -C <dir> commit`.
CWD="$SESSION_CWD"
DIR=$(printf '%s' "$ITEM" | jq -r '.dir // empty')
[ -n "$DIR" ] && [ -d "$DIR" ] && CWD="$DIR"
BRANCH=$(git -C "$CWD" symbolic-ref --short -q HEAD 2>/dev/null || true)
case "$BRANCH" in
  dev | main)
    hook_block "Commits directly on '$BRANCH' are not allowed." \
      "Create a feature/<name> or bug/<name> branch from origin/dev first."
    ;;
esac

# ── Conventional Commits ─────────────────────────────────────────
MESSAGE=$(printf '%s' "$ITEM" | jq -r '.message // empty')
[ -z "$MESSAGE" ] && hook_allow
# Message built from shell substitutions we cannot evaluate — don't guess.
case "$MESSAGE" in *'$('* | *'`'*) hook_allow ;; esac

HEADER=$(printf '%s\n' "$MESSAGE" | sed -e '/^[[:space:]]*$/d' | head -n 1)
if ! ERR=$(conventional_header_error "$HEADER"); then
  hook_block "Commit message is not a valid Conventional Commit: $ERR" \
    "Header: $HEADER\nExpected: type(scope): subject  — types: $CC_TYPES; header <= $CC_HEADER_MAX chars."
fi
hook_allow
