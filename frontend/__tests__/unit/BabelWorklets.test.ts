/**
 * Reanimated 4 needs `react-native-worklets/plugin` to turn `'worklet'` functions
 * into objects the UI runtime can run (#901). babel-preset-expo already adds it
 * when react-native-worklets is installed, so listing it in babel.config.js is
 * redundant. These tests prove the plugin comes from the preset: the app's config
 * transforms a worklet, and the same preset with `worklets: false` does not.
 */
import path from 'path';
import { transformSync, type TransformOptions } from '@babel/core';

const root = path.resolve(__dirname, '../..');
const SOURCE = "export function double(x) {\n  'worklet';\n  return x * 2;\n}\n";

function transformWorklet(platform: string, config: Partial<TransformOptions>) {
  const result = transformSync(SOURCE, {
    // In dev the plugin reads the source file back for its source map, so the
    // filename must exist: use this test file (its content is only copied).
    filename: __filename,
    caller: { name: 'metro', bundler: 'metro', platform } as never,
    babelrc: false,
    ...config,
  });
  return result?.code ?? '';
}

const countWorkletHashes = (code: string) => code.match(/__workletHash\s*=/g)?.length ?? 0;

describe('Babel worklets plugin (#901)', () => {
  it.each(['ios', 'android'])('the app config transforms worklet functions on %s', (platform) => {
    const code = transformWorklet(platform, { configFile: path.join(root, 'babel.config.js') });
    expect(countWorkletHashes(code)).toBe(1);
  });

  it.each(['ios', 'android'])(
    'babel-preset-expo with worklets: false leaves them alone on %s',
    (platform) => {
      const code = transformWorklet(platform, {
        configFile: false,
        presets: [[require.resolve('babel-preset-expo'), { worklets: false, reanimated: false }]],
      });
      expect(countWorkletHashes(code)).toBe(0);
    }
  );

  it('babel.config.js does not list the worklets or reanimated plugin itself', () => {
    const config = require(path.join(root, 'babel.config.js'))({ cache: () => undefined });
    const plugins = JSON.stringify(config.plugins ?? []);
    expect(plugins).not.toMatch(/react-native-worklets\/plugin/);
    expect(plugins).not.toMatch(/react-native-reanimated\/plugin/);
  });
});
