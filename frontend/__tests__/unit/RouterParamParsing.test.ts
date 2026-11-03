/**
 * Expo Router's deep-link / URL param parsing goes through query-string@7 and
 * so through decode-uri-component (#779). This drives the router's own
 * getStateFromPath and checks decoded params come out.
 */
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { getStateFromPath } = require('expo-router/build/fork/getStateFromPath');

const config = { screens: { book: 'book' } };

function paramsOf(path: string): Record<string, unknown> | undefined {
  const state = getStateFromPath(path, config);
  return state?.routes?.[0]?.params;
}

describe('expo-router URL param parsing', () => {
  it('decodes percent-encoded query params from a deep link', () => {
    expect(paramsOf('/book?title=Dune%20Messiah&mark=%E2%9C%93')).toEqual({
      title: 'Dune Messiah',
      mark: '✓',
    });
  });

  it('survives a malformed sequence without throwing', () => {
    expect(() => paramsOf('/book?title=%E0%A4%A')).not.toThrow();
    // The exact value is the router's own decoder's choice since SDK 58 (#884);
    // only "does not throw" is ours to guarantee (#712 owner decision).
    expect(typeof paramsOf('/book?title=%E0%A4%A').title).toBe('string');
  });
});
