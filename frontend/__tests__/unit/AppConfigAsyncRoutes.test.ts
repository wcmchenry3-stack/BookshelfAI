/**
 * #712: the SDK 58 expo-router config plugin turns async (lazy) routes on for
 * web unless the app says otherwise. SDK 56 bundled every route into the entry
 * bundle, and so must we until lazy web routes are adopted on purpose: with
 * them on, the `--dev` web export the e2e suite runs (`npm run e2e:build`)
 * names every chunk `<basename>-<md5 of ''>.js`, so the My Books, Settings and
 * Wishlist route chunks are overwritten by the lazily loaded locale files of
 * the same name and the tabs crash with "Requiring unknown module".
 */
import appJson from '../../app.json';

type PluginEntry = string | [string, Record<string, unknown>?];

describe('app.json expo-router plugin', () => {
  it('keeps async routes off on every platform', () => {
    const plugins = appJson.expo.plugins as PluginEntry[];
    const router = plugins.find((p) => (Array.isArray(p) ? p[0] : p) === 'expo-router');

    expect(Array.isArray(router)).toBe(true);
    expect((router as [string, Record<string, unknown>])[1]).toEqual(
      expect.objectContaining({ asyncRoutes: false })
    );
  });
});
