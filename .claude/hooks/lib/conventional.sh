#!/usr/bin/env bash
# lib/conventional.sh — Conventional Commits header validation.
#
# Mirrors @commitlint/config-conventional. The CI "Commitlint" workflow calls
# a reusable workflow in the org's .github repo and this repo has no local
# commitlint config, so the shared defaults are mirrored here:
#   type-enum, type-case lower, type-empty never, scope-case lower,
#   subject-empty never, subject-full-stop never ".",
#   subject-case never sentence/upper-case, header-max-length 100.
# Merge/revert/fixup!/squash! headers are ignored like commitlint does.

CC_TYPES="feat fix docs style refactor perf test build ci chore revert"
CC_HEADER_MAX=100

# conventional_header_error <header> — prints a reason and returns 1 if invalid.
conventional_header_error() {
  local header="$1" re type subject first rest
  case "$header" in
    "Merge "* | 'Revert "'* | "fixup! "* | "squash! "* | "amend! "*) return 0 ;;
  esac
  if [ -z "$header" ]; then
    echo "header is empty"
    return 1
  fi
  if [ "${#header}" -gt "$CC_HEADER_MAX" ]; then
    echo "header is ${#header} chars (max $CC_HEADER_MAX)"
    return 1
  fi
  re='^([A-Za-z]+)(\(([^()]+)\))?(!)?: (.+)$'
  if ! [[ "$header" =~ $re ]]; then
    echo "header must look like 'type(scope): subject' (types: $CC_TYPES)"
    return 1
  fi
  type="${BASH_REMATCH[1]}"
  subject="${BASH_REMATCH[5]}"
  if ! [[ " $CC_TYPES " == *" $type "* ]]; then
    echo "type '$type' is not allowed (types: $CC_TYPES)"
    return 1
  fi
  if [[ "${BASH_REMATCH[3]}" =~ [A-Z] ]]; then
    echo "scope must be lower-case"
    return 1
  fi
  if [ "${subject: -1}" = "." ]; then
    echo "subject must not end with a period"
    return 1
  fi
  first="${subject:0:1}"
  rest="${subject:1}"
  if [[ "$first" =~ [A-Z] ]]; then
    # sentence-case ("Add thing") and upper-case ("ADD THING") are rejected;
    # mixed forms such as "ISBN lookup" pass, as in commitlint.
    if ! [[ "$rest" =~ [A-Z] ]] || ! [[ "$rest" =~ [a-z] ]]; then
      echo "subject must not be sentence-case or upper-case (start with a lower-case letter)"
      return 1
    fi
  fi
  return 0
}
