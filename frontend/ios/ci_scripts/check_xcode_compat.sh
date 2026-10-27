#!/bin/bash
# Fails fast when the selected Xcode is too new for the installed Expo SDK (#708).
#
# Expo SDK 56 (expo-modules-jsi 56.x) does not support Xcode 27. The
# "Build ExpoModulesJSI xcframework" build phase fails the archive with
# "the following command failed with exit code 0 but produced no further output",
# and an app that did build would crash at launch (_swift_allocateMetadataPack, #598).
# Upstream support for Xcode 27 starts at expo-modules-jsi 58.0.12.
#
# Xcode Cloud workflows on "Latest Release" move to a new Xcode major silently, so
# check before spending a build on it. Called by ci_post_clone.sh before any install.
#
# Usage: check_xcode_compat.sh <path to frontend/package-lock.json>
# Env:   ALLOW_UNSUPPORTED_XCODE=1 skips the check (e.g. to try an Expo upgrade on Xcode 27).
#        XCODE_VERSION_OVERRIDE / JSI_VERSION_OVERRIDE are for tests only.
# Bash 3.2 compatible (macOS /bin/bash).

set -eu

MIN_JSI_FOR_XCODE_27="58.0.12"

lockfile="${1:?usage: check_xcode_compat.sh <package-lock.json>}"

xcode_line="${XCODE_VERSION_OVERRIDE:-$(xcodebuild -version 2>/dev/null | head -1 || true)}"
xcode_major="$(printf '%s\n' "$xcode_line" | sed -nE 's/^Xcode ([0-9]+).*/\1/p')"

if [ -z "${JSI_VERSION_OVERRIDE:-}" ] && [ ! -r "$lockfile" ]; then
  echo "ERROR: lockfile not found or unreadable: $lockfile" >&2
  exit 1
fi

# Every expo-modules-jsi entry at any depth (a nested copy can be older than the
# top-level one); keep the numeric x.y.z core of each version (prerelease
# suffixes such as -canary-… are dropped) and take the lowest.
jsi_version="${JSI_VERSION_OVERRIDE:-$(grep -A1 'node_modules/expo-modules-jsi"' "$lockfile" \
  | sed -nE 's/.*"version": *"([0-9]+\.[0-9]+\.[0-9]+)[^"]*".*/\1/p' \
  | sort -t. -k1,1n -k2,2n -k3,3n | head -1 || true)}"

echo "INFO: ${xcode_line:-Xcode version unknown}, expo-modules-jsi ${jsi_version:-unknown}"

if [ "${ALLOW_UNSUPPORTED_XCODE:-}" = "1" ]; then
  echo "WARN: ALLOW_UNSUPPORTED_XCODE=1, skipping the Xcode compatibility check"
  exit 0
fi

# Unknown versions (no Xcode on PATH, no expo-modules-jsi entry): don't block
# the build on a parsing problem. A missing lockfile already failed above.
if [ -z "$xcode_major" ] || [ -z "$jsi_version" ]; then
  echo "WARN: could not determine Xcode or expo-modules-jsi version, skipping the check"
  exit 0
fi

# True when version $1 >= version $2 (x.y.z, numeric per field).
version_at_least() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$2" ]
}

if [ "$xcode_major" -ge 27 ] && ! version_at_least "$jsi_version" "$MIN_JSI_FOR_XCODE_27"; then
  cat >&2 <<EOF
ERROR: ${xcode_line} is not supported by this app's Expo SDK
       (expo-modules-jsi ${jsi_version}; Xcode 27 needs >= ${MIN_JSI_FOR_XCODE_27}, Expo SDK 58).
       The archive would fail in "Build ExpoModulesJSI xcframework", or the app would crash at launch.
       Fix: in App Store Connect > Xcode Cloud > workflow > Environment, pin Xcode to the newest 26.x
       instead of "Latest Release". See docs/claude/ios-ci.md § Xcode version rollovers (#708).
EOF
  exit 1
fi

echo "INFO: Xcode / Expo compatibility check passed"
