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
 * overlap, so only the newest request may apply its response.
 */
export function useLibraryBooks<T extends { book: object | null }>() {
  const { t } = useTranslation('my-books');
  const [books, setBooks] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const latest = useRef(0);

  const load = useCallback(async () => {
    const request = ++latest.current;
    try {
      const { data } = await api.get<T[]>('/user-books');
      if (request === latest.current) setBooks(data.filter((b) => b.book != null));
    } catch (err) {
      if (request === latest.current) {
        Alert.alert(
          t('errorTitle', { ns: 'common' }),
          isServiceUnavailable(err)
            ? t('serviceUnavailable', { ns: 'common' })
            : t('errorLoadBooks')
        );
      }
    } finally {
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

  useFocusEffect(
    useCallback(() => {
      void loadRef.current();
    }, [])
  );
  useEffect(() => onLibraryChanged(() => void loadRef.current()), []);

  return { books, setBooks, loading, refreshing, setRefreshing, reload: load };
}
