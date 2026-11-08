import { useCallback, useRef, useState, type ReactNode } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import { useFocusEffect } from 'expo-router';
import { useTranslation } from 'react-i18next';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { AppText } from '../components/ui/AppText';
import { EmptyState } from '../components/ui/EmptyState';
import { LibraryRow, LibraryRowSeparator } from '../components/library/LibraryRow';
import { TonalChip } from '../components/library/TonalChip';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { OfflineNotice } from '../components/OfflineNotice';
import { useBanner } from '../hooks/useBanner';
import { useOnlineOnly } from '../hooks/useOnlineOnly';
import { useTheme } from '../hooks/useTheme';
import { listTrash, restoreUserBook, type TrashedUserBook } from '../lib/api';
import { isServiceUnavailable } from '../lib/apiErrorReporting';
import { notifyLibraryChanged } from '../lib/libraryChanged';
import {
  daysUntilPurge,
  leaveTrash,
  PURGE_SOON_DAYS,
  restoreFailure,
  tidyLabel,
} from '../lib/trash';
import { fontFor } from '../theme/typography';

// The trash (#683): entries removed from the library, kept 30 days, newest
// deletion first. Restore returns an entry to the library; the library tabs
// reload whenever they regain focus, so they show it as soon as the user goes
// back. The screen has no Stack header: it owns its top inset (#936).
export default function TrashScreen() {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const { t } = useTranslation('my-books');
  const { showBanner } = useBanner();
  const { isConnected } = useOnlineOnly();
  const [items, setItems] = useState<TrashedUserBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  // "Today" for the days-left lines: read once per load, so every row in a render agrees.
  const [now, setNow] = useState(() => new Date());
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
      setNow(new Date());
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- showBanner and t are omitted on purpose: load is built once so the focus effect does not refire on banner or language changes
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

  // The compact top row (#932): Back on the left, the title centred, the same
  // width of nothing on the right so the title stays in the middle.
  const topBar = (
    <View style={[styles.topBar, { borderBottomColor: theme.colors.outline }]}>
      <View style={styles.topSide}>
        <Pressable
          onPress={leaveTrash}
          style={styles.back}
          accessibilityRole="button"
          accessibilityLabel={t('trash.back')}
          hitSlop={4}
        >
          <MaterialIcons name="chevron-left" size={24} color={theme.colors.primary} />
          <AppText variant="body" color="primary" style={styles.backText}>
            {t('trash.back')}
          </AppText>
        </Pressable>
      </View>
      <AppText variant="body" accessibilityRole="header" numberOfLines={1} style={styles.topTitle}>
        {t('trash.title')}
      </AppText>
      <View style={styles.topSide} />
    </View>
  );

  const infoCard = (
    <View style={[styles.infoCard, { backgroundColor: theme.colors.field }]}>
      <MaterialIcons
        name="info-outline"
        size={18}
        color={theme.colors.onSurfaceVariant}
        style={styles.infoIcon}
        accessibilityElementsHidden
        importantForAccessibility="no"
      />
      <AppText variant="secondary" color="onSurfaceVariant" style={styles.infoText}>
        {t('trash.intro')}
      </AppText>
    </View>
  );

  const frame = (children: ReactNode) => (
    <View
      style={[
        styles.container,
        { backgroundColor: theme.colors.background, paddingTop: insets.top },
      ]}
    >
      {topBar}
      {children}
    </View>
  );

  if (loading) {
    return frame(<LoadingSpinner message={t('trash.loading')} />);
  }

  if (loadError && items.length === 0) {
    return frame(
      <EmptyState
        testID="trash-error"
        title={t('trash.errorLoad')}
        body={loadError === 'unavailable' ? t('serviceUnavailable', { ns: 'common' }) : undefined}
        primaryAction={{
          label: t('trash.retry'),
          onPress: () => {
            setLoading(true);
            void load();
          },
        }}
      />
    );
  }

  return frame(
    <>
      {!isConnected && <OfflineNotice />}
      <FlatList
        testID="trash-list"
        data={items}
        keyExtractor={(item) => item.id}
        contentContainerStyle={styles.list}
        ListHeaderComponent={infoCard}
        ListEmptyComponent={
          <EmptyState
            testID="trash-empty"
            title={t('trash.emptyTitle')}
            body={t('trash.emptyBody')}
          />
        }
        ItemSeparatorComponent={LibraryRowSeparator}
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
          const days = daysUntilPurge(item.purge_after, now);
          const when =
            days === null
              ? undefined
              : days === 0
                ? t('trash.purgeToday')
                : t('trash.purgeIn', { count: days });
          return (
            <LibraryRow
              item={{ ...item, edition: null }}
              testID={`trash-item-${item.id}`}
              metaText={when ?? ''}
              metaTone={days !== null && days <= PURGE_SOON_DAYS ? 'danger' : 'default'}
              accessibilityLabel={tidyLabel(
                t(item.unverified ? 'trash.rowUnverifiedA11y' : 'trash.rowA11y', {
                  title: item.book.title,
                  author: item.book.author,
                  when: when ?? '',
                })
              )}
              trailing={
                <TonalChip
                  icon="restore"
                  label={t('trash.restore')}
                  accessibilityLabel={t('trash.restoreA11y', { title: item.book.title })}
                  onPress={() => handleRestore(item)}
                  disabled={disabled}
                />
              }
            />
          );
        }}
      />
    </>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  topBar: {
    flexDirection: 'row',
    alignItems: 'center',
    minHeight: 52,
    paddingHorizontal: 4,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  topSide: { flex: 1, flexDirection: 'row', alignItems: 'center' },
  back: {
    minHeight: 44,
    minWidth: 44,
    flexDirection: 'row',
    alignItems: 'center',
    paddingRight: 8,
  },
  backText: { fontFamily: fontFor('500') },
  topTitle: { fontFamily: fontFor('600'), fontSize: 17, lineHeight: 22, textAlign: 'center' },
  list: { paddingBottom: 32 },
  infoCard: {
    marginHorizontal: 16,
    marginTop: 16,
    marginBottom: 8,
    paddingVertical: 12,
    paddingHorizontal: 14,
    borderRadius: 12,
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: 10,
  },
  infoIcon: { marginTop: 1 },
  infoText: { flex: 1, lineHeight: 20 },
});
