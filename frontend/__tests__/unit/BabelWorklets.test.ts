/**
 * Reanimated 4 needs `react-native-worklets/plugin` to turn `'worklet'` functions
 * into objects the UI runtime can run (#901). babel-preset-expo adds it by itself
 * when react-native-worklets is installed, so babel.config.js does not list it.
 * This test transforms a worklet with the app's own Babel config and checks that
 * the plugin ran, and that it runs only once.
 */
import path from 'path';
import { transformSync } from '@babel/core';

const root = path.resolve(__dirname, '../..');

function transformWorklet(platform: string) {
  const result = transformSync("export function double(x) {\n  'worklet';\n  return x * 2;\n}\n", {
    configFile: path.join(root, 'babel.config.js'),
    // In dev the plugin reads the source file back for its source map, so the
    // filename must exist: use this test file (its content is only copied).
    filename: __filename,
    caller: { name: 'metro', bundler: 'metro', platform } as never,
    babelrc: false,
  });
  return result?.code ?? '';
}

describe('Babel worklets plugin (#901)', () => {
  it.each(['ios', 'android'])('transforms worklet functions on %s', (platform) => {
    const code = transformWorklet(platform);
    expect(code).toContain('__workletHash');
    // Listed once: a second copy of the plugin would wrap the worklet twice.
    expect(code.match(/__workletHash\s*=/g)).toHaveLength(1);
  });

  it('does not list the plugin in babel.config.js as well as the preset', () => {
    const config = require(path.join(root, 'babel.config.js'))({ cache: () => undefined });
    expect(config.plugins ?? []).not.toEqual(
      expect.arrayContaining([expect.stringContaining('worklets/plugin')])
    );
  });
});
