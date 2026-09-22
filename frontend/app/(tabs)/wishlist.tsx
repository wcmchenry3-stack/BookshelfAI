import { useCallback, useState } from 'react';
import {
  Alert,
  FlatList,
  Image,
  Platform,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { LoadingSpinner } from '../../components/LoadingSpinner';
import { OfflineNotice } from '../../components/OfflineNotice';
import { useOnlineOnly } from '../../hooks/useOnlineOnly';
import { useTheme } from '../../hooks/useTheme';
import { api } from '../../lib/api';

interface UserBook {
  id: string;
  status: string;
  book: {
    id: string;
    title: string;
    author: string;
    cover_url: string | null;
  };
  edition: {
    publish_year: number | null;
  } | null;
}

export default function WishlistScreen() {
  const { theme } = useTheme();
  const { t } = useTranslation('wishlist');
  const insets = useSafeAreaInsets();
  // Transparent header only in dark mode — push list content below it.
  // Standard nav bar height: 44px (iOS) / 56px (Android) + safe-area top inset.
  const headerHeight = Platform.OS === 'android' ? 56 : insets.top + 44;
  const topPad = theme.isDark ? headerHeight : 0;
  // Gold tertiary in dark mode, primary in light mode — active/CTA accent.
  const activeColor = theme.isDark ? theme.colors.tertiary : theme.colors.primary;
  const onActiveColor = theme.isDark ? theme.colors.onTertiary : theme.colors.onPrimary;
  const [books, setBooks] = useState<UserBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  // Rows with a request in flight. The server is the source of truth, so the
  // list only changes once a request succeeds; this just blocks double-taps.
  const [mutatingIds, setMutatingIds] = useState<ReadonlySet<string>>(new Set());
  const { isConnected, requireOnline } = useOnlineOnly();

  function setMutating(id: string, on: boolean) {
    setMutatingIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function fetchWishlist() {
    try {
      const { data } = await api.get<UserBook[]>('/user-books', {
        params: { status: 'wishlisted' },
      });
      setBooks(data);
    } catch {
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorLoadWishlist'));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useFocusEffect(
    useCallback(() => {
      setLoading(true);
      fetchWishlist();
    }, [])
  );

  async function handleMarkPurchased(item: UserBook) {
    if (mutatingIds.has(item.id) || !requireOnline()) return;
    setMutating(item.id, true);
    try {
      await api.patch(`/user-books/${item.id}`, { status: 'purchased' });
      // Only after the server confirms: the book is no longer wishlisted.
      setBooks((prev) => prev.filter((b) => b.id !== item.id));
    } catch {
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorUpdateStatus'));
    } finally {
      setMutating(item.id, false);
    }
  }

  async function handleRemove(item: UserBook) {
    if (mutatingIds.has(item.id) || !requireOnline()) return;
    setMutating(item.id, true);
    try {
      await api.delete(`/user-books/${item.id}`);
      setBooks((prev) => prev.filter((b) => b.id !== item.id));
    } catch {
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorRemoveBook'));
    } finally {
      setMutating(item.id, false);
    }
  }

  if (loading) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        <LoadingSpinner message={t('loading')} />
      </View>
    );
  }

  if (books.length === 0) {
    return (
      <View
        style={[
          styles.container,
          styles.emptyContainer,
          { backgroundColor: theme.colors.background },
        ]}
      >
        <Text
          style={[
            styles.emptyText,
            { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
          ]}
        >
          {t('empty')}
        </Text>
      </View>
    );
  }

  const listHeader = (
    <View style={styles.listHeader}>
      {!isConnected && <OfflineNotice />}
      {/* Hero */}
      <Text
        style={[
          styles.heroTitle,
          {
            color: theme.colors.text,
            fontFamily: theme.typography.fontFamilyHeadline,
            fontSize: theme.typography.fontSizeH1,
          },
        ]}
      >
        {t('title')}
      </Text>
      <Text
        style={[
          styles.heroSubtitle,
          { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
        ]}
      >
        {t('heroSubtitle')}
      </Text>

      {/* Archive summary card */}
      <View style={[styles.summaryCard, { backgroundColor: theme.colors.surfaceContainerHigh }]}>
        <View style={[styles.summaryAccent, { backgroundColor: activeColor }]} />
        <Text style={[styles.summaryCount, { color: theme.colors.text }]}>{books.length}</Text>
        <Text
          style={[
            styles.summaryLabel,
            { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
          ]}
        >
          {t('archiveSummary')}
        </Text>
      </View>
    </View>
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
              fetchWishlist();
            }}
            tintColor={activeColor}
          />
        }
        renderItem={({ item }) => {
          const actionsDisabled = !isConnected || mutatingIds.has(item.id);
          return (
            <View
              style={[
                styles.card,
                {
                  backgroundColor: theme.colors.surfaceContainerLow,
                  borderLeftColor: activeColor,
                },
              ]}
            >
              {/* Cover */}
              {item.book.cover_url ? (
                <Image
                  source={{ uri: item.book.cover_url }}
                  style={styles.cover}
                  resizeMode="cover"
                  accessibilityLabel={t('coverAlt', { ns: 'common', title: item.book.title })}
                />
              ) : (
                <View
                  style={[
                    styles.cover,
                    styles.coverPlaceholder,
                    { backgroundColor: theme.colors.border },
                  ]}
                  accessibilityLabel={t('noCoverAvailable', { ns: 'common' })}
                />
              )}

              {/* Content */}
              <View style={styles.content}>
                {/* Category label */}
                <Text
                  style={[
                    styles.categoryLabel,
                    { color: activeColor, fontSize: theme.typography.fontSizeXS },
                  ]}
                >
                  {t('categoryLabel')}
                </Text>

                <Text
                  style={[
                    styles.bookTitle,
                    { color: theme.colors.text, fontSize: theme.typography.fontSizeBase },
                  ]}
                  numberOfLines={2}
                >
                  {item.book.title}
                </Text>

                <Text
                  style={[
                    styles.bookAuthor,
                    { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
                  ]}
                  numberOfLines={1}
                >
                  {item.book.author}
                </Text>

                {item.edition?.publish_year ? (
                  <Text
                    style={[
                      styles.bookYear,
                      { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeXS },
                    ]}
                  >
                    {item.edition.publish_year}
                  </Text>
                ) : null}

                <View style={styles.actions}>
                  <Pressable
                    style={[
                      styles.actionButton,
                      { backgroundColor: activeColor },
                      actionsDisabled && styles.actionDisabled,
                    ]}
                    onPress={() => handleMarkPurchased(item)}
                    disabled={actionsDisabled}
                    accessibilityRole="button"
                    accessibilityLabel={t('markPurchasedA11y', { title: item.book.title })}
                    accessibilityState={{ disabled: actionsDisabled }}
                  >
                    <Text
                      style={[
                        styles.actionText,
                        { fontSize: theme.typography.fontSizeXS, color: onActiveColor },
                      ]}
                    >
                      {t('markPurchased')}
                    </Text>
                  </Pressable>
                  <Pressable
                    style={[
                      styles.actionButton,
                      { backgroundColor: theme.colors.secondaryContainer },
                      actionsDisabled && styles.actionDisabled,
                    ]}
                    onPress={() => handleRemove(item)}
                    disabled={actionsDisabled}
                    accessibilityRole="button"
                    accessibilityLabel={t('removeA11y', { title: item.book.title })}
                    accessibilityState={{ disabled: actionsDisabled }}
                  >
                    <Text
                      style={[
                        styles.actionText,
                        {
                          color: theme.colors.onSecondaryContainer,
                          fontSize: theme.typography.fontSizeXS,
                        },
                      ]}
                    >
                      {t('remove')}
                    </Text>
                  </Pressable>
                </View>
              </View>
            </View>
          );
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  list: { padding: 16, gap: 12 },
  emptyContainer: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 32 },
  emptyText: { textAlign: 'center', lineHeight: 24 },

  // Hero + summary
  listHeader: { marginBottom: 8, gap: 6 },
  heroTitle: {
    fontWeight: '700',
    lineHeight: 48,
    letterSpacing: -0.5,
  },
  heroSubtitle: { lineHeight: 20, marginBottom: 4 },
  summaryCard: {
    flexDirection: 'row',
    alignItems: 'center',
    borderRadius: 12,
    overflow: 'hidden',
    paddingHorizontal: 16,
    paddingVertical: 12,
    gap: 12,
    marginTop: 4,
  },
  summaryAccent: { width: 3, height: 32, borderRadius: 2 },
  summaryCount: { fontSize: 28, fontWeight: '700', lineHeight: 32 },
  summaryLabel: { flex: 1, lineHeight: 18 },

  // Bento card
  card: {
    flexDirection: 'row',
    borderRadius: 12,
    overflow: 'hidden',
    minHeight: 120,
    borderLeftWidth: 4,
  },
  cover: { width: 110, alignSelf: 'stretch' },
  coverPlaceholder: { opacity: 0.4 },
  content: { flex: 1, padding: 12, gap: 4 },
  categoryLabel: {
    textTransform: 'uppercase',
    letterSpacing: 1,
    fontWeight: '700',
    marginBottom: 2,
  },
  bookTitle: { fontWeight: '600', lineHeight: 22 },
  bookAuthor: { lineHeight: 18 },
  bookYear: { lineHeight: 16 },
  actions: { flexDirection: 'row', gap: 8, marginTop: 8, flexWrap: 'wrap' },
  actionButton: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 6,
    minHeight: 32,
    justifyContent: 'center',
  },
  actionDisabled: { opacity: 0.5 },
  actionText: { fontWeight: '600' },
});
