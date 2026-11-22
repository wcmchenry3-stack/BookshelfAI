#!/usr/bin/env bash
# lib/cache.sh — tiny keyed cache with a TTL for hooks.
#
# Entries are empty marker files under
#   ${XDG_CACHE_HOME:-$HOME/.cache}/bookshelf-claude-hooks/
# whose mtime records when they were written.
#
#   cache_fresh <key> <ttl-seconds>   # exit 0 if a non-expired entry exists
#   cache_set   <key>                 # (re)writes the entry
#   cache_clear <key>

cache_dir() {
  printf '%s' "${XDG_CACHE_HOME:-$HOME/.cache}/bookshelf-claude-hooks"
}

_cache_path() {
  # Keys are sanitised so they cannot escape the cache directory.
  local safe
  safe=$(printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_')
  printf '%s/%s' "$(cache_dir)" "$safe"
}

_cache_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

cache_fresh() {
  local path mtime now
  path=$(_cache_path "$1")
  [ -f "$path" ] || return 1
  mtime=$(_cache_mtime "$path") || return 1
  now=$(date +%s)
  [ $((now - mtime)) -lt "${2:-0}" ]
}

cache_set() {
  local path
  path=$(_cache_path "$1")
  mkdir -p "$(cache_dir)" 2>/dev/null || return 0
  : >"$path" 2>/dev/null || true
}

cache_clear() {
  rm -f "$(_cache_path "$1")" 2>/dev/null || true
}
