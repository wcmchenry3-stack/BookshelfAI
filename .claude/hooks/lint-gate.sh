#!/usr/bin/env bash
# lint-gate.sh — PreToolUse hook for Claude Code
# Blocks `gh pr create` when linting issues exist, prompting the
# lint-review sub-agent to auto-fix before retrying.
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/output.sh
source "$HOOK_DIR/lib/output.sh"

# ── Read tool input from stdin ───────────────────────────────────
hook_read_input
COMMAND=$(hook_jq '.tool_input.command // empty')

# Only gate on PR-creation commands
if ! echo "$COMMAND" | grep -qE 'gh[[:space:]]+pr[[:space:]]+create'; then
  hook_allow
fi

FAIL=0
REPORT=""

# ── Python linting (black + ruff) ───────────────────────────────
if [ -f "pyproject.toml" ] || [ -f "requirements.txt" ] || [ -f "setup.py" ] || [ -f "setup.cfg" ]; then
  if command -v black &>/dev/null; then
    OUT=$(black --check --quiet . 2>&1) || {
      FAIL=1
      REPORT+="## black\n${OUT}\n\n"
    }
  fi
  if command -v ruff &>/dev/null; then
    OUT=$(ruff check --no-fix . 2>&1) || {
      FAIL=1
      REPORT+="## ruff\n${OUT}\n\n"
    }
  fi
fi

# ── Frontend linting (eslint + prettier) ─────────────────────────
if [ -f "package.json" ]; then
  if [ -x "node_modules/.bin/eslint" ]; then
    OUT=$(npx eslint . 2>&1) || {
      FAIL=1
      REPORT+="## eslint\n${OUT}\n\n"
    }
  fi
  if [ -x "node_modules/.bin/prettier" ]; then
    OUT=$(npx prettier --check . 2>&1) || {
      FAIL=1
      REPORT+="## prettier\n${OUT}\n\n"
    }
  fi
fi

# ── Verdict ──────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  hook_block "Lint check failed. Invoke the lint-review agent to auto-fix." "$REPORT"
fi

hook_allow
