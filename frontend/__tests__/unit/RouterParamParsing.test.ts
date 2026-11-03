/**
 * Expo Router's deep-link / URL param parsing goes through query-string@7 and
 * so through decode-uri-component (#779). This drives the router's own
 * getStateFromPath and checks decoded params come out.
 */
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { getStateFromPath } = require('expo-router/build/react-navigation/core/getStateFromPath');

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
    expect(paramsOf('/book?title=%E0%A4%A')).toEqual({ title: '%E0%A4%A' });
  });
});
