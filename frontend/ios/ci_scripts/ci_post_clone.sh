#!/bin/zsh
# Xcode Cloud: runs automatically after the repo is cloned.
# Node.js is not pre-installed on Xcode Cloud workers — install via Homebrew.
# Pods/ is gitignored, so pod install must run before xcodebuild archive.
set -e

# Allow Sentry source-map upload to fail gracefully in CI
# (org/project/auth token must be configured as Xcode Cloud env vars)
export SENTRY_ALLOW_FAILURE=true

# ── Retry helper ──────────────────────────────────────────────
# Usage: retry <max_attempts> <delay_seconds> <command...>
# Retries a command with exponential back-off (delay doubles each attempt).
# Needed because pod install fetches from GitHub/CDN which can 429 or 500.
retry() {
  local max_attempts=$1; shift
  local delay=$1; shift
  local attempt=1
  while true; do
    if "$@"; then
      return 0
    fi
    if (( attempt >= max_attempts )); then
      echo "ERROR: '$*' failed after $max_attempts attempts" >&2
      return 1
    fi
    echo "RETRY: attempt $attempt/$max_attempts failed, waiting ${delay}s..." >&2
    sleep "$delay"
    (( attempt++ ))
    (( delay *= 2 ))
  done
}

# Install Node.js 22 LTS — pinned away from latest (25.x) because npm 11.x
# (which ships with Node 25) has a known crash: "Exit handler never called!"
# that fires on our package-lock.json tree. Node 22 LTS ships npm 10.x and
# satisfies react-native's ">= 20.19.4" engine requirement.
brew install node@22
export PATH="$(brew --prefix node@22)/bin:$PATH"

# Persist NODE_BINARY for Xcode build phases (which run in separate processes).
# Without this, .xcode.env falls back to `command -v node` which returns empty
# on Xcode Cloud (node@22 is keg-only and not on the default PATH).
# An empty NODE_BINARY causes the "Bundle React Native code" phase to fail
# silently (masked by SENTRY_ALLOW_FAILURE=true), producing an archive
# without main.jsbundle → handleBundleLoadingError crash at launch.
NODE22_BIN="$(brew --prefix node@22)/bin/node"
echo "export NODE_BINARY=\"$NODE22_BIN\"" > "$CI_PRIMARY_REPOSITORY_PATH/frontend/ios/.xcode.env.local"
echo "INFO: wrote .xcode.env.local → NODE_BINARY=$NODE22_BIN"

# Install Node.js dependencies (required for React Native pod scripts)
cd "$CI_PRIMARY_REPOSITORY_PATH/frontend"
retry 3 10 npm ci

# Install CocoaPods dependencies
cd ios
# NOTE: Podfile.lock is committed and up-to-date (regenerated for RN 0.85.3 / SDK 56).
# Do NOT delete it — using the committed lock ensures reproducible pod versions across
# CI builds and prevents Hermes/React-Native runtime drift that can cause
# handleBundleLoadingError crashes at launch.
retry 3 30 pod install
