import { useRef, useState } from 'react';
import { Alert } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useBanner } from './useBanner';
import { useOnlineOnly } from './useOnlineOnly';
import { api } from '../lib/api';
import { openTrash } from '../lib/trash';

interface MutableBook {
  id: string;
  status: string;
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
 */
export function useLibraryMutations<T extends MutableBook>({
  namespace,
  nextStatus,
  onAdvanced,
  onRemoved,
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

  async function advanceStatus(item: T) {
    const next = nextStatus(item);
    if (!next || !requireOnline() || !beginMutating(item.id)) return;
    try {
      const { data } = await api.patch<T>(`/user-books/${item.id}`, { status: next });
      onAdvanced(item, data);
    } catch {
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorUpdateStatus'));
    } finally {
      endMutating(item.id);
    }
  }

  async function removeBook(item: T) {
    if (!requireOnline() || !beginMutating(item.id)) return;
    try {
      await api.delete(`/user-books/${item.id}`);
      // A delete moves the entry to the trash (#683), restorable for 30 days.
      showBanner({
        message: t('movedToTrash', { ns: 'my-books' }),
        type: 'success',
        actions: [{ label: t('viewTrash', { ns: 'my-books' }), onPress: openTrash }],
      });
      onRemoved(item);
    } catch {
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorRemoveBook'));
    } finally {
      endMutating(item.id);
    }
  }

  return { isConnected, mutatingIds, advanceStatus, removeBook };
}
