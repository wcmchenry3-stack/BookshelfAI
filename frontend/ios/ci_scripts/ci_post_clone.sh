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

# Fail fast if Xcode Cloud rolled onto an Xcode this Expo SDK can't build (#708).
"$CI_PRIMARY_REPOSITORY_PATH/frontend/ios/ci_scripts/check_xcode_compat.sh" \
  "$CI_PRIMARY_REPOSITORY_PATH/frontend/package-lock.json"

# Install Node.js 22 LTS — pinned away from latest (25.x) because npm 11.x
# (which ships with Node 25) has a known crash: "Exit handler never called!"
# that fires on our package-lock.json tree. Node 22 LTS ships npm 10.x and
# satisfies react-native's ">= 20.19.4" engine requirement.
#
# Downloaded from nodejs.org, not Homebrew (#718): Xcode Cloud runs Xcode 26.x
# on Intel x86_64 workers, Homebrew stopped shipping Intel bottles in Sept 2026,
# and `brew install node@22` then builds ~20 dependencies from source and fails
# ("C compiler cannot create executables"). The official tarball is prebuilt for
# both architectures and checked against the SHASUMS256 published by nodejs.org.
# To bump: pick a v22.x from https://nodejs.org/dist/index.json whose npm is
# 10.x, and copy both darwin hashes from its SHASUMS256.txt.
NODE_VERSION="22.23.4"
case "$(uname -m)" in
  arm64)  NODE_ARCH="arm64"; NODE_SHA256="c9e1dbc748b33ed2d14f42445191ecaf14c56259c7a9a26d0042c49ec2b66428" ;;
  x86_64) NODE_ARCH="x64";   NODE_SHA256="efcc2b0db6f94bf53eddb038fe2b370277a042bd29e4f19b8f8e23598795b819" ;;
  *) echo "ERROR: unsupported CPU architecture $(uname -m)" >&2; exit 1 ;;
esac
NODE_DIST="node-v${NODE_VERSION}-darwin-${NODE_ARCH}"
NODE_ROOT="$HOME/.bookshelf-node"
NODE_HOME="$NODE_ROOT/$NODE_DIST"
if [[ ! -x "$NODE_HOME/bin/node" ]]; then
  mkdir -p "$NODE_ROOT"
  retry 3 10 curl -fsSL -o "$NODE_ROOT/$NODE_DIST.tar.gz" \
    "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_DIST}.tar.gz"
  echo "$NODE_SHA256  $NODE_ROOT/$NODE_DIST.tar.gz" | shasum -a 256 -c -
  tar -xzf "$NODE_ROOT/$NODE_DIST.tar.gz" -C "$NODE_ROOT"
  rm -f "$NODE_ROOT/$NODE_DIST.tar.gz"
fi
export PATH="$NODE_HOME/bin:$PATH"
echo "INFO: node $(node --version), npm $(npm --version) ($(uname -m))"

# Persist NODE_BINARY for Xcode build phases (which run in separate processes).
# Without this, .xcode.env falls back to `command -v node` which returns empty
# on Xcode Cloud (this Node is not on the default PATH).
# An empty NODE_BINARY causes the "Bundle React Native code" phase to fail
# silently (masked by SENTRY_ALLOW_FAILURE=true), producing an archive
# without main.jsbundle → handleBundleLoadingError crash at launch.
NODE22_BIN="$NODE_HOME/bin/node"
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
