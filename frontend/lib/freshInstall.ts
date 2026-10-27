import { Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';

import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY } from './api';
import { Sentry } from './sentry';
import * as storage from './storage';

// First-run marker (#722). The iOS Keychain, where expo-secure-store keeps
// the tokens, survives an app uninstall, but AsyncStorage does not. The
// marker is not a credential, so AsyncStorage is fine for it (rule 6 covers
// tokens only).
//
// A missing marker alone does not mean a fresh install: every install from
// before this build has no marker either. So the decision uses all of
// AsyncStorage's keys:
// - marker present: a known install; nothing to do.
// - no keys at all: a fresh install. Tokens in the Keychain belong to a
//   previous install and are deleted.
// - other keys but no marker: an existing install upgrading to this build.
//   The marker is written and the tokens are kept.
// In a normal session the app writes `scan_job_queue` (lib/scanJobStorage.ts):
// ScanJobProvider persists the queue, even when it is empty, after it loads
// on every launch. Theme and language preferences are not in AsyncStorage.
// So an existing user whose AsyncStorage is entirely empty (never got past a
// first launch, or the queue was never written) is signed out once. That is
// acceptable and rare.
//
// One getAllKeys call answers both questions. AsyncStorage runs calls in the
// order they are issued, and AuthProvider starts this check while it renders,
// before any child effect (such as ScanJobProvider's) can write a key on a
// fresh install and make it look like an upgrade.
export const INSTALL_MARKER_KEY = 'bookshelf_install_marker';

async function runInstallCheck(): Promise<boolean> {
  if (Platform.OS === 'web') return false;

  let keys: readonly string[];
  try {
    keys = await AsyncStorage.getAllKeys();
  } catch {
    return false;
  }
  if (keys.includes(INSTALL_MARKER_KEY)) return false;

  if (keys.length > 0) {
    try {
      await AsyncStorage.setItem(INSTALL_MARKER_KEY, '1');
      Sentry.addBreadcrumb({
        category: 'auth',
        message: 'upgrade: install marker set',
        level: 'info',
      });
    } catch {
      // Tried again on the next launch, which still sees the other keys.
    }
    return false;
  }

  let hadTokens: boolean;
  try {
    const [access, refresh] = await Promise.all([
      storage.getItem(ACCESS_TOKEN_KEY),
      storage.getItem(REFRESH_TOKEN_KEY),
    ]);
    hadTokens = access !== null || refresh !== null;
    await storage.deleteItem(ACCESS_TOKEN_KEY);
    await storage.deleteItem(REFRESH_TOKEN_KEY);
  } catch {
    // Leave the marker unset so the wipe is tried again on the next launch.
    return false;
  }
  if (hadTokens) {
    Sentry.addBreadcrumb({
      category: 'auth',
      message: 'fresh install: cleared stale tokens',
      level: 'info',
    });
  }

  try {
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, '1');
  } catch {
    // The tokens are already gone; the next launch decides again.
  }
  return hadTokens;
}

let installCheck: Promise<boolean> | null = null;

/**
 * Once per app launch: on a fresh install, delete both tokens from
 * SecureStore and then write the install marker; on an upgrade, only write
 * the marker. Resolves to true when stale tokens were cleared. Repeat calls
 * share the first call's result.
 *
 * Native only: on web the token store is in memory, so nothing survives.
 * Fails safe toward keeping the session: if AsyncStorage cannot be read,
 * nothing is wiped. Never rejects.
 */
export function clearTokensIfFreshInstall(): Promise<boolean> {
  if (!installCheck) installCheck = runInstallCheck();
  return installCheck;
}

/** Test-only: forget the memoised result so the next call checks again. */
export function resetInstallCheckForTests(): void {
  installCheck = null;
}
