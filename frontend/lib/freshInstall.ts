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
// AsyncStorage's keys and the marker's value:
// - marker 'pending': an earlier wipe did not finish. Wipe again. This is
//   checked before the upgrade rule below, because by now the app has written
//   other keys and the install would otherwise look like an upgrade.
// - marker '1' (done): a known install; nothing to do.
// - no keys at all: a fresh install. Tokens in the Keychain belong to a
//   previous install. The marker is set to 'pending', both tokens are
//   deleted, and only then is the marker set to '1'.
// - other keys but no marker: an existing install upgrading to this build.
//   The marker is set to '1' and the tokens are kept.
// In a normal session the app writes `scan_job_queue` (lib/scanJobStorage.ts):
// ScanJobProvider persists the queue, even when it is empty, after it loads
// on every launch. Theme and language preferences are not in AsyncStorage.
// So an existing user whose AsyncStorage is entirely empty (never got past a
// first launch, or the queue was never written) is signed out once. That is
// acceptable and rare.
//
// When the wipe fails (SecureStore throws), the result is 'wipe_failed' and
// AuthProvider treats the launch as signed out without reading the tokens
// (fail closed); the 'pending' marker makes the next launch try again.
//
// Known gaps. Both fail open, and both need AsyncStorage itself to fail:
// - If getAllKeys (or reading the marker) throws, nothing is wiped, because a
//   fresh install cannot be told from an existing one.
// - If writing 'pending' fails and the wipe then fails too, the next launch
//   sees the queue key and takes the upgrade path.
// If writing '1' fails after a good wipe, the marker stays 'pending' and the
// next launch wipes again (fail closed: at worst one more sign-in).
//
// The check starts with one getAllKeys call. AsyncStorage runs calls in the
// order they are issued, and AuthProvider starts this check while it renders,
// before any child effect (such as ScanJobProvider's) can write a key on a
// fresh install and make it look like an upgrade.
export const INSTALL_MARKER_KEY = 'bookshelf_install_marker';
export const MARKER_PENDING = 'pending';
export const MARKER_DONE = '1';

/**
 * - `no_change`: nothing was deleted (known install, upgrade, web, fresh
 *   install with no tokens, or AsyncStorage unreadable).
 * - `cleared`: tokens left by a previous install were deleted.
 * - `wipe_failed`: a wipe was needed but SecureStore failed. The tokens may
 *   still be there, so this launch must be treated as signed out.
 */
export type InstallCheckResult = 'no_change' | 'cleared' | 'wipe_failed';

async function wipeTokens(): Promise<InstallCheckResult> {
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
    Sentry.addBreadcrumb({
      category: 'auth',
      message: 'fresh install: clearing stale tokens failed',
      level: 'warning',
    });
    return 'wipe_failed';
  }
  if (hadTokens) {
    Sentry.addBreadcrumb({
      category: 'auth',
      message: 'fresh install: cleared stale tokens',
      level: 'info',
    });
  }
  try {
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, MARKER_DONE);
  } catch {
    // The marker stays 'pending', so the next launch wipes again.
  }
  return hadTokens ? 'cleared' : 'no_change';
}

async function runInstallCheck(): Promise<InstallCheckResult> {
  if (Platform.OS === 'web') return 'no_change';

  let keys: readonly string[];
  let marker: string | null = null;
  try {
    keys = await AsyncStorage.getAllKeys();
    if (keys.includes(INSTALL_MARKER_KEY)) {
      marker = await AsyncStorage.getItem(INSTALL_MARKER_KEY);
    }
  } catch {
    return 'no_change';
  }

  if (marker === MARKER_PENDING) return wipeTokens();
  if (marker !== null) return 'no_change';

  if (keys.length > 0) {
    try {
      await AsyncStorage.setItem(INSTALL_MARKER_KEY, MARKER_DONE);
      Sentry.addBreadcrumb({
        category: 'auth',
        message: 'upgrade: install marker set',
        level: 'info',
      });
    } catch {
      // Tried again on the next launch, which still sees the other keys.
    }
    return 'no_change';
  }

  try {
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, MARKER_PENDING);
  } catch {
    // Wipe anyway; see the known gaps above.
  }
  return wipeTokens();
}

let installCheck: Promise<InstallCheckResult> | null = null;

/**
 * Once per app launch: on a fresh install (or after an unfinished wipe),
 * delete both tokens from SecureStore; on an upgrade, only write the install
 * marker. Repeat calls share the first call's result.
 *
 * Native only: on web the token store is in memory, so nothing survives.
 * Never rejects.
 */
export function clearTokensIfFreshInstall(): Promise<InstallCheckResult> {
  if (!installCheck) installCheck = runInstallCheck();
  return installCheck;
}

/** Test-only: forget the memoised result so the next call checks again. */
export function resetInstallCheckForTests(): void {
  installCheck = null;
}
