import { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  FlatList,
  Image,
  Modal,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { LoadingSpinner } from '../../components/LoadingSpinner';
import { useTheme } from '../../hooks/useTheme';
import { api } from '../../lib/api';

type Status = 'all' | 'wishlisted' | 'purchased' | 'reading' | 'read';

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
  edition: {
    publish_year: number | null;
    publisher: string | null;
    page_count: number | null;
  } | null;
}

const STATUS_TAB_KEYS: Status[] = ['all', 'wishlisted', 'purchased', 'reading', 'read'];

const NEXT_STATUS: Record<string, string> = {
  wishlisted: 'purchased',
  purchased: 'reading',
  reading: 'read',
};

const SCREEN_PADDING = 16;

export default function MyBooksScreen() {
  const { theme } = useTheme();
  const { t } = useTranslation('my-books');
  const insets = useSafeAreaInsets();
  // Transparent header only in dark mode — push content below the floating header.
  // Standard nav bar height: 44px (iOS) / 56px (Android) + safe-area top inset.
  const headerHeight = Platform.OS === 'android' ? 56 : insets.top + 44;
  const topPad = theme.isDark ? headerHeight : 0;
  // Gold tertiary in dark mode, primary in light mode — active/CTA accent.
  const activeColor = theme.isDark ? theme.colors.tertiary : theme.colors.primary;
  const onActiveColor = theme.isDark ? theme.colors.onTertiary : theme.colors.onPrimary;
  const [books, setBooks] = useState<UserBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState<Status>('all');
  const [selected, setSelected] = useState<UserBook | null>(null);
  const [query, setQuery] = useState('');

  async function fetchBooks(tab: Status = activeTab) {
    try {
      const params = tab !== 'all' ? { status: tab } : {};
      const { data } = await api.get<UserBook[]>('/user-books', { params });
      setBooks(data.filter((b) => b.book != null));
    } catch {
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorLoadBooks'));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useFocusEffect(
    useCallback(() => {
      setLoading(true);
      fetchBooks(activeTab);
    }, [activeTab])
  );

  function handleTabChange(tab: Status) {
    setActiveTab(tab);
    setQuery('');
    setLoading(true);
    fetchBooks(tab);
  }

  async function handleAdvanceStatus(item: UserBook) {
    const next = NEXT_STATUS[item.status];
    if (!next) return;
    setBooks((prev) =>
      activeTab === 'all'
        ? prev.map((b) => (b.id === item.id ? { ...b, status: next } : b))
        : prev.filter((b) => b.id !== item.id)
    );
    setSelected(null);
    try {
      await api.patch(`/user-books/${item.id}`, { status: next });
    } catch {
      setBooks((prev) =>
        activeTab === 'all'
          ? prev.map((b) => (b.id === item.id ? { ...b, status: item.status } : b))
          : [...prev, item]
      );
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorUpdateStatus'));
    }
  }

  async function handleRemove(item: UserBook) {
    setBooks((prev) => prev.filter((b) => b.id !== item.id));
    setSelected(null);
    try {
      await api.delete(`/user-books/${item.id}`);
    } catch {
      setBooks((prev) => [...prev, item]);
      Alert.alert(t('errorTitle', { ns: 'common' }), t('errorRemoveBook'));
    }
  }

  const filteredBooks = useMemo(() => {
    if (!query.trim()) return books;
    const q = query.toLowerCase();
    return books.filter(
      (b) => b.book.title.toLowerCase().includes(q) || b.book.author.toLowerCase().includes(q)
    );
  }, [books, query]);

  if (loading) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        <LoadingSpinner message={t('loading')} />
      </View>
    );
  }

  const listHeader = (
    <View style={styles.listHeader}>
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
        {t('sectionTitle')}
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
      {/* Search bar — paddingTop offsets for transparent floating header in dark mode */}
      <View style={[styles.searchRow, { paddingTop: 12 + topPad }]}>
        <View
          style={[styles.searchWrap, { backgroundColor: theme.colors.surfaceContainerHighest }]}
        >
          <MaterialIcons
            name="search"
            size={20}
            color={theme.colors.outline}
            style={styles.searchIcon}
          />
          <TextInput
            style={[
              styles.searchInput,
              { color: theme.colors.onSurface, fontSize: theme.typography.fontSizeBase },
            ]}
            value={query}
            onChangeText={setQuery}
            placeholder={t('searchPlaceholder')}
            placeholderTextColor={theme.colors.outline}
            returnKeyType="search"
            accessibilityLabel={t('searchA11y')}
          />
        </View>
      </View>

      {/* Filter tabs */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.tabsContainer}
      >
        <View style={[styles.tabsTrack, { backgroundColor: theme.colors.surfaceContainerLow }]}>
          {STATUS_TAB_KEYS.map((key) => {
            const active = activeTab === key;
            const label = t(`statusTab.${key}`);
            return (
              <Pressable
                key={key}
                style={[styles.tab, active && { backgroundColor: activeColor }]}
                onPress={() => handleTabChange(key)}
                accessibilityRole="button"
                accessibilityLabel={t('filterBy', { label })}
                accessibilityState={{ selected: active }}
              >
                <Text
                  style={[
                    styles.tabText,
                    {
                      color: active ? onActiveColor : theme.colors.secondary,
                      fontSize: theme.typography.fontSizeSM,
                    },
                  ]}
                >
                  {label}
                </Text>
              </Pressable>
            );
          })}
        </View>
      </ScrollView>

      {/* Bento list */}
      {filteredBooks.length === 0 ? (
        <View style={styles.emptyContainer}>
          <Text
            style={[
              styles.emptyText,
              { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
            ]}
          >
            {t('noBooksYet')}
          </Text>
        </View>
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
                fetchBooks();
              }}
              tintColor={activeColor}
            />
          }
          renderItem={({ item }) => {
            const nextStatus = NEXT_STATUS[item.status];
            return (
              <Pressable
                style={[
                  styles.card,
                  {
                    backgroundColor: theme.colors.surfaceContainerLow,
                    borderLeftColor: activeColor,
                  },
                ]}
                onPress={() => setSelected(item)}
                accessibilityRole="button"
                accessibilityLabel={t('bookCardA11y', {
                  title: item.book.title,
                  status: item.status,
                })}
                accessibilityHint={t('bookCardHint')}
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
                  <Text
                    style={[
                      styles.categoryLabel,
                      { color: activeColor, fontSize: theme.typography.fontSizeXS },
                    ]}
                  >
                    {t(`status.${item.status}`)}
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
                        {
                          color: theme.colors.textSecondary,
                          fontSize: theme.typography.fontSizeXS,
                        },
                      ]}
                    >
                      {item.edition.publish_year}
                    </Text>
                  ) : null}

                  <View style={styles.actions}>
                    {nextStatus && (
                      <Pressable
                        style={[styles.actionButton, { backgroundColor: activeColor }]}
                        onPress={() => handleAdvanceStatus(item)}
                        accessibilityRole="button"
                        accessibilityLabel={t('markAsA11y', {
                          status: t(`status.${nextStatus}`),
                        })}
                      >
                        <Text
                          style={[
                            styles.actionText,
                            { fontSize: theme.typography.fontSizeXS, color: onActiveColor },
                          ]}
                        >
                          {t('markAs', { status: t(`status.${nextStatus}`) })}
                        </Text>
                      </Pressable>
                    )}
                    <Pressable
                      style={[
                        styles.actionButton,
                        { backgroundColor: theme.colors.secondaryContainer },
                      ]}
                      onPress={() => handleRemove(item)}
                      accessibilityRole="button"
                      accessibilityLabel={t('removeBookA11y', { title: item.book.title })}
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
                        {t('removeFromList')}
                      </Text>
                    </Pressable>
                  </View>
                </View>
              </Pressable>
            );
          }}
        />
      )}

      {/* Detail sheet — full book info, advance status, remove */}
      <Modal
        visible={!!selected}
        animationType="slide"
        presentationStyle="pageSheet"
        onRequestClose={() => setSelected(null)}
        accessibilityViewIsModal
      >
        {selected && (
          <View style={[styles.sheet, { backgroundColor: theme.colors.surface }]}>
            <View style={[styles.sheetHeader, { borderBottomColor: theme.colors.border }]}>
              <Text
                style={[
                  styles.sheetTitle,
                  { color: theme.colors.text, fontSize: theme.typography.fontSizeLG },
                ]}
                accessibilityRole="header"
                numberOfLines={2}
              >
                {selected.book.title}
              </Text>
              <Pressable
                onPress={() => setSelected(null)}
                accessibilityRole="button"
                accessibilityLabel={t('closeDetail')}
                style={styles.closeButton}
                hitSlop={8}
              >
                <Text style={{ color: activeColor, fontSize: theme.typography.fontSizeBase }}>
                  {t('close', { ns: 'common' })}
                </Text>
              </Pressable>
            </View>

            <ScrollView contentContainerStyle={styles.sheetContent}>
              {selected.book.cover_url && (
                <Image
                  source={{ uri: selected.book.cover_url }}
                  style={styles.sheetCover}
                  accessibilityLabel={t('coverAlt', { ns: 'common', title: selected.book.title })}
                />
              )}
              <Text
                style={[
                  styles.sheetAuthor,
                  { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
                ]}
              >
                {selected.book.author}
              </Text>
              {selected.book.description && (
                <Text
                  style={[
                    styles.sheetDescription,
                    { color: theme.colors.text, fontSize: theme.typography.fontSizeSM },
                  ]}
                >
                  {selected.book.description}
                </Text>
              )}
              {selected.edition?.page_count && (
                <Text
                  style={[
                    { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
                  ]}
                >
                  {t('pages', { count: selected.edition.page_count })}
                </Text>
              )}

              {NEXT_STATUS[selected.status] && (
                <Pressable
                  style={[styles.sheetButton, { backgroundColor: activeColor }]}
                  onPress={() => handleAdvanceStatus(selected)}
                  accessibilityRole="button"
                  accessibilityLabel={t('markAsA11y', {
                    status: t(`status.${NEXT_STATUS[selected.status]}`),
                  })}
                >
                  <Text
                    style={[
                      styles.sheetButtonText,
                      { fontSize: theme.typography.fontSizeBase, color: onActiveColor },
                    ]}
                  >
                    {t('markAs', { status: t(`status.${NEXT_STATUS[selected.status]}`) })}
                  </Text>
                </Pressable>
              )}

              <Pressable
                style={[styles.sheetButton, { backgroundColor: theme.colors.surfaceContainerHigh }]}
                onPress={() => handleRemove(selected)}
                accessibilityRole="button"
                accessibilityLabel={t('removeBookA11y', { title: selected.book.title })}
              >
                <Text
                  style={[
                    styles.sheetButtonText,
                    {
                      color: theme.colors.onSurfaceVariant,
                      fontSize: theme.typography.fontSizeBase,
                    },
                  ]}
                >
                  {t('removeFromList')}
                </Text>
              </Pressable>
            </ScrollView>
          </View>
        )}
      </Modal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },

  // Search
  searchRow: { paddingHorizontal: SCREEN_PADDING, paddingBottom: 4 },
  searchWrap: {
    flexDirection: 'row',
    alignItems: 'center',
    borderRadius: 12,
    paddingHorizontal: 12,
    height: 48,
  },
  searchIcon: { marginRight: 8 },
  searchInput: { flex: 1, height: '100%' },

  // Filter tabs
  tabsContainer: { paddingHorizontal: SCREEN_PADDING, paddingVertical: 8 },
  tabsTrack: {
    flexDirection: 'row',
    borderRadius: 12,
    padding: 4,
    gap: 2,
  },
  tab: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 10,
    minHeight: 36,
    justifyContent: 'center',
  },
  tabText: { fontWeight: '600' },

  // Bento list
  list: { paddingHorizontal: SCREEN_PADDING, paddingBottom: 32, gap: 12 },

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
  actionText: { fontWeight: '600' },

  // Empty
  emptyContainer: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 32 },
  emptyText: { textAlign: 'center' },

  // Detail sheet
  sheet: { flex: 1 },
  sheetHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 16,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  sheetTitle: { fontWeight: '700', flex: 1, marginRight: 8 },
  closeButton: { minWidth: 44, minHeight: 44, justifyContent: 'center', alignItems: 'flex-end' },
  sheetContent: { padding: 16, gap: 12, alignItems: 'center' },
  sheetCover: { width: 120, height: 180, borderRadius: 4 },
  sheetAuthor: { alignSelf: 'flex-start' },
  sheetDescription: { lineHeight: 20 },
  sheetButton: {
    width: '100%',
    paddingVertical: 14,
    borderRadius: 8,
    alignItems: 'center',
    minHeight: 44,
    justifyContent: 'center',
  },
  sheetButtonText: { fontWeight: '600' },
});
