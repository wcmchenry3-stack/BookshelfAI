import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Alert, FlatList, StyleSheet, View } from 'react-native';
import { useFocusEffect, useRouter } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { BookDetailSheet, type DetailBook } from '../../components/library/BookDetailSheet';
import { BoughtChip } from '../../components/library/BoughtChip';
import { filterBooks } from '../../components/library/filterBooks';
import { LibraryRow } from '../../components/library/LibraryRow';
import { LIBRARY_LIST_PROPS, libraryRefreshControl } from '../../components/library/libraryList';
import { LibraryScreen } from '../../components/library/LibraryScreen';
import { useLibraryAccent } from '../../components/library/useLibraryAccent';
import { SCREEN_PADDING } from '../../components/library/libraryShared';
import { WishlistEmptyIllustration } from '../../components/library/WishlistEmptyIllustration';
import { LoadingSpinner } from '../../components/LoadingSpinner';
import { OfflineNotice } from '../../components/OfflineNotice';
import { AppText } from '../../components/ui/AppText';
import { EmptyState } from '../../components/ui/EmptyState';
import { SearchField } from '../../components/ui/SearchField';
import { useLibraryMutations } from '../../hooks/useLibraryMutations';
import { onLibraryChanged } from '../../lib/libraryChanged';
import { api } from '../../lib/api';
import { isServiceUnavailable } from '../../lib/apiErrorReporting';

interface WishlistRowProps {
  item: DetailBook;
  disabled: boolean;
  onOpen: (item: DetailBook) => void;
  onBought: (item: DetailBook) => void;
}

/** One Wishlist row: opens the detail sheet; the "Bought" chip moves it to purchased. */
const WishlistRow = memo(function WishlistRow({
  item,
  disabled,
  onOpen,
  onBought,
}: WishlistRowProps) {
  const { t } = useTranslation('wishlist');
  return (
    <LibraryRow
      item={item}
      showStatus={false}
      onPress={() => onOpen(item)}
      // This labels the row's tap target (the text is inert), so the Unverified
      // badge (#616) must be said here too.
      accessibilityLabel={t(item.unverified ? 'rowUnverifiedA11y' : 'rowA11y', {
        title: item.book.title,
        status: t('statusTab.wishlisted', { ns: 'my-books' }),
        author: item.book.author,
      })}
      accessibilityHint={t('rowHint')}
      trailing={
        <BoughtChip
          label={t('bought')}
          accessibilityLabel={t('boughtA11y', { title: item.book.title })}
          onPress={() => onBought(item)}
          disabled={disabled}
          testID={`wishlist-bought-${item.id}`}
        />
      }
    />
  );
});

