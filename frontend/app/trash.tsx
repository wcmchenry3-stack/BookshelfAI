import { useCallback, useRef, useState } from 'react';
import {
  Alert,
  FlatList,
  Image,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';

import { LoadingSpinner } from '../components/LoadingSpinner';
import { OfflineNotice } from '../components/OfflineNotice';
import { UnverifiedBadge } from '../components/UnverifiedBadge';
import { useBanner } from '../hooks/useBanner';
import { useOnlineOnly } from '../hooks/useOnlineOnly';
import { useTheme } from '../hooks/useTheme';
import { listTrash, restoreUserBook, type TrashedUserBook } from '../lib/api';
import { isServiceUnavailable } from '../lib/apiErrorReporting';
import { notifyLibraryChanged } from '../lib/libraryChanged';
import { formatLocalDate, leaveTrash, restoreFailure } from '../lib/trash';

// The trash (#683): entries removed from the library, kept 30 days, newest
// deletion first. Restore returns an entry to the library; the library tabs
// reload whenever they regain focus, so they show it as soon as the user goes
// back. The root Stack's header is opaque for this screen (app/_layout.tsx).
export default function TrashScreen() {
  const { theme } = useTheme();
  const { t, i18n } = useTranslation('my-books');
  const { showBanner } = useBanner();
  const { isConnected } = useOnlineOnly();
  const [items, setItems] = useState<TrashedUserBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<'failed' | 'unavailable' | null>(null);
  // Same double-tap guard as My Books: `restoringRef` is checked and set
  // synchronously; `restoringIds` only drives the disabled styling.
  const restoringRef = useRef<Set<string>>(new Set());
  const [restoringIds, setRestoringIds] = useState<ReadonlySet<string>>(new Set());

  // The latest load wins, and an id restored in this session never comes back
  // from a load that was already in flight when the restore succeeded.
  const loadIdRef = useRef(0);
  const restoredRef = useRef<Set<string>>(new Set());
  const itemsRef = useRef<TrashedUserBook[]>([]);

  const load = useCallback(async () => {
    const loadId = ++loadIdRef.current;
    try {
      const data = await listTrash();
      // A newer load is under way and will set the state (and clear the flags).
      if (loadId !== loadIdRef.current) return;
      const next = data.filter((i) => i.book != null && !restoredRef.current.has(i.id));
      itemsRef.current = next;
      setItems(next);
      setLoadError(null);
    } catch (err) {
      if (loadId !== loadIdRef.current) return;
      if (itemsRef.current.length > 0) {
        // The list on screen stays; say that it could not be refreshed.
        showBanner({
          message: isServiceUnavailable(err)
            ? t('serviceUnavailable', { ns: 'common' })
            : t('trash.errorRefresh'),
          type: 'error',
        });
      } else {
        setLoadError(isServiceUnavailable(err) ? 'unavailable' : 'failed');
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load])
  );

  function setRestoring(id: string, on: boolean) {
    if (on) restoringRef.current.add(id);
    else restoringRef.current.delete(id);
    setRestoringIds(new Set(restoringRef.current));
  }

  async function handleRestore(item: TrashedUserBook) {
    if (restoringRef.current.has(item.id)) return;
    if (!isConnected) {
      showBanner({ message: t('requiresConnection', { ns: 'common' }), type: 'error' });
      return;
    }
    setRestoring(item.id, true);
    try {
      await restoreUserBook(item.id);
      restoredRef.current.add(item.id);
      itemsRef.current = itemsRef.current.filter((i) => i.id !== item.id);
      setItems(itemsRef.current);
      notifyLibraryChanged();
      showBanner({
        message: t('trash.restored', { title: item.book.title }),
        type: 'success',
        duration: 4000,
      });
    } catch (err) {
      const failure = restoreFailure(err);
      if (failure === 'not_found') void load();
      const message =
        failure === 'conflict'
          ? t('trash.restoreConflict')
          : failure === 'limit'
            ? t('addUnverified.limitReached', { ns: 'components' })
            : failure === 'not_found'
              ? t('trash.restoreNotFound')
              : failure === 'unavailable'
                ? t('serviceUnavailable', { ns: 'common' })
                : t('trash.restoreFailed');
      showBanner({ message, type: 'error' });
    } finally {
      setRestoring(item.id, false);
    }
  }

  const header = (
    <View style={styles.header}>
      <Pressable
        onPress={leaveTrash}
        style={styles.back}
        accessibilityRole="button"
        accessibilityLabel={t('trash.back')}
      >
        <Text
          style={[
            styles.backText,
            { color: theme.colors.primary, fontSize: theme.typography.fontSizeBase },
          ]}
        >
          {t('trash.back')}
        </Text>
      </Pressable>
      <Text
        style={[
          styles.title,
          {
            color: theme.colors.text,
            fontFamily: theme.typography.fontFamilyHeadline,
            fontSize: theme.typography.fontSizeH1,
          },
        ]}
        accessibilityRole="header"
      >
        {t('trash.title')}
      </Text>
      <Text
        style={[
          styles.intro,
          { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
        ]}
      >
        {t('trash.intro')}
      </Text>
    </View>
  );

  if (loading) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        <LoadingSpinner message={t('trash.loading')} />
      </View>
    );
  }

  if (loadError && items.length === 0) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        {header}
        <View style={styles.centered} testID="trash-error">
          <Text
            style={[
              styles.centeredText,
              { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
            ]}
          >
            {loadError === 'unavailable'
              ? t('serviceUnavailable', { ns: 'common' })
              : t('trash.errorLoad')}
          </Text>
          <Pressable
            onPress={() => {
              setLoading(true);
              void load();
            }}
            style={[styles.retry, { backgroundColor: theme.colors.primary }]}
            accessibilityRole="button"
            accessibilityLabel={t('trash.retry')}
          >
            <Text
              style={[
                styles.retryText,
                { color: theme.colors.onPrimary, fontSize: theme.typography.fontSizeBase },
              ]}
            >
              {t('trash.retry')}
            </Text>
          </Pressable>
        </View>
      </View>
    );
  }

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
      {!isConnected && <OfflineNotice />}
      <FlatList
        testID="trash-list"
        data={items}
        keyExtractor={(item) => item.id}
        contentContainerStyle={styles.list}
        ListHeaderComponent={header}
        ListEmptyComponent={
          <View style={styles.centered} testID="trash-empty">
            <Text
              style={[
                styles.emptyTitle,
                { color: theme.colors.text, fontSize: theme.typography.fontSizeLG },
              ]}
              accessibilityRole="header"
            >
              {t('trash.emptyTitle')}
            </Text>
            <Text
              style={[
                styles.centeredText,
                { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
              ]}
            >
              {t('trash.emptyBody')}
            </Text>
          </View>
        }
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => {
              setRefreshing(true);
              void load();
            }}
            tintColor={theme.colors.primary}
          />
        }
        renderItem={({ item }) => {
          const disabled = !isConnected || restoringIds.has(item.id);
          const dates = t('trash.dates', {
            deleted: formatLocalDate(item.deleted_at, i18n.language),
            purge: formatLocalDate(item.purge_after, i18n.language),
          });
          return (
            <View
              style={[styles.card, { backgroundColor: theme.colors.surfaceContainerLow }]}
              testID={`trash-item-${item.id}`}
            >
              {item.book.cover_url ? (
                <Image
                  source={{ uri: item.book.cover_url }}
                  style={styles.cover}
                  resizeMode="cover"
                  accessible={false}
                  importantForAccessibility="no"
                />
              ) : (
                <View
                  style={[
                    styles.cover,
                    styles.coverPlaceholder,
                    { backgroundColor: theme.colors.border },
                  ]}
                  accessible={false}
                  importantForAccessibility="no"
                />
              )}
              <View style={styles.content}>
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
                {item.unverified ? <UnverifiedBadge /> : null}
                <Text
                  style={[
                    styles.dates,
                    { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeXS },
                  ]}
                >
                  {dates}
                </Text>
                <Pressable
                  style={[
                    styles.restore,
                    { backgroundColor: theme.colors.primary },
                    disabled && styles.disabled,
                  ]}
                  onPress={() => handleRestore(item)}
                  disabled={disabled}
                  accessibilityRole="button"
                  accessibilityLabel={t('trash.restoreA11y', { title: item.book.title })}
                  accessibilityState={{ disabled }}
                >
                  <Text
                    style={[
                      styles.restoreText,
                      { color: theme.colors.onPrimary, fontSize: theme.typography.fontSizeSM },
                    ]}
                  >
                    {t('trash.restore')}
                  </Text>
                </Pressable>
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
  list: { paddingHorizontal: 16, paddingBottom: 32, gap: 12 },
  header: { gap: 6, marginBottom: 8 },
  back: { minHeight: 44, minWidth: 44, justifyContent: 'center', alignSelf: 'flex-start' },
  backText: { fontWeight: '600' },
  title: { fontWeight: '700', lineHeight: 48, letterSpacing: -0.5 },
  intro: { lineHeight: 20 },
  centered: { alignItems: 'center', justifyContent: 'center', padding: 32, gap: 12 },
  centeredText: { textAlign: 'center' },
  emptyTitle: { fontWeight: '700', textAlign: 'center' },
  retry: {
    minHeight: 44,
    paddingHorizontal: 24,
    borderRadius: 8,
    justifyContent: 'center',
    alignItems: 'center',
  },
  retryText: { fontWeight: '600' },
  card: { flexDirection: 'row', borderRadius: 12, overflow: 'hidden', minHeight: 120 },
  cover: { width: 90, alignSelf: 'stretch' },
  coverPlaceholder: { opacity: 0.4 },
  content: { flex: 1, padding: 12, gap: 4 },
  bookTitle: { fontWeight: '600', lineHeight: 22 },
  bookAuthor: { lineHeight: 18 },
  dates: { lineHeight: 16 },
  restore: {
    alignSelf: 'flex-start',
    minHeight: 44,
    paddingHorizontal: 16,
    borderRadius: 8,
    justifyContent: 'center',
    marginTop: 8,
  },
  restoreText: { fontWeight: '600' },
  disabled: { opacity: 0.5 },
});
