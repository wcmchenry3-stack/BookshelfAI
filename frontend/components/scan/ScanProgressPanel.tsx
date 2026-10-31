import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { openScanQueue } from '../../lib/scanQueueNav';
import { useTheme } from '../../hooks/useTheme';
import type { ScanUsage } from '../../hooks/useScanUsage';
import type { ScanNotice } from '../../lib/scanErrors';
import { ScanStatusPanel } from '../ScanStatusPanel';

interface Props {
  waitingCount: number;
  failedCount: number;
  inProgress: boolean;
  progressText: string;
  isConnected: boolean;
  activeColor: string;
  /** "Send now" on the pending badge. */
  onSendPending: () => void;
  usage: ScanUsage | null;
  notice: ScanNotice | null;
  onDismissNotice: () => void;
  onRetryNotice?: () => void;
}

/**
 * Everything the Scan screen shows under the mode toggle: the pending/failed
 * queue badge, the upload-progress live region and the quota/notice panel
 * (`components/ScanStatusPanel.tsx`).
 */
export function ScanProgressPanel({
  waitingCount,
  failedCount,
  inProgress,
  progressText,
  isConnected,
  activeColor,
  onSendPending,
  usage,
  notice,
  onDismissNotice,
  onRetryNotice,
}: Props) {
  const { theme } = useTheme();
  const { t } = useTranslation('scan');

  const pendingBadge =
    waitingCount > 0 ? (
      <View
        style={[styles.pendingBadge, { backgroundColor: theme.colors.surfaceContainerHighest }]}
        accessibilityRole="text"
        accessibilityLiveRegion="polite"
      >
        <Text style={[styles.pendingBadgeText, { color: theme.colors.onSurface }]}>
          {t('pendingUploads', { count: waitingCount })}
        </Text>
        <Pressable
          onPress={openScanQueue}
          style={styles.sendNow}
          accessibilityRole="button"
          accessibilityLabel={t('viewQueue')}
        >
          <Text style={[styles.sendNowText, { color: activeColor }]}>{t('viewQueue')}</Text>
        </Pressable>
        {isConnected ? (
          <Pressable
            onPress={onSendPending}
            style={styles.sendNow}
            accessibilityRole="button"
            accessibilityLabel={t('sendPendingNow')}
          >
            <Text style={[styles.sendNowText, { color: activeColor }]}>{t('sendPendingNow')}</Text>
          </Pressable>
        ) : null}
      </View>
    ) : failedCount > 0 ? (
      // Only failed scans are left: still a way into the queue (#605).
      <Pressable
        onPress={openScanQueue}
        style={[styles.sendNow, styles.failedLink]}
        accessibilityRole="button"
        accessibilityLabel={t('viewQueueFailed', { count: failedCount })}
      >
        <Text style={[styles.sendNowText, { color: activeColor }]}>
          {t('viewQueueFailed', { count: failedCount })}
        </Text>
      </Pressable>
    ) : null;

  return (
    <>
      {pendingBadge}
      {/* The live-region container stays mounted so Android announces its content. */}
      <View accessibilityLiveRegion="polite" testID="scan-progress-region">
        {inProgress ? (
          <View
            style={[styles.progressRow, { backgroundColor: theme.colors.surfaceContainerHighest }]}
            accessible
            accessibilityRole="progressbar"
            accessibilityLabel={progressText}
            testID="scan-progress"
          >
            <ActivityIndicator
              size="small"
              color={theme.colors.primary}
              importantForAccessibility="no"
            />
            <Text
              style={[styles.pendingBadgeText, { color: theme.colors.onSurface }]}
              importantForAccessibility="no"
            >
              {progressText}
            </Text>
          </View>
        ) : null}
      </View>
      <ScanStatusPanel
        usage={usage}
        notice={notice}
        onDismissNotice={onDismissNotice}
        onRetryNotice={onRetryNotice}
      />
    </>
  );
}

const styles = StyleSheet.create({
  progressRow: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'center',
    gap: 8,
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderRadius: 12,
    marginBottom: 8,
  },
  pendingBadge: {
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 6,
    marginBottom: 8,
    alignItems: 'center',
  },
  sendNow: { minHeight: 44, minWidth: 44, alignItems: 'center', justifyContent: 'center' },
  failedLink: { alignSelf: 'center', marginBottom: 8 },
  sendNowText: { fontWeight: '600', fontSize: 13 },
  pendingBadgeText: {
    fontWeight: '600',
    fontSize: 12,
  },
});