export default function WishlistScreen() {
  const router = useRouter();
  const { t } = useTranslation('wishlist');
  const { activeColor } = useLibraryAccent();
  const [books, setBooks] = useState<DetailBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selected, setSelected] = useState<DetailBook | null>(null);
  const [query, setQuery] = useState('');

  const { isConnected, mutatingIds, advanceStatus, removeBook } = useLibraryMutations<DetailBook>({
    namespace: 'wishlist',
    nextStatus: () => 'purchased',
    // Only after the server confirms: the book is no longer wishlisted.
    onAdvanced: (item) => {
      setBooks((prev) => prev.filter((b) => b.id !== item.id));
      setSelected(null);
    },
    onRemoved: (item) => {
      setBooks((prev) => prev.filter((b) => b.id !== item.id));
      setSelected(null);
    },
  });

  async function fetchWishlist() {
    try {
      const { data } = await api.get<DetailBook[]>('/user-books', {
        params: { status: 'wishlisted' },
      });
      setBooks(data);
    } catch (err) {
      Alert.alert(
        t('errorTitle', { ns: 'common' }),
        isServiceUnavailable(err)
          ? t('serviceUnavailable', { ns: 'common' })
          : t('errorLoadWishlist')
      );
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  // The latest fetchWishlist, so the effects below stay stable instead of
  // re-running (and re-fetching) whenever `t` or the closure changes.
  const fetchWishlistRef = useRef(fetchWishlist);
  useEffect(() => {
    fetchWishlistRef.current = fetchWishlist;
  });

  useFocusEffect(
    useCallback(() => {
      setLoading(true);
      void fetchWishlistRef.current();
    }, [])
  );

  // A restore from the Trash (#683) can finish after this screen's focus reload.
  useEffect(() => onLibraryChanged(() => void fetchWishlistRef.current()), []);

  // The latest advanceStatus, so the row callbacks below stay stable.
  const advanceRef = useRef(advanceStatus);
  useEffect(() => {
    advanceRef.current = advanceStatus;
  });
  const openDetail = useCallback((item: DetailBook) => setSelected(item), []);
  const markBought = useCallback((item: DetailBook) => void advanceRef.current(item), []);

  if (loading) {
    return (
      <LibraryScreen>
        <LoadingSpinner message={t('loading')} />
      </LibraryScreen>
    );
  }

  const visible = filterBooks(books, query);
  const isEmpty = books.length === 0;

  return (
    <LibraryScreen testID="wishlist-screen">
      <View style={styles.header}>
        <AppText variant="screenTitle" accessibilityRole="header">
          {t('title')}
        </AppText>
        <AppText variant="secondary" color="onSurfaceVariant" testID="wishlist-subtitle">
          {isEmpty ? t('subtitleEmpty') : t('subtitle', { count: books.length })}
        </AppText>
      </View>

      {!isEmpty && (
        <SearchField
          value={query}
          onChangeText={setQuery}
          placeholder={t('searchPlaceholder')}
          accessibilityLabel={t('searchA11y')}
          clearAccessibilityLabel={t('searchClearA11y')}
          testID="wishlist-search"
          style={styles.search}
        />
      )}

      {!isConnected && <OfflineNotice />}

      {isEmpty ? (
        <EmptyState
          style={styles.empty}
          testID="wishlist-empty"
          illustration={<WishlistEmptyIllustration />}
          title={t('emptyState.title')}
          body={t('emptyState.body')}
          primaryAction={{
            label: t('emptyState.scan'),
            onPress: () => router.push('/scan'),
            testID: 'wishlist-empty-scan',
          }}
          secondaryAction={{
            label: t('emptyState.search'),
            onPress: () => router.push('/scan?mode=text'),
            testID: 'wishlist-empty-search',
          }}
        />
      ) : (
        <FlatList
          data={visible}
          {...LIBRARY_LIST_PROPS}
          keyboardShouldPersistTaps="handled"
          contentContainerStyle={styles.list}
          refreshControl={libraryRefreshControl(
            refreshing,
            setRefreshing,
            () => fetchWishlist(),
            activeColor
          )}
          ListEmptyComponent={
            <EmptyState
              testID="wishlist-no-match"
              title={t('emptySearch.title', { query: query.trim() })}
              secondaryAction={{
                label: t('emptySearch.clear'),
                onPress: () => setQuery(''),
                testID: 'wishlist-clear-search',
              }}
            />
          }
          renderItem={({ item }) => (
            <WishlistRow
              item={item}
              disabled={!isConnected || mutatingIds.has(item.id)}
              onOpen={openDetail}
              onBought={markBought}
            />
          )}
        />
      )}

      <BookDetailSheet
        selected={selected}
        actionsDisabled={!isConnected || (selected != null && mutatingIds.has(selected.id))}
        onClose={() => setSelected(null)}
        onAdvance={advanceStatus}
        onRemove={removeBook}
      />
    </LibraryScreen>
  );
}

const styles = StyleSheet.create({
  header: { paddingHorizontal: SCREEN_PADDING, paddingTop: 8, gap: 2 },
  search: { marginHorizontal: SCREEN_PADDING, marginTop: 12, marginBottom: 4 },
  list: { paddingBottom: 16 },
  empty: { flex: 1, paddingHorizontal: SCREEN_PADDING + 16, paddingBottom: 40 },
});
