import React, { useEffect, useState } from 'react';
import { Image, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useScanJobs } from '../hooks/useScanJobs';
import { useTheme } from '../hooks/useTheme';
import { leaveScanQueue } from '../lib/scanQueueNav';
import { formatLocalDateTime, formatLocalTime } from '../lib/scanErrors';
import { failedTimeOf, isPendingUpload, type ScanJob } from '../lib/scanJob';

// The scan queue (#605): photos waiting to be scanned, and photos that failed
// (retry or delete; removed automatically after FAILED_SCAN_RETENTION_MS).
// Text searches are listed too, with their query in place of a thumbnail.
export default function ScanQueueScreen() {
  const { theme } = useTheme();
  const { t, i18n } = useTranslation('scan');
  // Fixed at open: "Next try at" is shown only for a try still in the future.
  const [openedAt] = useState(() => Date.now());
  const { jobs, dismissJob, restartScanSchedule, purgeExpiredFailedJobs } = useScanJobs();

  // Remove failed scans older than 7 days each time the screen opens.
  useEffect(() => {
    purgeExpiredFailedJobs();
  }, [purgeExpiredFailedJobs]);

  const waiting = jobs.filter((j) => isPendingUpload(j) || j.status === 'searching');
  const failed = jobs.filter((j) => j.status === 'failed');

  function stateOf(job: ScanJob): string {
    if (job.status === 'searching') return t('queueStateSending');
    if (job.nextAttemptAt != null && job.nextAttemptAt > openedAt) {
      return t('queueStateNextTry', { time: formatLocalTime(job.nextAttemptAt, i18n.language) });
    }
    return t('queueStateWaiting');
  }

  function thumb(job: ScanJob) {
    if (job.imageUri) {
      return (
        <Image
          source={{ uri: job.imageUri }}
          style={[styles.thumb, { backgroundColor: theme.colors.surfaceContainerHighest }]}
          accessibilityRole="image"
          accessibilityLabel={t('queuePhotoA11y')}
        />
      );
    }
    return (
      <View style={[styles.thumb, { backgroundColor: theme.colors.surfaceContainerHighest }]}>
        <Text
          numberOfLines={3}
          style={[styles.queryText, { color: theme.colors.onSurfaceVariant }]}
        >
          {job.query}
        </Text>
      </View>
    );
  }

  return (
    <ScrollView
      style={{ backgroundColor: theme.colors.background }}
      contentContainerStyle={styles.content}
    >
      <Pressable
        onPress={leaveScanQueue}
        style={styles.back}
        accessibilityRole="button"
        accessibilityLabel={t('queueBack')}
      >
        <Text style={[styles.backText, { color: theme.colors.primary }]}>{t('queueBack')}</Text>
      </Pressable>
      <Text accessibilityRole="header" style={[styles.title, { color: theme.colors.onSurface }]}>
        {t('queueTitle')}
      </Text>

      <Text
        accessibilityRole="header"
        style={[styles.sectionTitle, { color: theme.colors.onSurface }]}
      >
        {t('queueWaitingTitle')}
      </Text>
      {waiting.length === 0 ? (
        <Text style={[styles.empty, { color: theme.colors.onSurfaceVariant }]}>
          {t('queueWaitingEmpty')}
        </Text>
      ) : (
        waiting.map((job) => (
          <View
            key={job.id}
            testID={`queue-waiting-${job.id}`}
            style={[styles.row, { backgroundColor: theme.colors.surfaceContainerLow }]}
          >
            {thumb(job)}
            <Text style={[styles.rowText, { color: theme.colors.onSurface }]}>{stateOf(job)}</Text>
          </View>
        ))
      )}

      <Text
        accessibilityRole="header"
        style={[styles.sectionTitle, { color: theme.colors.onSurface }]}
      >
        {t('queueFailedTitle')}
      </Text>
      <Text style={[styles.note, { color: theme.colors.onSurfaceVariant }]}>
        {t('queueFailedNote')}
      </Text>
      {failed.length === 0 ? (
        <Text style={[styles.empty, { color: theme.colors.onSurfaceVariant }]}>
          {t('queueFailedEmpty')}
        </Text>
      ) : (
        failed.map((job) => (
          <View
            key={job.id}
            testID={`queue-failed-${job.id}`}
            style={[styles.row, { backgroundColor: theme.colors.surfaceContainerLow }]}
          >
            {thumb(job)}
            <View style={styles.rowBody}>
              <Text style={[styles.rowText, { color: theme.colors.onSurface }]}>
                {t('queueFailedAt', {
                  time: formatLocalDateTime(failedTimeOf(job), i18n.language),
                })}
              </Text>
              <View style={styles.actions}>
                <Pressable
                  onPress={() => void restartScanSchedule(job.id)}
                  style={[styles.button, { backgroundColor: theme.colors.primary }]}
                  accessibilityRole="button"
                  accessibilityLabel={t('queueRetry')}
                >
                  <Text style={[styles.buttonText, { color: theme.colors.onPrimary }]}>
                    {t('queueRetry')}
                  </Text>
                </Pressable>
                <Pressable
                  onPress={() => dismissJob(job.id)}
                  style={[styles.button, { borderColor: theme.colors.outline, borderWidth: 1 }]}
                  accessibilityRole="button"
                  accessibilityLabel={t('queueDelete')}
                >
                  <Text style={[styles.buttonText, { color: theme.colors.onSurface }]}>
                    {t('queueDelete')}
                  </Text>
                </Pressable>
              </View>
            </View>
          </View>
        ))
      )}
    </ScrollView>
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
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    padding: 12,
    borderRadius: 12,
    marginBottom: 8,
  },
  rowBody: { flex: 1, gap: 8 },
  rowText: { flex: 1, fontSize: 14 },
  thumb: {
    width: 64,
    height: 64,
    borderRadius: 8,
    overflow: 'hidden',
    justifyContent: 'center',
    padding: 4,
  },
  queryText: { fontSize: 11 },
  actions: { flexDirection: 'row', gap: 8 },
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
