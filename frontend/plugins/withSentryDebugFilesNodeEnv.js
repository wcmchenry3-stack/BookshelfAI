/* eslint-env node */
// Expo config plugin: makes NODE_BINARY available to the "Upload Debug Symbols
// to Sentry" Xcode build phase across `expo prebuild` (including `--clean`).
//
// @sentry/react-native's Expo plugin (re)writes that phase on every prebuild to
//   /bin/sh "`${NODE_BINARY:-node} --print "...sentry-xcode-debug-files.sh"`"
// but nothing in the phase's environment sets NODE_BINARY, and Xcode Cloud has
// no `node` on PATH (ci_post_clone.sh installs Node privately and records it in
// ios/.xcode.env.local). The command substitution is then empty and
// `/bin/sh ""` fails the archive (Xcode Cloud build 161, #717). This plugin
// prepends a block that sources ios/.xcode.env and ios/.xcode.env.local exactly
// as the "Bundle React Native code and images" phase does. See
// docs/claude/ios-ci.md.
//
// ORDERING: for the same mod, Expo runs the LAST-registered plugin's action
// FIRST (each withMod action runs, then calls the previously registered mod).
// This plugin must therefore be listed BEFORE `@sentry/react-native/expo` in
// app.json so its action runs after Sentry has rewritten the phase.
const { withXcodeProject } = require('expo/config-plugins');

const PHASE_NAME = 'Upload Debug Symbols to Sentry';
const SENTRY_SCRIPT = 'sentry-xcode-debug-files.sh';

const TAG = 'bookshelf-sentry-node-env';
const BEGIN = `# @generated begin ${TAG} (plugins/withSentryDebugFilesNodeEnv.js - DO NOT MODIFY)`;
const END = `# @generated end ${TAG}`;

const BLOCK_RE = new RegExp(
  `^# @generated begin ${TAG}[^\\n]*\\n[\\s\\S]*?^# @generated end ${TAG}\\n`,
  'm'
);

// POSIX sh: the phase runs with shellPath /bin/sh.
const PRELUDE = `${BEGIN}
# Load NODE_BINARY like "Bundle React Native code and images" does.
if [ -f "$SRCROOT/.xcode.env" ]; then . "$SRCROOT/.xcode.env"; fi
if [ -f "$SRCROOT/.xcode.env.local" ]; then . "$SRCROOT/.xcode.env.local"; fi
if [ -z "$NODE_BINARY" ] && ! command -v node >/dev/null 2>&1; then
  if [ "$SENTRY_ALLOW_FAILURE" = true ]; then
    echo "warning: Sentry dSYM upload skipped: NODE_BINARY is unset and node is not on PATH (SENTRY_ALLOW_FAILURE=true)"
    exit 0
  fi
  echo "error: Sentry dSYM upload: NODE_BINARY is unset and node is not on PATH. Set NODE_BINARY in ios/.xcode.env.local."
  exit 1
fi
${END}
`;

/**
 * Prepend (or refresh) the env-loading block in a raw (decoded) shell script.
 * Idempotent. Leaves the Sentry-generated command untouched. Throws if the
 * script no longer invokes sentry-xcode-debug-files.sh.
 */
function applySentryNodeEnv(script) {
  if (!script.includes(SENTRY_SCRIPT)) {
    throw new Error(
      `[withSentryDebugFilesNodeEnv] The "${PHASE_NAME}" phase no longer calls ${SENTRY_SCRIPT}. ` +
        `@sentry/react-native changed its generated script; update plugins/withSentryDebugFilesNodeEnv.js.`
    );
  }
  const rest = script.replace(BLOCK_RE, '');
  return `${PRELUDE}${rest}`;
}

/**
 * Apply the fix to the PBXShellScriptBuildPhase object of an `xcode` project.
 * The `xcode` library stores existing scripts JSON-encoded; a phase Sentry just
 * created via addBuildPhase may still hold a raw string.
 */
function applyToXcodeProject(xcodeProject) {
  const phase = xcodeProject.pbxItemByComment(PHASE_NAME, 'PBXShellScriptBuildPhase');
  if (!phase) {
    throw new Error(
      `[withSentryDebugFilesNodeEnv] No "${PHASE_NAME}" build phase found. ` +
        `Is @sentry/react-native/expo still registered AFTER this plugin in app.json? ` +
        `See docs/claude/ios-ci.md.`
    );
  }
  let encoded = true;
  let script;
  try {
    script = JSON.parse(phase.shellScript);
    if (typeof script !== 'string') throw new Error('not a string');
  } catch {
    encoded = false;
    script = phase.shellScript;
  }
  const next = applySentryNodeEnv(script);
  phase.shellScript = encoded ? JSON.stringify(next) : next;
  return xcodeProject;
}

const withSentryDebugFilesNodeEnv = (config) =>
  withXcodeProject(config, (mod) => {
    applyToXcodeProject(mod.modResults);
    return mod;
  });

module.exports = withSentryDebugFilesNodeEnv;
module.exports.applySentryNodeEnv = applySentryNodeEnv;
module.exports.applyToXcodeProject = applyToXcodeProject;
module.exports.BEGIN = BEGIN;
module.exports.END = END;
module.exports.PHASE_NAME = PHASE_NAME;
