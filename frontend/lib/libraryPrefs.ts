import { getItem, setItem } from './storage';

/** Device-level My Books preferences: sort (#933) and view (#934). Wraps `lib/storage`. */
const LIBRARY_SORT_KEY = 'library.sort';
const LIBRARY_VIEW_KEY = 'library.view';

const LIBRARY_VIEWS = ['list', 'grid'] as const;
export type LibraryView = (typeof LIBRARY_VIEWS)[number];

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

/** The saved My Books view, or `null` when none is saved or the value is unknown. Never rejects. */
export function loadLibraryView(): Promise<LibraryView | null> {
  return readPref(LIBRARY_VIEW_KEY, LIBRARY_VIEWS);
}

/** Saves the My Books view. Never rejects. */
export function saveLibraryView(view: LibraryView): Promise<void> {
  return writePref(LIBRARY_VIEW_KEY, view);
}
