/* eslint-env jest, node */
const fs = require('fs');
const path = require('path');

const {
  applyPodDeploymentTargetFix,
  BEGIN,
  END,
} = require('../../plugins/withPodDeploymentTargetFix');

// Trimmed copy of the Expo Podfile template around the post_install hook.
const TEMPLATE = `target 'App' do
  post_install do |installer|
    react_native_post_install(
      installer,
      config[:reactNativePath],
      :mac_catalyst_enabled => false,
      :ccache_enabled => ccache_enabled?(podfile_properties),
    )

    # Fix fmt consteval compilation error
    fmt_dir = 'x'
  end
end
`;

describe('withPodDeploymentTargetFix', () => {
  it('inserts the generated block after react_native_post_install(...)', () => {
    const out = applyPodDeploymentTargetFix(TEMPLATE);

    const callEnd = out.indexOf('    )\n') + '    )\n'.length;
    expect(out.indexOf(BEGIN)).toBeGreaterThan(callEnd - 1);
    expect(out.indexOf(END)).toBeGreaterThan(out.indexOf(BEGIN));
    expect(out.indexOf('# Fix fmt consteval')).toBeGreaterThan(out.indexOf(END));
    expect(out).toContain('resource_bundle_targets');
    expect(out).toContain('t.platform.deployment_target');
  });

  it('is idempotent', () => {
    const once = applyPodDeploymentTargetFix(TEMPLATE);
    expect(applyPodDeploymentTargetFix(once)).toBe(once);
    expect(once.split(BEGIN)).toHaveLength(2);
  });

  it('refreshes a stale generated block instead of duplicating it', () => {
    const stale = TEMPLATE.replace(
      '    # Fix fmt',
      `    ${BEGIN}\n    old_code_that_should_go_away\n    ${END}\n\n    # Fix fmt`
    );
    const out = applyPodDeploymentTargetFix(stale);

    expect(out).not.toContain('old_code_that_should_go_away');
    expect(out.split(BEGIN)).toHaveLength(2);
    expect(out).toContain('resource_bundle_targets');
  });

  it('throws if the Expo template no longer has react_native_post_install(...)', () => {
    expect(() => applyPodDeploymentTargetFix("target 'App' do\nend\n")).toThrow(
      /react_native_post_install/
    );
  });
});

describe('committed ios/Podfile', () => {
  // Guards against `expo prebuild --clean` (or a hand edit) silently dropping
  // the Xcode 27 fix: the committed Podfile must equal the plugin's output.
  it('already contains the plugin output', () => {
    const podfile = fs.readFileSync(path.join(__dirname, '../../ios/Podfile'), 'utf8');

    expect(podfile).toContain(BEGIN);
    expect(applyPodDeploymentTargetFix(podfile)).toBe(podfile);
  });
});
