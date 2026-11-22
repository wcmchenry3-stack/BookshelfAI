/**
 * #598: `ios.infoPlist.CFBundleURLTypes` in app.json replaces the top-level
 * `scheme` on iOS (prebuild logs "Ignoring abstract property scheme"), so
 * `bookshelf` must be listed there explicitly, and in the committed Info.plist
 * (Xcode Cloud does not run prebuild). The Google reversed client id must stay:
 * expo-auth-session redirects back to it.
 */
import fs from 'fs';
import path from 'path';

import appJson from '../../app.json';

const GOOGLE_PREFIX = 'com.googleusercontent.apps.';

type UrlType = { CFBundleURLSchemes: string[] };

function plistUrlSchemes(xml: string): string[] {
  const block = /<key>CFBundleURLTypes<\/key>\s*<array>([\s\S]*?)\n\t<\/array>/.exec(xml);
  if (!block) return [];
  return [...block[1].matchAll(/<string>([^<]*)<\/string>/g)].map((m) => m[1]);
}

describe('iOS URL schemes', () => {
  it('app.json registers bookshelf and the Google scheme for iOS', () => {
    const types = appJson.expo.ios.infoPlist.CFBundleURLTypes as UrlType[];
    const schemes = types.flatMap((t) => t.CFBundleURLSchemes);

    expect(schemes).toContain(appJson.expo.scheme);
    expect(schemes).toContain('bookshelf');
    expect(schemes.some((s) => s.startsWith(GOOGLE_PREFIX))).toBe(true);
  });

  it('committed Info.plist registers bookshelf and the Google scheme', () => {
    const xml = fs.readFileSync(path.join(__dirname, '../../ios/BookShelfAI/Info.plist'), 'utf8');
    const schemes = plistUrlSchemes(xml);

    expect(schemes).toContain('bookshelf');
    expect(schemes.some((s) => s.startsWith(GOOGLE_PREFIX))).toBe(true);
  });
});
