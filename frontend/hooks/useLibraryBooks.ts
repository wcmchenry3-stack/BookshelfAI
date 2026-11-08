import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert } from 'react-native';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { api } from '../lib/api';
import { isServiceUnavailable } from '../lib/apiErrorReporting';
import { onLibraryChanged } from '../lib/libraryChanged';

/**
 * The My Books list (#930): one unfiltered `GET /user-books`, fetched once per
 * focus, on `onLibraryChanged` (a Trash restore, #683) and on `reload()` (pull to
 * refresh). The status chips, their counts and the search are derived from it.
 *
 * Only the first load reports `loading`; later loads refresh in place, so the
 * search text and the chip stay on screen. Focus, a restore and a pull can
 * overlap, so only the newest request may apply its response. `loadError` is set
 * when the newest request failed (cleared by the next success).
 */
export function useLibraryBooks<T extends { book: object | null }>() {
  const { t } = useTranslation('my-books');
  const [books, setBooks] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const latest = useRef(0);
  const inFlight = useRef(0);

  const load = useCallback(async () => {
    const request = ++latest.current;
    inFlight.current += 1;
    try {
      const { data } = await api.get<T[]>('/user-books');
      if (request === latest.current) {
        setBooks(data.filter((b) => b.book != null));
        setLoadError(false);
      }
    } catch (err) {
      if (request === latest.current) {
        setLoadError(true);
        Alert.alert(
          t('errorTitle', { ns: 'common' }),
          isServiceUnavailable(err)
            ? t('serviceUnavailable', { ns: 'common' })
            : t('errorLoadBooks')
        );
      }
    } finally {
      inFlight.current -= 1;
      if (request === latest.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [t]);

  // The latest load, so the effects below stay stable instead of re-running
  // (and re-fetching) whenever `t` changes.
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });

  /**
   * Drops every response still in flight. Call it when a mutation the server has
   * confirmed is applied locally: a GET that started before it could otherwise
   * overwrite the change or bring a removed row back. If one was in flight (a
   * pull to refresh, a Trash restore), a fresh load replaces it so that refresh
   * still lands. It also clears a stale `loadError`.
   */
  const invalidate = useCallback(() => {
    const hadRequest = inFlight.current > 0;
    latest.current += 1;
    setLoadError(false);
    if (hadRequest) void loadRef.current();
    else setRefreshing(false);
  }, []);

  useFocusEffect(
    useCallback(() => {
      void loadRef.current();
    }, [])
  );
  useEffect(() => onLibraryChanged(() => void loadRef.current()), []);

  return {
    books,
    setBooks,
    loading,
    loadError,
    refreshing,
    setRefreshing,
    reload: load,
    invalidate,
  };
}
