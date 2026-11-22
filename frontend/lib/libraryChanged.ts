/**
 * A tiny "the library changed" signal (#683). Screens that list library
 * entries (My Books, Wishlist) subscribe and reload; a screen that changes the
 * library from elsewhere (restoring from the Trash) fires it. Needed because
 * the focus reload can finish before a restore that was still in flight when
 * the user went back.
 */
type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe; returns the unsubscribe function. */
export function onLibraryChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function notifyLibraryChanged(): void {
  for (const listener of [...listeners]) listener();
}
