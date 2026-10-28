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
// - marker '1' (done): a known install; nothing to do.
// - marker 'pending', or any other value: an earlier wipe did not finish (or
//   the marker is corrupt). Wipe again (fail closed). This is checked before
//   the upgrade rule below, because by now the app has written other keys and
//   the install would otherwise look like an upgrade.
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
// When the wipe fails (SecureStore throws), the result is 'wipe_failed'.
// When getAllKeys or the marker read throws, a fresh install cannot be told
// from an existing one, so nothing is deleted, the marker is set to 'pending'
// (best effort) and the result is 'check_failed'. For both, AuthProvider ends
// the launch signed out and gates the request layer (lib/api.ts
// gateStoredSession), so the stored tokens are neither sent nor refreshed
// (fail closed). The 'pending' marker makes the next launch wipe before any
// token is used, even though ScanJobProvider writes `scan_job_queue` in
// between (which would otherwise make it look like an upgrade). An existing
// user therefore signs in again after a launch where AsyncStorage failed.
// A sign-out whose SecureStore delete fails sets 'pending' too
// (markWipePending), so the next launch wipes what was left behind.
//
// Known gaps, failing open and needing two storage failures in one launch:
// - writing 'pending' fails and the wipe then fails too;
// - the check fails and writing 'pending' fails too;
// - a sign-out delete fails and writing 'pending' fails too.
// In each, the next launch may treat the leftover tokens as this install's.
// If writing '1' fails after a good wipe, the marker stays 'pending' and the
// next launch wipes again (fail closed: at worst one more sign-in).
//
// Signing in during the launch supersedes the check (supersedeInstallCheck):
// - The check is capped at 5 s (AuthContext), so it may still be running when
//   the user signs in. Once superseded it starts no more token deletes and
//   writes no more 'pending' markers, and login waits for deletes already
//   started, so the new session's tokens are never deleted by it.
// - After login has written both new tokens, the marker is set to '1'
//   (markInstallDone): the previous install's tokens were overwritten, so the
//   next launch has nothing to wipe. Without this, a 'pending' left by a failed
//   check, wipe or sign-out would wipe the new session on the next launch.
// Marker writes run one at a time in the order they are issued, so a sign-out
// after that login still leaves 'pending' as the last write.
//
// The check starts with one getAllKeys call. AsyncStorage runs calls in the
// order they are issued, and AuthProvider starts this check while it renders,
// before any child effect (such as ScanJobProvider's) can write a key on a
// fresh install and make it look like an upgrade.
export const INSTALL_MARKER_KEY = 'bookshelf_install_marker';
export const MARKER_PENDING = 'pending';
export const MARKER_DONE = '1';

/**
 * - `no_change`: nothing was deleted (known install, upgrade, web, or a fresh
 *   install with no tokens). The stored tokens can be used.
 * - `cleared`: tokens left by a previous install were deleted.
 * - `wipe_failed`: a wipe was needed but SecureStore failed. The tokens may
 *   still be there, so this launch must be treated as signed out.
 * - `check_failed`: AsyncStorage could not be read, so the install state is
 *   unknown. Nothing was deleted; this launch must be treated as signed out.
 */
export type InstallCheckResult = 'no_change' | 'cleared' | 'wipe_failed' | 'check_failed';

/** Whether the stored tokens may be used after this install check. */
export function storedTokensUsable(result: InstallCheckResult): boolean {
  return result === 'no_change' || result === 'cleared';
}

// Set when a login supersedes this launch's install check.
let superseded = false;
// The check's token deletes, while they run; login waits for them.
let deletesInFlight: Promise<unknown> | null = null;
// Marker writes, run one at a time in the order issued.
let markerWrites: Promise<void> = Promise.resolve();

// Best effort, never rejects. `fromCheck` writes are skipped once a login has
// superseded the check.
function writeMarker(value: string, fromCheck: boolean): Promise<void> {
  markerWrites = markerWrites.then(async () => {
    if (fromCheck && superseded) return;
    try {
      await AsyncStorage.setItem(INSTALL_MARKER_KEY, value);
    } catch {
      // See the known gaps above.
    }
  });
  return markerWrites;
}

async function deleteBothTokens(): Promise<void> {
  await storage.deleteItem(ACCESS_TOKEN_KEY);
  await storage.deleteItem(REFRESH_TOKEN_KEY);
}

async function wipeTokens(): Promise<InstallCheckResult> {
  let hadTokens: boolean;
  try {
    const [access, refresh] = await Promise.all([
      storage.getItem(ACCESS_TOKEN_KEY),
      storage.getItem(REFRESH_TOKEN_KEY),
    ]);
    hadTokens = access !== null || refresh !== null;
    // A login took over while the tokens were read: the stored tokens are
    // now (or are about to be) the new session's, so delete nothing.
    if (superseded) return 'check_failed';
    deletesInFlight = deleteBothTokens();
    try {
      await deletesInFlight;
    } finally {
      deletesInFlight = null;
    }
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
  // If this fails, the marker stays 'pending' and the next launch wipes again.
  await writeMarker(MARKER_DONE, true);
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
    Sentry.addBreadcrumb({
      category: 'auth',
      message: 'install check: storage unreadable',
      level: 'warning',
    });
    await writeMarker(MARKER_PENDING, true);
    return 'check_failed';
  }

  if (marker === MARKER_DONE) return 'no_change';
  if (keys.includes(INSTALL_MARKER_KEY)) return wipeTokens();

  if (keys.length > 0) {
    // If this fails, the next launch still sees the other keys and retries.
    await writeMarker(MARKER_DONE, true);
    Sentry.addBreadcrumb({
      category: 'auth',
      message: 'upgrade: install marker set',
      level: 'info',
    });
    return 'no_change';
  }

  // Wipe even if this write fails; see the known gaps above.
  await writeMarker(MARKER_PENDING, true);
  return wipeTokens();
}

/**
 * Best effort: makes the next launch wipe the stored tokens before using
 * them. Used when this launch could not confirm or finish a wipe, or a
 * sign-out could not delete the tokens. Never rejects.
 */
export async function markWipePending(): Promise<void> {
  if (Platform.OS === 'web') return;
  await writeMarker(MARKER_PENDING, false);
}

/**
 * Called by login before it writes the new tokens. From now on this launch's
 * install check deletes nothing and writes no marker; resolves once any
 * token delete it already started has settled. Never rejects.
 */
export async function supersedeInstallCheck(): Promise<void> {
  superseded = true;
  if (deletesInFlight) await deletesInFlight.catch(() => {});
}

/**
 * Called by login after both new tokens are written: the previous install's
 * tokens are gone, so a 'pending' marker must not wipe the new session on the
 * next launch. Best effort, never rejects.
 */
export async function markInstallDone(): Promise<void> {
  if (Platform.OS === 'web') return;
  await writeMarker(MARKER_DONE, false);
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
  superseded = false;
  deletesInFlight = null;
  markerWrites = Promise.resolve();
}
