#!/usr/bin/env bash
# precheck-gate.sh — PreToolUse hook (matcher: Bash) for Claude Code
# Before `git push`, runs fast lint/type checks for the packages changed vs
# origin/dev. A pass is cached for 24h keyed by the HEAD tree hash.
# Missing tools => warning, never a block.
#   backend : ruff check + black --check on changed .py
#   frontend: eslint + prettier --check on changed files, `npm run typecheck`
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/output.sh
source "$HOOK_DIR/lib/output.sh"
# shellcheck source=lib/cache.sh
source "$HOOK_DIR/lib/cache.sh"

TTL=86400

hook_read_input
COMMAND=$(hook_jq '.tool_input.command // empty')
[ -z "$COMMAND" ] && hook_allow
case "$COMMAND" in *push*) ;; *) hook_allow ;; esac
command -v python3 &>/dev/null || hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"

CWD=$(hook_jq '.cwd // empty')
[ -z "$CWD" ] || [ ! -d "$CWD" ] && CWD=$(pwd)

PARSED=$(printf '%s' "$COMMAND" | python3 "$HOOK_DIR/lib/parse_cmd.py" push "$CWD" 2>/dev/null) || hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
if [ "$(printf '%s' "$PARSED" | jq -r '.unparseable // false')" = "true" ]; then
  hook_block "couldn't parse this command safely; run git commit / git push on its own, without comments or unusual quoting"
fi
[ "$(printf '%s' "$PARSED" | jq -r '.match // false')" = "true" ] || hook_allow

if [ "$(printf '%s' "$PARSED" | jq -r '.violation')" = "true" ]; then
  hook_block "run git commit / git push on its own (only git add/status/diff/log may share the command), so the gate checks the exact repo and branch"
fi

ITEM=$(printf '%s' "$PARSED" | jq -c '.items[0]')
BAD_FLAG=$(printf '%s' "$ITEM" | jq -r '.bad_flag // empty')
if [ -n "$BAD_FLAG" ]; then
  hook_block "git push flag $BAD_FLAG isn't allowed by the gate; push the current branch with plain flags"
fi
# Deleting a remote branch pushes no code.
[ "$(printf '%s' "$ITEM" | jq -r '.delete')" = "true" ] && hook_allow
if [ "$(printf '%s' "$ITEM" | jq -r '.all')" = "true" ]; then
  hook_block "check out <ref> before pushing it, so the precheck sees what you push" \
    "A bare ':' (matching) refspec push refs other than the current branch."
fi

# Effective directory: honours `git -C <dir> push`.
DIR=$(printf '%s' "$ITEM" | jq -r '.dir // empty')
[ -n "$DIR" ] && [ -d "$DIR" ] && CWD="$DIR"
cd "$CWD" 2>/dev/null || hook_allow
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || hook_allow
cd "$ROOT" || hook_allow

# Only the current branch may be pushed: the precheck validates HEAD.
CURRENT=$(git symbolic-ref --short -q HEAD 2>/dev/null || true)

# Unnamed remote: git's push precedence is branch.<b>.pushRemote,
# remote.pushDefault, branch.<b>.remote, then origin.
REMOTE=$(printf '%s' "$ITEM" | jq -r '.remote // empty')
[ -z "$REMOTE" ] && [ -n "$CURRENT" ] && REMOTE=$(git config --get "branch.$CURRENT.pushRemote" 2>/dev/null || true)
[ -z "$REMOTE" ] && REMOTE=$(git config --get remote.pushDefault 2>/dev/null || true)
[ -z "$REMOTE" ] && [ -n "$CURRENT" ] && REMOTE=$(git config --get "branch.$CURRENT.remote" 2>/dev/null || true)
[ -z "$REMOTE" ] && REMOTE=origin
# remote.<name>.mirror makes a plain push send every ref, not just HEAD.
if [ "$(git config --bool --get "remote.$REMOTE.mirror" 2>/dev/null || true)" = "true" ]; then
  hook_block "remote.$REMOTE.mirror is set, so a push would send every ref; unset it or push from a non-mirror remote"
fi
# With no refspec, git picks the ref from remote.<name>.push, then push.default.
if [ "$(printf '%s' "$ITEM" | jq -r '.has_refspec')" != "true" ]; then
  if [ -n "$(git config --get-all "remote.$REMOTE.push" 2>/dev/null)" ]; then
    hook_block "remote.$REMOTE.push is configured; push with an explicit refspec (git push $REMOTE ${CURRENT:-<current-branch>})"
  fi
  case "$(git config --get push.default 2>/dev/null || true)" in
    matching | nothing)
      hook_block "push.default=$(git config --get push.default) pushes unpredictable refs; push with an explicit refspec (git push $REMOTE ${CURRENT:-<current-branch>})"
      ;;
  esac
fi
while IFS= read -r SRC; do
  [ -z "$SRC" ] && continue
  case "$SRC" in
    HEAD) continue ;;
    "$CURRENT" | "refs/heads/$CURRENT") [ -n "$CURRENT" ] && continue ;;
  esac
  hook_block "check out $SRC before pushing it, so the precheck sees what you push"
done < <(printf '%s' "$ITEM" | jq -r '.srcs[]?')

TREE=$(git rev-parse 'HEAD^{tree}' 2>/dev/null) || hook_allow
KEY="precheck-$TREE"
cache_fresh "$KEY" "$TTL" && hook_allow

