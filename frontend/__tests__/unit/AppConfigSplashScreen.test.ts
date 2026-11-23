/**
 * #1010: on SDK 58 `expo prebuild` writes the native splash (Android
 * `splashscreen_logo` and styles, iOS `SplashScreenLogo` and storyboard) only
 * from the `expo-splash-screen` config plugin; the top-level `splash` key is
 * ignored, and without the plugin Android gets the template's grey placeholder.
 * Light and dark are both configured (the brand navy is fixed, so they match).
 */
import appJson from '../../app.json';
import pkg from '../../package.json';

type PluginEntry = string | [string, Record<string, unknown>?];

describe('app.json expo-splash-screen plugin', () => {
  const plugins = appJson.expo.plugins as PluginEntry[];
  const entry = plugins.find((p) => (Array.isArray(p) ? p[0] : p) === 'expo-splash-screen');
  const config = (entry as [string, Record<string, unknown>])[1] as {
    image: string;
    imageWidth: number;
    resizeMode: string;
    backgroundColor: string;
    dark: { image: string; backgroundColor: string };
  };

  it('is registered with a config and installed as a dependency', () => {
    expect(Array.isArray(entry)).toBe(true);
    expect(pkg.dependencies).toHaveProperty('expo-splash-screen');
  });

  it('configures the light splash from the generated splash-icon asset', () => {
    expect(config.image).toBe('./assets/splash-icon.png');
    expect(config.resizeMode).toBe('contain');
    expect(config.imageWidth).toBeGreaterThan(0);
    expect(config.backgroundColor).toBe('#263250');
  });

  it('configures the dark splash', () => {
    expect(config.dark.image).toBe('./assets/splash-icon.png');
    expect(config.dark.backgroundColor).toBe('#263250');
  });

  it('no longer uses the ignored top-level splash key', () => {
    expect(appJson.expo).not.toHaveProperty('splash');
  });
});
