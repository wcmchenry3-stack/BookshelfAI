import React, { useEffect, useState } from 'react';
import { Alert, Image, Pressable, SectionList, StyleSheet, Text, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import { useTranslation } from 'react-i18next';

import { useScanJobs } from '../hooks/useScanJobs';
import { useTheme } from '../hooks/useTheme';
import { leaveScanQueue } from '../lib/scanQueueNav';
import { formatLocalDateTime, formatLocalTime } from '../lib/scanErrors';
import { failedTimeOf, isPendingUpload, type ScanJob } from '../lib/scanJob';

// How often "Next try at" is re-checked while the screen is open.
const CLOCK_TICK_MS = 30_000;

interface QueueSection {
  key: 'waiting' | 'failed';
  data: ScanJob[];
}

// The scan queue (#605): photos waiting to be scanned, and photos that failed
// (retry or delete; removed automatically after FAILED_SCAN_RETENTION_MS, which
// counts from `createdAt` for failed jobs saved before `failedAt` existed).
// Text searches are listed too, with their query in place of a thumbnail.
// The root Stack's header is made opaque for this screen in app/_layout.tsx,
// so the list starts below it in dark mode too.
export default function ScanQueueScreen() {
  const { theme } = useTheme();
  const { t, i18n } = useTranslation('scan');
  // A passed "Next try at" shows "Waiting for connection": re-check every 30 s.
  const [now, setNow] = useState(() => Date.now());
  const { jobs, dismissJob, restartScanSchedule, purgeExpiredFailedJobs } = useScanJobs();

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), CLOCK_TICK_MS);
    return () => clearInterval(id);
  }, []);

  // Remove failed scans older than 7 days each time the screen opens.
  useEffect(() => {
    purgeExpiredFailedJobs();
  }, [purgeExpiredFailedJobs]);

  const sections: QueueSection[] = [
    {
      key: 'waiting',
      data: jobs.filter((j) => isPendingUpload(j) || j.status === 'searching'),
    },
    { key: 'failed', data: jobs.filter((j) => j.status === 'failed') },
  ];

  function stateOf(job: ScanJob): string {
    if (job.status === 'searching') return t('queueStateSending');
    if (job.nextAttemptAt != null && job.nextAttemptAt > now) {
      return t('queueStateNextTry', { time: formatLocalTime(job.nextAttemptAt, i18n.language) });
    }
    return t('queueStateWaiting');
  }

  function confirmDelete(job: ScanJob) {
    Alert.alert(t('queueDeleteConfirm'), undefined, [
      { text: t('queueDeleteCancel'), style: 'cancel' },
      { text: t('queueDelete'), style: 'destructive', onPress: () => dismissJob(job.id) },
    ]);
  }

  function renderItem({ item: job, section }: { item: ScanJob; section: QueueSection }) {
    const isFailed = section.key === 'failed';
    const when = isFailed ? formatLocalDateTime(failedTimeOf(job), i18n.language) : '';
    const state = isFailed ? t('queueFailedAt', { time: when }) : stateOf(job);
    const what = job.imageUri ? t('queuePhotoA11y') : (job.query ?? '');
    return (
      <View
        testID={`queue-${section.key}-${job.id}`}
        style={[styles.row, { backgroundColor: theme.colors.surfaceContainerLow }]}
      >
        {/* One accessible item: the thumbnail and the state text read together. */}
        <View
          accessible
          accessibilityLabel={t('queueRowA11y', { what, state })}
          style={styles.info}
        >
          <Thumb job={job} />
          <Text style={[styles.rowText, { color: theme.colors.onSurface }]}>{state}</Text>
        </View>
        {isFailed ? (
          <View testID={`queue-actions-${job.id}`} style={styles.actions}>
            <Pressable
              onPress={() => void restartScanSchedule(job.id)}
              style={[styles.button, { backgroundColor: theme.colors.primary }]}
              accessibilityRole="button"
              accessibilityLabel={t('queueRetryA11y', { time: when })}
            >
              <Text style={[styles.buttonText, { color: theme.colors.onPrimary }]}>
                {t('queueRetry')}
              </Text>
            </Pressable>
            <Pressable
              onPress={() => confirmDelete(job)}
              style={[styles.button, { borderColor: theme.colors.outline, borderWidth: 1 }]}
              accessibilityRole="button"
              accessibilityLabel={t('queueDeleteA11y', { time: when })}
            >
              <Text style={[styles.buttonText, { color: theme.colors.onSurface }]}>
                {t('queueDelete')}
              </Text>
            </Pressable>
          </View>
        ) : null}
      </View>
    );
  }

  return (
    <SectionList<ScanJob, QueueSection>
      style={{ backgroundColor: theme.colors.background }}
      contentContainerStyle={styles.content}
      sections={sections}
      keyExtractor={(job) => job.id}
      stickySectionHeadersEnabled={false}
      renderItem={renderItem}
      ListHeaderComponent={
        <>
          <Pressable
            onPress={leaveScanQueue}
            style={styles.back}
            accessibilityRole="button"
            accessibilityLabel={t('queueBack')}
          >
            <Text style={[styles.backText, { color: theme.colors.primary }]}>{t('queueBack')}</Text>
          </Pressable>
          <Text
            accessibilityRole="header"
            style={[styles.title, { color: theme.colors.onSurface }]}
          >
            {t('queueTitle')}
          </Text>
        </>
      }
      renderSectionHeader={({ section }) => (
        <View>
          <Text
            accessibilityRole="header"
            style={[styles.sectionTitle, { color: theme.colors.onSurface }]}
          >
            {section.key === 'waiting' ? t('queueWaitingTitle') : t('queueFailedTitle')}
          </Text>
          {section.key === 'failed' ? (
            <Text style={[styles.note, { color: theme.colors.onSurfaceVariant }]}>
              {t('queueFailedNote')}
            </Text>
          ) : null}
        </View>
      )}
      renderSectionFooter={({ section }) =>
        section.data.length === 0 ? (
          <Text style={[styles.empty, { color: theme.colors.onSurfaceVariant }]}>
            {section.key === 'waiting' ? t('queueWaitingEmpty') : t('queueFailedEmpty')}
          </Text>
        ) : null
      }
    />
  );
}

