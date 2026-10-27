import { Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';

import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY } from './api';
import { Sentry } from './sentry';
import * as storage from './storage';

// First-run marker (#722). The iOS Keychain, where expo-secure-store keeps
// the tokens, survives an app uninstall, but AsyncStorage does not. A missing
// marker therefore means a fresh install, and any tokens still in the
// Keychain belong to the previous install. The marker is not a credential,
// so AsyncStorage is fine for it (rule 6 covers tokens only).
export const INSTALL_MARKER_KEY = 'bookshelf_install_marker';

/**
 * On a fresh install, delete both tokens from SecureStore and then write the
 * install marker. Returns true when stale tokens were cleared.
 *
 * Native only: on web the token store is in memory, so nothing survives.
 * Fails safe toward keeping the session: if AsyncStorage cannot be read,
 * nothing is wiped. Never throws.
 */
export async function clearTokensIfFreshInstall(): Promise<boolean> {
  if (Platform.OS === 'web') return false;

  let marker: string | null;
  try {
    marker = await AsyncStorage.getItem(INSTALL_MARKER_KEY);
  } catch {
    return false;
  }
  if (marker !== null) return false;

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
    // The tokens are already gone; the next launch simply wipes again.
  }
  return hadTokens;
}
