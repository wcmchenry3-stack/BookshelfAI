import { useRef, useState } from 'react';
import { Alert } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useBanner } from './useBanner';
import { useOnlineOnly } from './useOnlineOnly';
import { api } from '../lib/api';
import { ifMatch, isVersionConflict } from '../lib/entryVersion';
import { isIdempotencyOutcomeUnknown } from '../lib/idempotency';
import { notifyLibraryChanged } from '../lib/libraryChanged';
import { openTrash } from '../lib/trash';

interface MutableBook {
  id: string;
  status: string;
  /** The entry's version as last read (#685), sent back as `If-Match`. */
  version?: number;
}

interface UseLibraryMutationsOptions<T extends MutableBook> {
  /** Which screen's copy to use for the error alerts. */
  namespace: 'my-books' | 'wishlist';
  /** The status the item moves to, or undefined when it cannot advance. */
  nextStatus: (item: T) => string | undefined;
  /** Called once the server confirmed the status change, with its response. */
  onAdvanced: (item: T, updated: T) => void;
  /** Called once the server confirmed the removal (the entry is in the trash). */
  onRemoved: (item: T) => void;
  /**
   * Called when the server refused the change because the entry changed on
   * another device (412, #685), after the list reload was requested.
   */
  onConflict?: (item: T) => void;
}

/**
 * Status-advance and remove mutations shared by My Books and Wishlist.
 *
 * The server is the source of truth, so the list only changes once a request
 * succeeds (via `onAdvanced` / `onRemoved`); the in-flight set just blocks
 * double-taps. `mutatingRef` is the actual guard — it's checked-and-set
 * synchronously, so two presses handled before a re-render can't both slip
 * through the way they could reading `mutatingIds` state directly.
 * `mutatingIds` just drives the disabled/opacity styling.
 *
 * Each write sends the entry's version as `If-Match` (#685). A 412 means the
 * entry changed on another device since this list was read: nothing was
 * written, so there is nothing to roll back; the "changed on another device"
 * banner shows, every library list reloads (`notifyLibraryChanged`) and the
 * write is not retried.
 */
export function useLibraryMutations<T extends MutableBook>({
  namespace,
  nextStatus,
  onAdvanced,
  onRemoved,
  onConflict,
}: UseLibraryMutationsOptions<T>) {
  const { t } = useTranslation(namespace);
  const mutatingRef = useRef<Set<string>>(new Set());
  const [mutatingIds, setMutatingIds] = useState<ReadonlySet<string>>(new Set());
  const { isConnected, requireOnline } = useOnlineOnly();
  const { showBanner } = useBanner();

  /** Atomically claims `id` for a mutation. Returns false if one is already in flight. */
  function beginMutating(id: string): boolean {
    if (mutatingRef.current.has(id)) return false;
    mutatingRef.current.add(id);
    setMutatingIds(new Set(mutatingRef.current));
    return true;
  }

  function endMutating(id: string) {
    mutatingRef.current.delete(id);
    setMutatingIds(new Set(mutatingRef.current));
  }

  /** Handles a 412, or a write applied without a response; false for any other failure. */
  function handledConflict(err: unknown, item: T): boolean {
    if (isIdempotencyOutcomeUnknown(err)) {
      // #685: the write was applied; reload the lists instead of an error.
      notifyLibraryChanged();
      return true;
    }
    if (!isVersionConflict(err)) return false;
    showBanner({ message: t('changedOnAnotherDevice', { ns: 'my-books' }), type: 'info' });
    notifyLibraryChanged();
    onConflict?.(item);
    return true;
  }

  async function advanceStatus(item: T) {
    const next = nextStatus(item);
    if (!next || !requireOnline() || !beginMutating(item.id)) return;
    try {
      const { data } = await api.patch<T>(
        `/user-books/${item.id}`,
        { status: next },
        ...ifMatch(item.version)
      );
      onAdvanced(item, data);
    } catch (err) {
      if (!handledConflict(err, item)) {
        Alert.alert(t('errorTitle', { ns: 'common' }), t('errorUpdateStatus'));
      }
    } finally {
      endMutating(item.id);
    }
  }

  /**
   * Resolves true once the server confirmed the removal, false otherwise
   * (offline, already in flight, a 412, or an error, each reported here), so a
   * caller such as the swipe row (#935) knows whether to close.
   */
  async function removeBook(item: T): Promise<boolean> {
    if (!requireOnline() || !beginMutating(item.id)) return false;
    try {
      await api.delete(`/user-books/${item.id}`, ...ifMatch(item.version));
      // A delete moves the entry to the trash (#683), restorable for 30 days.
      showBanner({
        message: t('movedToTrash', { ns: 'my-books' }),
        type: 'success',
        actions: [{ label: t('viewTrash', { ns: 'my-books' }), onPress: openTrash }],
      });
      onRemoved(item);
      return true;
    } catch (err) {
      if (!handledConflict(err, item)) {
        Alert.alert(t('errorTitle', { ns: 'common' }), t('errorRemoveBook'));
      }
      return false;
    } finally {
      endMutating(item.id);
    }
  }

  return { isConnected, mutatingIds, advanceStatus, removeBook };
}
