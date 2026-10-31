import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, FlatList, RefreshControl, StyleSheet, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { LoadingSpinner } from '../../components/LoadingSpinner';
import { OfflineNotice } from '../../components/OfflineNotice';
import { BookDetailSheet } from '../../components/library/BookDetailSheet';
import { BookRow, BookRowAction } from '../../components/library/BookRow';
import { EmptyState } from '../../components/library/EmptyState';
import { LibraryHeader } from '../../components/library/LibraryHeader';
import { LibrarySearchBar } from '../../components/library/LibrarySearchBar';
import { StatusTabs } from '../../components/library/StatusTabs';
import { useLibraryAccent } from '../../components/library/useLibraryAccent';
import { NEXT_STATUS, SCREEN_PADDING, type Status } from '../../components/library/libraryShared';
import { useLibraryMutations } from '../../hooks/useLibraryMutations';
import { onLibraryChanged } from '../../lib/libraryChanged';
import { useTheme } from '../../hooks/useTheme';
import { api } from '../../lib/api';
import { isServiceUnavailable } from '../../lib/apiErrorReporting';

interface UserBook {
  id: string;
  status: string;
  rating: number | null;
  notes: string | null;
  wishlisted_at: string | null;
  purchased_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  book: {
    id: string;
    title: string;
    author: string;
    cover_url: string | null;
    description: string | null;
  };
  /** A book the user added themselves; the catalog could not find it (#616). */
  unverified?: boolean;
  edition: {
    publish_year: number | null;
    publisher: string | null;
    page_count: number | null;
  } | null;
}

export default function MyBooksScreen() {
  const { theme } = useTheme();
  const { t } = useTranslation('my-books');
  const { activeColor } = useLibraryAccent();
  const [books, setBooks] = useState<UserBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState<Status>('all');
  const [selected, setSelected] = useState<UserBook | null>(null);
  const [query, setQuery] = useState('');

  // Read inside the async mutation handlers below, so a tab switch while a
  // request is in flight is picked up instead of applying a stale tab's rule
  // to whatever list is on screen by the time the server responds.
  const activeTabRef = useRef(activeTab);
  useEffect(() => {
    activeTabRef.current = activeTab;
  }, [activeTab]);

  const { isConnected, mutatingIds, advanceStatus, removeBook } = useLibraryMutations<UserBook>({
    namespace: 'my-books',
    nextStatus: (item) => NEXT_STATUS[item.status],
    onAdvanced: (item, data) => {
      // Only after the server confirms, and against whatever tab is current —
      // not the tab that was active when the button was pressed, which the
      // user may have since left (fetchBooks would have already replaced
      // `books` with that tab's data).
      // Keep (and update) the row when it still belongs to the current tab —
      // "all", or the tab for its new status, whose GET may already have
      // returned it if the user switched there while the PATCH was in flight.
      setBooks((prev) => {
        if (!prev.some((b) => b.id === item.id)) return prev;
        const tab = activeTabRef.current;
        return tab === 'all' || tab === data.status
          ? prev.map((b) => (b.id === item.id ? { ...b, ...data } : b))
          : prev.filter((b) => b.id !== item.id);
      });
      setSelected(null);
    },
    onRemoved: (item) => {
      setBooks((prev) => prev.filter((b) => b.id !== item.id));
      setSelected(null);
    },
  });

  async function fetchBooks(tab: Status) {
    try {
      const params = tab !== 'all' ? { status: tab } : {};
      const { data } = await api.get<UserBook[]>('/user-books', { params });
      setBooks(data.filter((b) => b.book != null));
    } catch (err) {
      Alert.alert(
        t('errorTitle', { ns: 'common' }),
        isServiceUnavailable(err) ? t('serviceUnavailable', { ns: 'common' }) : t('errorLoadBooks')
      );
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  // The latest fetchBooks, so the effects below stay stable instead of
  // re-running (and re-fetching) whenever `t` or the closure changes.
  const fetchBooksRef = useRef(fetchBooks);
  useEffect(() => {
    fetchBooksRef.current = fetchBooks;
  });

  useFocusEffect(
    useCallback(() => {
      setLoading(true);
      void fetchBooksRef.current(activeTab);
    }, [activeTab])
  );

  // A restore from the Trash (#683) can finish after this screen's focus reload.
  useEffect(() => onLibraryChanged(() => void fetchBooksRef.current(activeTabRef.current)), []);

  function handleTabChange(tab: Status) {
    setActiveTab(tab);
    setQuery('');
    setLoading(true);
    void fetchBooks(tab);
  }

  const filteredBooks = useMemo(() => {
    if (!query.trim()) return books;
    const q = query.toLowerCase();
    return books.filter(
      (b) => b.book.title.toLowerCase().includes(q) || b.book.author.toLowerCase().includes(q)
    );
  }, [books, query]);

  const sheetActionsDisabled = !isConnected || (selected != null && mutatingIds.has(selected.id));

  if (loading) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        <LoadingSpinner message={t('loading')} />
      </View>
    );
  }

  const listHeader = (
    <LibraryHeader
      title={t('sectionTitle')}
      subtitle={t('heroSubtitle')}
      count={books.length}
      summaryLabel={t('archiveSummary')}
    />
  );

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
      <LibrarySearchBar value={query} onChangeText={setQuery} />
      <StatusTabs activeTab={activeTab} onChange={handleTabChange} />

      {!isConnected && <OfflineNotice />}

      {/* Bento list */}
      {filteredBooks.length === 0 ? (
        <EmptyState message={t('noBooksYet')} />
      ) : (
        <FlatList
          data={filteredBooks}
          keyExtractor={(item) => item.id}
          contentContainerStyle={styles.list}
          ListHeaderComponent={listHeader}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => {
                setRefreshing(true);
                void fetchBooks(activeTab);
              }}
              tintColor={activeColor}
            />
          }
          renderItem={({ item }) => {
            const nextStatus = NEXT_STATUS[item.status];
            const actionsDisabled = !isConnected || mutatingIds.has(item.id);
            return (
              <BookRow
                item={item}
                categoryLabel={t(`status.${item.status}`)}
                onPress={() => setSelected(item)}
                // This label replaces the card's children for a screen reader,
                // so the Unverified badge (#616) must be said here.
                accessibilityLabel={t(item.unverified ? 'bookCardUnverifiedA11y' : 'bookCardA11y', {
                  title: item.book.title,
                  status: item.status,
                })}
                accessibilityHint={t('bookCardHint')}
              >
                {nextStatus && (
                  <BookRowAction
                    variant="primary"
                    label={t('markAs', { status: t(`status.${nextStatus}`) })}
                    accessibilityLabel={t('markAsA11y', { status: t(`status.${nextStatus}`) })}
                    onPress={() => advanceStatus(item)}
                    disabled={actionsDisabled}
                  />
                )}
                <BookRowAction
                  variant="secondary"
                  label={t('removeFromList')}
                  accessibilityLabel={t('removeBookA11y', { title: item.book.title })}
                  onPress={() => removeBook(item)}
                  disabled={actionsDisabled}
                />
              </BookRow>
            );
          }}
        />
      )}

      <BookDetailSheet
        selected={selected}
        actionsDisabled={sheetActionsDisabled}
        onClose={() => setSelected(null)}
        onAdvance={advanceStatus}
        onRemove={removeBook}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  list: { paddingHorizontal: SCREEN_PADDING, paddingBottom: 32, gap: 12 },
});
