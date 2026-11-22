/**
 * #1001: the iOS privacy manifest declares the data the app sends off the
 * device. Scan photos go to our server and on to the AI provider (Photos,
 * not linked: owner decision), the account is a Google sign-in (email, name,
 * user ID), the library lives on our server (other user content), in-app
 * feedback and diagnostics go to Sentry, and per-user scan usage is recorded
 * (product interaction). Text searches leave the device in request URLs
 * (search history). Nothing is used for tracking.
 *
 * `ios/BookShelfAI/PrivacyInfo.xcprivacy` is generated from
 * `app.json` `ios.privacyManifests` by prebuild, but it is committed and Xcode
 * Cloud does not run prebuild, so the two are checked against each other here.
 */
import fs from 'fs';
import path from 'path';

import appJson from '../../app.json';

type PlistValue = string | boolean | PlistValue[] | { [key: string]: PlistValue };

interface CollectedDataType {
  NSPrivacyCollectedDataType: string;
  NSPrivacyCollectedDataTypeLinked: boolean;
  NSPrivacyCollectedDataTypeTracking: boolean;
  NSPrivacyCollectedDataTypePurposes: string[];
}

const T = 'NSPrivacyCollectedDataType';
const APP_FUNCTIONALITY = 'NSPrivacyCollectedDataTypePurposeAppFunctionality';
const ANALYTICS = 'NSPrivacyCollectedDataTypePurposeAnalytics';

/** [data type, linked to the user, purposes]. Tracking is false for all. */
const EXPECTED: [string, boolean, string[]][] = [
  [`${T}PhotosorVideos`, false, [APP_FUNCTIONALITY]],
  [`${T}EmailAddress`, true, [APP_FUNCTIONALITY]],
  [`${T}Name`, true, [APP_FUNCTIONALITY]],
  [`${T}UserID`, true, [APP_FUNCTIONALITY]],
  [`${T}OtherUserContent`, true, [APP_FUNCTIONALITY]],
  [`${T}CustomerSupport`, false, [APP_FUNCTIONALITY]],
  [`${T}ProductInteraction`, true, [APP_FUNCTIONALITY, ANALYTICS]],
  [`${T}CrashData`, false, [APP_FUNCTIONALITY]],
  [`${T}PerformanceData`, true, [APP_FUNCTIONALITY]],
  [`${T}OtherDiagnosticData`, true, [APP_FUNCTIONALITY]],
  // Search text is in Sentry breadcrumb URLs and the API access log (#1029).
  [`${T}SearchHistory`, false, [APP_FUNCTIONALITY]],
];

const EXPECTED_TYPES: CollectedDataType[] = EXPECTED.map(([type, linked, purposes]) => ({
  NSPrivacyCollectedDataType: type,
  NSPrivacyCollectedDataTypeLinked: linked,
  NSPrivacyCollectedDataTypeTracking: false,
  NSPrivacyCollectedDataTypePurposes: purposes,
}));

/**
 * Minimal XML plist reader for the elements a privacy manifest uses
 * (dict, array, key, string, true, false). Throws on anything else so an
 * unexpected element fails the test instead of being skipped.
 */
function parsePlist(xml: string): PlistValue {
  const body = /<plist[^>]*>([\s\S]*)<\/plist>/.exec(xml);
  if (!body) throw new Error('not a plist');
  const tokens = [...body[1].matchAll(/<(\/?)(\w+)(\/?)>([^<]*)/g)].map((m) => ({
    close: m[1] === '/',
    name: m[2],
    selfClosing: m[3] === '/',
    text: m[4],
  }));
  let i = 0;

  function value(): PlistValue {
    const tok = tokens[i++];
    if (!tok || tok.close) throw new Error('unexpected end of element');
    switch (tok.name) {
      case 'true':
      case 'false':
        if (!tok.selfClosing) throw new Error(`<${tok.name}> must be self-closing`);
        return tok.name === 'true';
      case 'string': {
        if (tok.selfClosing) return '';
        const end = tokens[i++];
        if (!end?.close || end.name !== 'string') throw new Error('bad <string>');
        return tok.text;
      }
      case 'array': {
        const items: PlistValue[] = [];
        if (tok.selfClosing) return items;
        while (!(tokens[i].close && tokens[i].name === 'array')) items.push(value());
        i++;
        return items;
      }
      case 'dict': {
        const dict: { [key: string]: PlistValue } = {};
        if (tok.selfClosing) return dict;
        while (!(tokens[i].close && tokens[i].name === 'dict')) {
          const key = tokens[i++];
          if (key.name !== 'key' || key.close) throw new Error('expected <key>');
          const end = tokens[i++];
          if (!end?.close || end.name !== 'key') throw new Error('bad <key>');
          dict[key.text] = value();
        }
        i++;
        return dict;
      }
      default:
        throw new Error(`unsupported plist element <${tok.name}>`);
    }
  }

  const root = value();
  if (i !== tokens.length) throw new Error('trailing content after the root element');
  return root;
}

function byType(types: CollectedDataType[]): CollectedDataType[] {
  return [...types].sort((a, b) =>
    a.NSPrivacyCollectedDataType.localeCompare(b.NSPrivacyCollectedDataType)
  );
}

const manifest = appJson.expo.ios.privacyManifests;
const appJsonTypes = manifest.NSPrivacyCollectedDataTypes as CollectedDataType[];
const plist = parsePlist(
  fs.readFileSync(path.join(__dirname, '../../ios/BookShelfAI/PrivacyInfo.xcprivacy'), 'utf8')
) as { [key: string]: PlistValue };
const plistTypes = plist.NSPrivacyCollectedDataTypes as unknown as CollectedDataType[];

describe('iOS privacy manifest', () => {
  it('app.json declares scan photos as not linked, not tracking, for app functionality', () => {
    const photos = appJsonTypes.find((t) => t.NSPrivacyCollectedDataType === `${T}PhotosorVideos`);

    expect(photos).toEqual({
      NSPrivacyCollectedDataType: `${T}PhotosorVideos`,
      NSPrivacyCollectedDataTypeLinked: false,
      NSPrivacyCollectedDataTypeTracking: false,
      NSPrivacyCollectedDataTypePurposes: [APP_FUNCTIONALITY],
    });
  });

  it('app.json declares exactly the audited data types', () => {
    expect(byType(appJsonTypes)).toEqual(byType(EXPECTED_TYPES));
  });

  it('app.json declares no tracking', () => {
    expect(manifest.NSPrivacyTracking).toBe(false);
    expect(manifest.NSPrivacyTrackingDomains).toEqual([]);
    expect(appJsonTypes.every((t) => t.NSPrivacyCollectedDataTypeTracking === false)).toBe(true);
  });

  it('the committed PrivacyInfo.xcprivacy declares the same data types as app.json', () => {
    // Order-sensitive: prebuild appends new types to the committed plist, so new
    // types go at the end of the app.json list (docs/claude/ios-ci.md).
    expect(plistTypes).toEqual(appJsonTypes);
    expect(byType(plistTypes)).toEqual(byType(EXPECTED_TYPES));
  });

  it('the committed PrivacyInfo.xcprivacy matches the whole app.json privacyManifests block', () => {
    // Order-sensitive too; see the note above.
    expect(plist).toEqual(manifest);
  });

  it('the plist reader rejects elements a privacy manifest does not use', () => {
    expect(() => parsePlist('<plist><integer>1</integer></plist>')).toThrow('unsupported');
  });
});
