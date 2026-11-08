import React, { memo, useCallback, useMemo, useState } from 'react';
import { FlatList, StyleSheet, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { LoadingSpinner } from '../../components/LoadingSpinner';
import { OfflineNotice } from '../../components/OfflineNotice';
import { BookDetailSheet } from '../../components/library/BookDetailSheet';
import { BookActionsSheet } from '../../components/library/BookActionsSheet';
import { LibraryRow } from '../../components/library/LibraryRow';
import { LIBRARY_LIST_PROPS, libraryRefreshControl } from '../../components/library/libraryList';
import { LibraryScreen } from '../../components/library/LibraryScreen';
import { MyBooksHeader } from '../../components/library/MyBooksHeader';
import { StatusChips } from '../../components/library/StatusChips';
import { useLibraryAccent } from '../../components/library/useLibraryAccent';
import {
  NEXT_STATUS,
  SCREEN_PADDING,
  countByStatus,
  filterBooks,
  type Status,
} from '../../components/library/libraryShared';
import { EmptyState } from '../../components/ui/EmptyState';
import { IconButton } from '../../components/ui/IconButton';
import { SearchField } from '../../components/ui/SearchField';
import { useModalHandoff } from '../../hooks/useModalHandoff';
import { useLibraryBooks } from '../../hooks/useLibraryBooks';
import { useLibraryMutations } from '../../hooks/useLibraryMutations';

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
  const router = useRouter();
  const { activeColor } = useLibraryAccent();
  const { books, setBooks, loading, loadError, refreshing, setRefreshing, reload, invalidate } =
    useLibraryBooks<UserBook>();
  const [activeTab, setActiveTab] = useState<Status>('all');
  const [selected, setSelected] = useState<UserBook | null>(null);
  // The book whose ⋯ menu is open.
  const [actionsFor, setActionsFor] = useState<UserBook | null>(null);
  // iOS cannot present a sheet while another is still dismissing: the detail sheet
  // waits for the menu to finish dismissing (see useModalHandoff).
  const menuGate = useModalHandoff(actionsFor !== null);
  const [query, setQuery] = useState('');

  const { isConnected, mutatingIds, advanceStatus, removeBook } = useLibraryMutations<UserBook>({
    namespace: 'my-books',
    nextStatus: (item) => NEXT_STATUS[item.status],
    onAdvanced: (item, data) => {
      // Only after the server confirms. The chip filter is derived from `books`,
      // so a row that moved to another status just leaves the filtered view.
      invalidate();
      setBooks((prev) => prev.map((b) => (b.id === item.id ? { ...b, ...data } : b)));
      setSelected(null);
      setActionsFor(null);
    },
    onRemoved: (item) => {
      invalidate();
      setBooks((prev) => prev.filter((b) => b.id !== item.id));
      setSelected(null);
      setActionsFor(null);
    },
  });

  const counts = useMemo(() => countByStatus(books), [books]);
  const filteredBooks = useMemo(
    () => filterBooks(books, activeTab, query),
    [books, activeTab, query]
  );

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

  const hasBooks = books.length > 0;
  const trimmedQuery = query.trim();
  const clearSearch = () => setQuery('');
  const showAll = () => setActiveTab('all');

  // The three empty states (#930); the search field and chips stay above the first two.
  let empty: React.ReactElement;
  if (!hasBooks && loadError) {
    // A failed load is not an empty library: say so and offer a retry.
    empty = (
      <EmptyState
        testID="my-books-load-error"
        title={t('errorLoadBooks')}
        primaryAction={{
          label: t('loadErrorAction'),
          onPress: () => void reload(),
          testID: 'empty-retry',
        }}
        style={styles.empty}
      />
    );
  } else if (!hasBooks) {
    empty = (
      <EmptyState
        testID="my-books-empty-library"
        title={t('emptyLibraryTitle')}
        body={t('emptyLibraryBody')}
        primaryAction={{
          label: t('emptyLibraryAction'),
          onPress: () => router.navigate('/(tabs)/scan'),
          testID: 'empty-scan-books',
        }}
        style={styles.empty}
      />
    );
  } else if (trimmedQuery) {
    empty = (
      <EmptyState
        testID="my-books-empty-search"
        title={t('emptySearchTitle', { query: trimmedQuery })}
        body={t('emptySearchBody')}
        primaryAction={{
          label: t('emptySearchAction'),
          onPress: clearSearch,
          testID: 'empty-clear-search',
        }}
        style={styles.empty}
      />
    );
  } else {
    empty = (
      <EmptyState
        testID="my-books-empty-filter"
        title={t(`emptyFilter.${activeTab}`)}
        body={t('emptyFilterBody')}
        primaryAction={{
          label: t('emptyFilterAction'),
          onPress: showAll,
          testID: 'empty-show-all',
        }}
        style={styles.empty}
      />
    );
  }

  const listHeader = (
    <View>
      <MyBooksHeader total={books.length} reading={counts.reading} />
      {hasBooks ? (
        <>
          <SearchField
            value={query}
            onChangeText={setQuery}
            placeholder={t('searchPlaceholder')}
            accessibilityLabel={t('searchA11y')}
            clearAccessibilityLabel={t('searchClearA11y')}
            testID="my-books-search"
            style={styles.search}
          />
          <StatusChips activeTab={activeTab} counts={counts} onChange={setActiveTab} />
        </>
      ) : null}
    </View>
  );

  return (
    <LibraryScreen>
      {!isConnected && <OfflineNotice />}

      {/* One list for every state, so the header scrolls with the rows and pull to refresh always works. */}
      <FlatList
        data={filteredBooks}
        {...LIBRARY_LIST_PROPS}
        keyboardShouldPersistTaps="handled"
        contentContainerStyle={styles.list}
        ListHeaderComponent={listHeader}
        ListEmptyComponent={empty}
        refreshControl={libraryRefreshControl(refreshing, setRefreshing, reload, activeColor)}
        renderItem={renderItem}
      />

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
  search: { marginHorizontal: SCREEN_PADDING, marginTop: 12 },
  empty: { paddingTop: 48 },
});
