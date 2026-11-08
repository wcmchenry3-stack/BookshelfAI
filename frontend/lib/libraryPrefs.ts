import { getItem, setItem } from './storage';

/** Device-level My Books preferences (#933). Wraps `lib/storage`; #934 adds `library.view`. */
const LIBRARY_SORT_KEY = 'library.sort';

/** Reads a stored preference; a missing, unreadable or failing store gives `null`. */
async function readPref<T extends string>(key: string, allowed: readonly T[]): Promise<T | null> {
  try {
    const raw = await getItem(key);
    return allowed.find((v) => v === raw) ?? null;
  } catch {
    return null;
  }
}

/** Writes a preference; the choice still applies this session if the store fails. */
async function writePref(key: string, value: string): Promise<void> {
  try {
    await setItem(key, value);
  } catch {
    // Not persisted; nothing else depends on it.
  }
}

/** The saved sort, or `null` when none is saved or the value is unknown. Never rejects. */
export function loadLibrarySort<T extends string>(allowed: readonly T[]): Promise<T | null> {
  return readPref(LIBRARY_SORT_KEY, allowed);
}

/** Saves the sort. Never rejects. */
export function saveLibrarySort(sort: string): Promise<void> {
  return writePref(LIBRARY_SORT_KEY, sort);
}
