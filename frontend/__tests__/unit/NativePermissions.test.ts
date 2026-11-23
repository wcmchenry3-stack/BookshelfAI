/**
 * #1017: the app only needs the camera (cover scanning). The microphone, Face ID
 * and overlay permissions came from plugin defaults and the prebuild template:
 * - expo-camera adds RECORD_AUDIO and NSMicrophoneUsageDescription unless
 *   `recordAudioAndroid` / `microphonePermission` are false;
 * - expo-secure-store adds NSFaceIDUsageDescription unless `faceIDPermission`
 *   is false (we never use `requireAuthentication`);
 * - the Android template's main manifest declares SYSTEM_ALERT_WINDOW, which
 *   only the dev menu needs (it stays in `src/debug` and `src/debugOptimized`).
 * The committed native projects must match, since Xcode Cloud and the local
 * Gradle release build do not run prebuild.
 */
import fs from 'fs';
import path from 'path';

import appJson from '../../app.json';

type PluginEntry = string | [string, Record<string, unknown>?];

const root = path.join(__dirname, '../..');
const read = (rel: string) => fs.readFileSync(path.join(root, rel), 'utf8');

const UNUSED_ANDROID = [
  'android.permission.RECORD_AUDIO',
  'android.permission.SYSTEM_ALERT_WINDOW',
];
const UNUSED_IOS = ['NSMicrophoneUsageDescription', 'NSFaceIDUsageDescription'];

function pluginConfig(name: string): Record<string, unknown> {
  const plugins = appJson.expo.plugins as PluginEntry[];
  const entry = plugins.find((p) => (Array.isArray(p) ? p[0] : p) === name);
  return (Array.isArray(entry) && entry[1]) || {};
}

/** `uses-permission` names the manifest requests, ignoring `tools:node="remove"` blocks. */
function requestedPermissions(xml: string): string[] {
  return [...xml.matchAll(/<uses-permission\b[^>]*>/g)]
    .map((m) => m[0])
    .filter((tag) => !/tools:node="remove"/.test(tag))
    .map((tag) => /android:name="([^"]+)"/.exec(tag)?.[1] ?? '');
}

function blockedPermissions(xml: string): string[] {
  return [...xml.matchAll(/<uses-permission\b[^>]*>/g)]
    .map((m) => m[0])
    .filter((tag) => /tools:node="remove"/.test(tag))
    .map((tag) => /android:name="([^"]+)"/.exec(tag)?.[1] ?? '');
}

function plistKeys(xml: string): string[] {
  return [...xml.matchAll(/<key>([^<]+)<\/key>/g)].map((m) => m[1]);
}

describe('app.json native permissions', () => {
  it('turns off the expo-camera microphone and keeps the camera string', () => {
    const camera = pluginConfig('expo-camera');
    expect(camera.microphonePermission).toBe(false);
    expect(camera.recordAudioAndroid).toBe(false);
    expect(typeof camera.cameraPermission).toBe('string');
  });

  it('turns off the expo-secure-store Face ID string', () => {
    expect(pluginConfig('expo-secure-store').faceIDPermission).toBe(false);
  });

  it('requests CAMERA and blocks the unused Android permissions', () => {
    const android = appJson.expo.android as { permissions: string[]; blockedPermissions: string[] };
    expect(android.permissions).toContain('CAMERA');
    expect(android.blockedPermissions).toEqual(expect.arrayContaining(UNUSED_ANDROID));
  });
});

describe('committed Android main manifest', () => {
  const xml = read('android/app/src/main/AndroidManifest.xml');

  it('requests CAMERA', () => {
    expect(requestedPermissions(xml)).toContain('android.permission.CAMERA');
  });

  it.each(UNUSED_ANDROID)('does not request %s and removes it from libraries', (perm) => {
    expect(requestedPermissions(xml)).not.toContain(perm);
    expect(blockedPermissions(xml)).toContain(perm);
  });
});

describe('committed iOS Info.plist', () => {
  const keys = plistKeys(read('ios/BookShelfAI/Info.plist'));

  it('keeps the camera usage string', () => {
    expect(keys).toContain('NSCameraUsageDescription');
  });

  it.each(UNUSED_IOS)('has no %s', (key) => {
    expect(keys).not.toContain(key);
  });
});