// The photo, a neutral icon when its file can't be shown, or the query of a
// text search. Decorative: the row's label describes it.
function Thumb({ job }: { job: ScanJob }) {
  const { theme } = useTheme();
  const [broken, setBroken] = useState(false);
  const box = [styles.thumb, { backgroundColor: theme.colors.surfaceContainerHighest }];
  if (job.imageUri && !broken) {
    return (
      <Image
        testID={`queue-thumb-${job.id}`}
        source={{ uri: job.imageUri }}
        style={box}
        accessible={false}
        onError={() => setBroken(true)}
      />
    );
  }
  if (job.imageUri) {
    return (
      <View testID={`queue-thumb-missing-${job.id}`} style={[box, styles.thumbCenter]}>
        <MaterialIcons name="image-not-supported" size={28} color={theme.colors.onSurfaceVariant} />
      </View>
    );
  }
  return (
    <View style={box}>
      <Text numberOfLines={3} style={[styles.queryText, { color: theme.colors.onSurfaceVariant }]}>
        {job.query}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  content: { padding: 16, paddingBottom: 48 },
  back: { minHeight: 44, minWidth: 44, justifyContent: 'center', alignSelf: 'flex-start' },
  backText: { fontSize: 16, fontWeight: '600' },
  title: { fontSize: 24, fontFamily: 'NotoSerif_700Bold', marginBottom: 16 },
  sectionTitle: { fontSize: 18, fontWeight: '700', marginTop: 16, marginBottom: 8 },
  note: { fontSize: 13, marginBottom: 8 },
  empty: { fontSize: 14, paddingVertical: 8 },
  row: { gap: 12, padding: 12, borderRadius: 12, marginBottom: 8 },
  info: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  rowText: { flex: 1, fontSize: 14 },
  thumb: {
    width: 64,
    height: 64,
    borderRadius: 8,
    overflow: 'hidden',
    justifyContent: 'center',
    padding: 4,
  },
  thumbCenter: { alignItems: 'center' },
  queryText: { fontSize: 11 },
  actions: { flexDirection: 'row', gap: 16 },
  button: {
    minHeight: 44,
    minWidth: 44,
    paddingHorizontal: 16,
    borderRadius: 22,
    alignItems: 'center',
    justifyContent: 'center',
  },
  buttonText: { fontSize: 14, fontWeight: '600' },
});
