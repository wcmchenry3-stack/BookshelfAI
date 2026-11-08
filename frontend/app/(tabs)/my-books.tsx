import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, FlatList, StyleSheet, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { LoadingSpinner } from '../../components/LoadingSpinner';
import { OfflineNotice } from '../../components/OfflineNotice';
import { BookDetailSheet } from '../../components/library/BookDetailSheet';
import { BookActionsSheet } from '../../components/library/BookActionsSheet';
import { EmptyState } from '../../components/library/EmptyState';
import { LibraryHeader } from '../../components/library/LibraryHeader';
import { LibraryRow } from '../../components/library/LibraryRow';
import { LIBRARY_LIST_PROPS, libraryRefreshControl } from '../../components/library/libraryList';
import { LibraryScreen } from '../../components/library/LibraryScreen';
import { LibrarySearchBar } from '../../components/library/LibrarySearchBar';
import { StatusTabs } from '../../components/library/StatusTabs';
import { useLibraryAccent } from '../../components/library/useLibraryAccent';
import { NEXT_STATUS, SCREEN_PADDING, type Status } from '../../components/library/libraryShared';
import { IconButton } from '../../components/ui/IconButton';
import { useModalHandoff } from '../../hooks/useModalHandoff';
import { useLibraryMutations } from '../../hooks/useLibraryMutations';
import { onLibraryChanged } from '../../lib/libraryChanged';
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

interface MyBookRowProps {
  item: UserBook;
  onOpen: (item: UserBook) => void;
  onMore: (item: UserBook) => void;
}

/** One My Books row. Memoised; the callbacks are stable, so only a changed item re-renders it. */
const MyBookRow = memo(function MyBookRow({ item, onOpen, onMore }: MyBookRowProps) {
  const { t } = useTranslation('my-books');
  const status = t(`statusTab.${item.status}`);
  const a11yArgs = { title: item.book.title, status, author: item.book.author };
  return (
    <LibraryRow
      item={item}
      onPress={() => onOpen(item)}
      // This labels the row's tap target (the text is inert), so the Unverified
      // badge (#616) must be said here, and the status must be the translated one.
      accessibilityLabel={t(item.unverified ? 'bookCardUnverifiedA11y' : 'bookCardA11y', a11yArgs)}
      accessibilityHint={t('bookCardHint')}
      trailing={
        <IconButton
          icon="more-vert"
          accessibilityLabel={t('moreOptionsA11y', { title: item.book.title })}
          onPress={() => onMore(item)}
          testID={`book-more-${item.id}`}
        />
      }
    />
  );
});

export default function MyBooksScreen() {
  const { t } = useTranslation('my-books');
  const { activeColor } = useLibraryAccent();
  const [books, setBooks] = useState<UserBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState<Status>('all');
  const [selected, setSelected] = useState<UserBook | null>(null);
  // The book whose ⋯ menu is open.
  const [actionsFor, setActionsFor] = useState<UserBook | null>(null);
  // iOS cannot present a sheet while another is still dismissing: the detail sheet
  // waits for the menu to finish dismissing (see useModalHandoff).
  const menuGate = useModalHandoff(actionsFor !== null);
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
      setActionsFor(null);
    },
    onRemoved: (item) => {
      setBooks((prev) => prev.filter((b) => b.id !== item.id));
      setSelected(null);
      setActionsFor(null);
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
  const menuActionsDisabled =
    !isConnected || (actionsFor != null && mutatingIds.has(actionsFor.id));

  const openDetail = useCallback((item: UserBook) => setSelected(item), []);
  const openMenu = useCallback((item: UserBook) => {
    // A detail request left over from an earlier menu must never pop up later.
    setSelected(null);
    setActionsFor(item);
  }, []);
  const renderItem = useCallback(
    ({ item }: { item: UserBook }) => (
      <MyBookRow item={item} onOpen={openDetail} onMore={openMenu} />
    ),
    [openDetail, openMenu]
  );

  function viewDetailsFromMenu(item: UserBook) {
    setSelected(item);
    setActionsFor(null);
  }

  if (loading) {
    return (
      <LibraryScreen>
        <LoadingSpinner message={t('loading')} />
      </LibraryScreen>
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
    <LibraryScreen>
      <LibrarySearchBar value={query} onChangeText={setQuery} />
      <StatusTabs activeTab={activeTab} onChange={handleTabChange} />

      {!isConnected && <OfflineNotice />}

      {/* Library list */}
      {filteredBooks.length === 0 ? (
        <EmptyState message={t('noBooksYet')} />
      ) : (
        <FlatList
          data={filteredBooks}
          {...LIBRARY_LIST_PROPS}
          contentContainerStyle={styles.list}
          ListHeaderComponent={<View style={styles.header}>{listHeader}</View>}
          refreshControl={libraryRefreshControl(
            refreshing,
            setRefreshing,
            () => fetchBooks(activeTab),
            activeColor
          )}
          renderItem={renderItem}
        />
      )}

      <BookActionsSheet
        selected={actionsFor}
        actionsDisabled={menuActionsDisabled}
        onClose={() => setActionsFor(null)}
        onHidden={menuGate.notifyDismissed}
        onAdvance={advanceStatus}
        onViewDetails={viewDetailsFromMenu}
        onRemove={removeBook}
      />

      <BookDetailSheet
        selected={menuGate.clear ? selected : null}
        actionsDisabled={sheetActionsDisabled}
        onClose={() => setSelected(null)}
        onAdvance={advanceStatus}
        onRemove={removeBook}
      />
    </LibraryScreen>
  );
}

const styles = StyleSheet.create({
  list: { paddingBottom: 32 },
  header: { paddingHorizontal: SCREEN_PADDING },
});