BASE=$(git merge-base origin/dev HEAD 2>/dev/null) || {
  hook_warn "precheck: origin/dev not found; skipping pre-push checks."
  hook_allow
}
git diff --quiet "$BASE" HEAD 2>/dev/null && hook_allow
# Filenames are read NUL-delimited (-z): git's default output quotes and
# escapes non-ASCII names, which would then match no pattern below.
changed_files() { git diff -z --name-only --diff-filter=d "$BASE" HEAD 2>/dev/null; }

REPORT=""
FAIL=0
SKIPPED=0
warn_skip() {
  SKIPPED=1
  hook_warn "$@"
}
# What gets pushed is the committed snapshot (HEAD), and the cache is keyed by
# HEAD^{tree}, so per-file checks lint `git show HEAD:<path>` via stdin, never
# the working copy. Only `npm run typecheck` needs a real tree: it runs on the
# working copy, and a pass is not cached while frontend/ differs from HEAD
# (cache key must equal exactly what was checked).
_record() { # _record <label> <status> <out>
  if [ "$2" -ne 0 ]; then
    FAIL=1
    REPORT+="## ${1}\n$(printf '%s' "$3" | head -n 40)\n\n"
  fi
}
# snap_check <label> <dir> <repo-path> <cmd...> — pipes HEAD:<repo-path> to <cmd> run in <dir>.
snap_check() {
  local label="$1" dir="$2" path="$3" out rc
  shift 3
  if command -v timeout &>/dev/null; then
    out=$(git show "HEAD:$path" | (cd "$dir" && timeout 300 "$@" 2>&1))
  else
    out=$(git show "HEAD:$path" | (cd "$dir" && "$@" 2>&1))
  fi
  rc=$?
  _record "$label ($path)" "$rc" "$out"
}
# prettier --check has no stdin mode: format via stdin and diff against the input.
prettier_snap() { # prettier_snap <relpath-in-frontend>
  local path="$1" tmp out rc
  tmp=$(mktemp)
  git show "HEAD:frontend/$path" >"$tmp"
  out=$(cd frontend && timeout 300 node_modules/.bin/prettier --stdin-filepath "$path" <"$tmp" 2>&1 | cmp -s - "$tmp" && echo ok)
  [ "$out" = "ok" ] && rc=0 || rc=1
  rm -f "$tmp"
  _record "prettier ($path)" "$rc" "frontend/$path is not prettier-formatted"
}
run_check() { # run_check <label> <dir> <cmd...> — working-copy command
  local label="$1" dir="$2" out rc
  shift 2
  out=$(cd "$dir" && timeout 300 "$@" 2>&1)
  rc=$?
  _record "$label" "$rc" "$out"
}
command -v timeout &>/dev/null || timeout() { shift; "$@"; }

# ── Backend ──────────────────────────────────────────────────────
PY_FILES=()
while IFS= read -r -d '' f; do
  case "$f" in backend/*.py) PY_FILES+=("${f#backend/}") ;; esac
done < <(changed_files)
if [ "${#PY_FILES[@]}" -gt 0 ]; then
  if command -v ruff &>/dev/null; then
    for f in "${PY_FILES[@]}"; do
      snap_check "ruff" backend "backend/$f" ruff check --no-fix --stdin-filename "$f" -
    done
  else
    warn_skip "precheck: ruff not installed; skipping backend ruff check."
  fi
  if command -v black &>/dev/null; then
    for f in "${PY_FILES[@]}"; do
      snap_check "black" backend "backend/$f" black --check --quiet --stdin-filename "$f" -
    done
  else
    warn_skip "precheck: black not installed; skipping backend black check."
  fi
fi

# ── Frontend ─────────────────────────────────────────────────────
FE_FILES=()
while IFS= read -r -d '' f; do
  case "$f" in frontend/*.js | frontend/*.jsx | frontend/*.ts | frontend/*.tsx | frontend/*.mjs | frontend/*.cjs)
    FE_FILES+=("${f#frontend/}")
    ;;
  esac
done < <(changed_files)
if [ "${#FE_FILES[@]}" -gt 0 ]; then
  if [ -x frontend/node_modules/.bin/eslint ]; then
    for f in "${FE_FILES[@]}"; do
      snap_check "eslint" frontend "frontend/$f" node_modules/.bin/eslint --stdin --stdin-filename "$f"
    done
  else
    warn_skip "precheck: frontend deps not installed; skipping eslint."
  fi
  if [ -x frontend/node_modules/.bin/prettier ]; then
    for f in "${FE_FILES[@]}"; do prettier_snap "$f"; done
  else
    warn_skip "precheck: prettier not installed; skipping prettier."
  fi
fi
# Typecheck is project-wide, so any frontend change counts: deletions (a
# removed module may still be imported), tsconfig and dependency manifests.
if ! git diff --quiet "$BASE" HEAD -- frontend 2>/dev/null; then
  if [ -x frontend/node_modules/.bin/tsc ] && command -v npm &>/dev/null; then
    # typecheck reads the working copy, so its result only speaks for HEAD
    # when frontend/ matches HEAD (untracked files included).
    if [ -n "$(git status --porcelain --untracked-files=all -- frontend 2>/dev/null)" ]; then
      _record "typecheck" 1 "frontend/ has uncommitted or untracked changes; typecheck runs on the working copy, so commit or stash them before pushing."
    else
      run_check "typecheck" frontend npm run --silent typecheck
    fi
  else
    warn_skip "precheck: frontend deps not installed; skipping typecheck."
  fi
fi

if [ "$FAIL" -ne 0 ]; then
  hook_block "Pre-push checks failed. Fix these before pushing." "$REPORT"
fi
# Only cache a pass when nothing was skipped for a missing tool.
[ "$SKIPPED" -eq 0 ] && cache_set "$KEY"
hook_allow
