import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Alert, FlatList, Platform, RefreshControl, StyleSheet, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { BookDetailSheet, type DetailBook } from '../../components/library/BookDetailSheet';
import { BoughtChip } from '../../components/library/BoughtChip';
import { EmptyState } from '../../components/library/EmptyState';
import { LibraryHeader } from '../../components/library/LibraryHeader';
import { LibraryRow, LibraryRowSeparator } from '../../components/library/LibraryRow';
import { LibraryScreen } from '../../components/library/LibraryScreen';
import { useLibraryAccent } from '../../components/library/useLibraryAccent';
import { SCREEN_PADDING } from '../../components/library/libraryShared';
import { LoadingSpinner } from '../../components/LoadingSpinner';
import { OfflineNotice } from '../../components/OfflineNotice';
import { useLibraryMutations } from '../../hooks/useLibraryMutations';
import { onLibraryChanged } from '../../lib/libraryChanged';
import { useTheme } from '../../hooks/useTheme';
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
      accessibilityLabel={t('rowA11y', {
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
  const { theme } = useTheme();
  const { t } = useTranslation('wishlist');
  const { activeColor } = useLibraryAccent();
  const [books, setBooks] = useState<DetailBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selected, setSelected] = useState<DetailBook | null>(null);

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

  if (books.length === 0) {
    return (
      <EmptyState
        message={t('empty')}
        lineHeight={24}
        style={[styles.container, { backgroundColor: theme.colors.background }]}
      />
    );
  }

  const listHeader = (
    <LibraryHeader
      leading={!isConnected && <OfflineNotice />}
      title={t('title')}
      subtitle={t('heroSubtitle')}
      count={books.length}
      summaryLabel={t('archiveSummary')}
    />
  );

  return (
    <LibraryScreen>
      <FlatList
        data={books}
        keyExtractor={(item) => item.id}
        contentContainerStyle={styles.list}
        ListHeaderComponent={<View style={styles.header}>{listHeader}</View>}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => {
              setRefreshing(true);
              void fetchWishlist();
            }}
            tintColor={activeColor}
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
        ItemSeparatorComponent={LibraryRowSeparator}
        initialNumToRender={12}
        removeClippedSubviews={Platform.OS === 'android'}
      />

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
  container: { flex: 1 },
  list: { paddingVertical: 16 },
  header: { paddingHorizontal: SCREEN_PADDING },
});
