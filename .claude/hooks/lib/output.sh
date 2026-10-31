#!/usr/bin/env bash
# lib/output.sh — shared output helpers for Claude Code PreToolUse hooks.
#
# Block format (same as the existing hooks): print the reason to stderr and
# exit 2. Claude Code feeds stderr back to the model and cancels the tool call.
# Allow: exit 0. Warnings go to stderr but never change the exit code.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/output.sh"
#   hook_read_input
#   CMD=$(hook_jq '.tool_input.command // empty')
#   hook_block "headline" "details"   # exits 2
#   hook_allow                        # exits 0

# Reads the hook's JSON payload from stdin into HOOK_INPUT.
hook_read_input() {
  HOOK_INPUT=$(cat)
}

# hook_jq <jq-filter> — evaluates a filter against HOOK_INPUT (raw output).
hook_jq() {
  printf '%s' "${HOOK_INPUT:-}" | jq -r "$1" 2>/dev/null
}

hook_allow() {
  exit 0
}

# hook_warn <message> — non-blocking notice.
hook_warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

# hook_block <headline> [details] — blocks the tool call (exit 2).
hook_block() {
  {
    printf 'BLOCKED: %s\n' "$1"
    if [ -n "${2:-}" ]; then
      printf '\n%b\n' "$2"
    fi
  } >&2
  exit 2
}
