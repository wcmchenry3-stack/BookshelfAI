import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, FlatList, RefreshControl, StyleSheet, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { BookRow, BookRowAction } from '../../components/library/BookRow';
import { EmptyState } from '../../components/library/EmptyState';
import { LibraryHeader } from '../../components/library/LibraryHeader';
import { useLibraryAccent } from '../../components/library/useLibraryAccent';
import type { LibraryBook } from '../../components/library/libraryShared';
import { LoadingSpinner } from '../../components/LoadingSpinner';
import { OfflineNotice } from '../../components/OfflineNotice';
import { useLibraryMutations } from '../../hooks/useLibraryMutations';
import { onLibraryChanged } from '../../lib/libraryChanged';
import { useTheme } from '../../hooks/useTheme';
import { api } from '../../lib/api';
import { isServiceUnavailable } from '../../lib/apiErrorReporting';

export default function WishlistScreen() {
  const { theme } = useTheme();
  const { t } = useTranslation('wishlist');
  const { topPad, activeColor } = useLibraryAccent();
  const [books, setBooks] = useState<LibraryBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const { isConnected, mutatingIds, advanceStatus, removeBook } = useLibraryMutations<LibraryBook>({
    namespace: 'wishlist',
    nextStatus: () => 'purchased',
    // Only after the server confirms: the book is no longer wishlisted.
    onAdvanced: (item) => setBooks((prev) => prev.filter((b) => b.id !== item.id)),
    onRemoved: (item) => setBooks((prev) => prev.filter((b) => b.id !== item.id)),
  });

  async function fetchWishlist() {
    try {
      const { data } = await api.get<LibraryBook[]>('/user-books', {
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

  if (loading) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        <LoadingSpinner message={t('loading')} />
      </View>
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
    <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
      <FlatList
        data={books}
        keyExtractor={(item) => item.id}
        contentContainerStyle={[styles.list, { paddingTop: topPad }]}
        ListHeaderComponent={listHeader}
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
        renderItem={({ item }) => {
          const actionsDisabled = !isConnected || mutatingIds.has(item.id);
          return (
            <BookRow item={item} categoryLabel={t('categoryLabel')}>
              <BookRowAction
                variant="primary"
                label={t('markPurchased')}
                accessibilityLabel={t('markPurchasedA11y', { title: item.book.title })}
                onPress={() => advanceStatus(item)}
                disabled={actionsDisabled}
              />
              <BookRowAction
                variant="secondary"
                label={t('remove')}
                accessibilityLabel={t('removeA11y', { title: item.book.title })}
                onPress={() => removeBook(item)}
                disabled={actionsDisabled}
              />
            </BookRow>
          );
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  list: { padding: 16, gap: 12 },
});
