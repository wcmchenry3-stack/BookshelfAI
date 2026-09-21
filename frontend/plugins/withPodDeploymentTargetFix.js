/* eslint-env node */
// Expo config plugin: keeps the Xcode 27 deployment-target fix in the Podfile
// across `expo prebuild` (including `--clean`).
//
// Xcode 27 hard-errors on IPHONEOS_DEPLOYMENT_TARGET < 15.0 (was a warning).
// react_native_post_install raises pod library targets, but not CocoaPods
// resource-bundle targets (Sentry-Sentry, RNCAsyncStorage-RNCAsyncStorage_resources),
// which inherit the podspec minimum (11.0 / 13.4). See docs/claude/ios-ci.md.
const { withPodfile } = require('expo/config-plugins');

const TAG = 'bookshelf-pod-deployment-target';
const BEGIN = `# @generated begin ${TAG} (plugins/withPodDeploymentTargetFix.js - DO NOT MODIFY)`;
const END = `# @generated end ${TAG}`;

const BLOCK_RE = new RegExp(
  `^[ \\t]*# @generated begin ${TAG}[^\\n]*\\n[\\s\\S]*?^[ \\t]*# @generated end ${TAG}\\n`,
  'm'
);
// The whole `react_native_post_install( ... )` call from the Expo template.
const ANCHOR_RE = /^[ \t]*react_native_post_install\(\n(?:.*\n)*?[ \t]*\)\n/m;

const SNIPPET = `${BEGIN}
# Raise CocoaPods resource-bundle targets to the app's deployment target
# (\`platform :ios\`). Never lowers a higher target; skips unset or non-version
# values such as "$(inherited)".
min_target = installer.aggregate_targets
  .map { |t| t.platform.deployment_target&.to_s }
  .compact
  .max_by { |v| Gem::Version.new(v) }
if min_target
  installer.target_installation_results.pod_target_installation_results.each_value do |result|
    result.resource_bundle_targets.each do |bundle_target|
      bundle_target.build_configurations.each do |bc|
        current = bc.build_settings['IPHONEOS_DEPLOYMENT_TARGET']
        next if current.nil? || !Gem::Version.correct?(current)
        if Gem::Version.new(current) < Gem::Version.new(min_target)
          bc.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = min_target
        end
      end
    end
  end
end
${END}
`;

function indent(text, spaces) {
  const pad = ' '.repeat(spaces);
  return text
    .split('\n')
    .map((line) => (line === '' ? line : pad + line))
    .join('\n');
}

/**
 * Insert (or refresh) the generated block right after the
 * `react_native_post_install(...)` call. Idempotent. Throws if the Expo
 * template no longer contains that call, so a template change cannot silently
 * drop the fix.
 */
function applyPodDeploymentTargetFix(contents) {
  const block = indent(SNIPPET, 4);

  if (BLOCK_RE.test(contents)) {
    return contents.replace(BLOCK_RE, () => block);
  }

  if (!ANCHOR_RE.test(contents)) {
    throw new Error(
      `[withPodDeploymentTargetFix] Could not find the react_native_post_install(...) call in the Podfile. ` +
        `The Expo Podfile template changed; update ANCHOR_RE in plugins/withPodDeploymentTargetFix.js.`
    );
  }
  return contents.replace(ANCHOR_RE, (match) => `${match}\n${block}`);
}

const withPodDeploymentTargetFix = (config) =>
  withPodfile(config, (mod) => {
    mod.modResults.contents = applyPodDeploymentTargetFix(mod.modResults.contents);
    return mod;
  });

module.exports = withPodDeploymentTargetFix;
module.exports.applyPodDeploymentTargetFix = applyPodDeploymentTargetFix;
module.exports.BEGIN = BEGIN;
module.exports.END = END;
