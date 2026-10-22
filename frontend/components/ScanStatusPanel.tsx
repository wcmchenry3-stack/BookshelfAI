import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../hooks/useTheme';
import type { ScanUsage } from '../hooks/useScanUsage';
import { scanNoticeMessage, type ScanNotice } from '../lib/scanErrors';

/** Scans left today: the tighter of the daily and the monthly allowance. */
export function remainingToday(usage: ScanUsage): number {
  return Math.max(
    0,
    Math.min(usage.daily_limit - usage.used_today, usage.monthly_limit - usage.used_month)
  );
}

interface Props {
  usage: ScanUsage | null;
  notice: ScanNotice | null;
  onDismissNotice: () => void;
  /** Present when the refused scan can be retried; shows "Try again". */
  onRetryNotice?: () => void;
}

/**
 * Usage counter ("X of Y scans used this month", scans left today) plus the
 * refusal notice (monthly/daily limit, still processing, paused). Each part
 * renders only when it has data, so an offline or failed usage request just
 * hides the counter.
 */
export function ScanStatusPanel({ usage, notice, onDismissNotice, onRetryNotice }: Props) {
  const { theme } = useTheme();
  const { t, i18n } = useTranslation('scan');
  if (!usage && !notice) return null;

  return (
    <View style={styles.wrap}>
      {notice ? (
        <View
          style={[styles.card, { backgroundColor: theme.colors.surfaceContainerHighest }]}
          accessibilityRole="alert"
          accessibilityLiveRegion="polite"
          testID="scan-notice"
        >
          <Text style={[styles.noticeText, { color: theme.colors.onSurface }]}>
            {scanNoticeMessage(notice, t, i18n.language)}
          </Text>
          <View style={styles.actions}>
            {onRetryNotice ? (
              <Pressable
                onPress={onRetryNotice}
                style={styles.dismiss}
                accessibilityRole="button"
                accessibilityLabel={t('tryAgainNotice')}
              >
                <Text style={[styles.dismissText, { color: theme.colors.primary }]}>
                  {t('tryAgainNotice')}
                </Text>
              </Pressable>
            ) : null}
            <Pressable
              onPress={onDismissNotice}
              style={styles.dismiss}
              accessibilityRole="button"
              accessibilityLabel={t('dismissNotice')}
            >
              <Text style={[styles.dismissText, { color: theme.colors.primary }]}>
                {t('dismissNotice')}
              </Text>
            </Pressable>
          </View>
        </View>
      ) : null}
      {usage ? (
        <View
          style={[styles.card, { backgroundColor: theme.colors.surfaceContainerLow }]}
          testID="scan-usage"
        >
          <Text style={[styles.usageText, { color: theme.colors.onSurface }]}>
            {t('usageMonth', { used: usage.used_month, limit: usage.monthly_limit })}
          </Text>
          <Text style={[styles.usageSub, { color: theme.colors.onSurfaceVariant }]}>
            {t('usageToday', { count: remainingToday(usage) })}
          </Text>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { alignSelf: 'stretch', alignItems: 'center', paddingHorizontal: 16, gap: 8 },
  card: {
    width: '100%',
    maxWidth: 480,
    borderRadius: 12,
    paddingHorizontal: 16,
    paddingVertical: 10,
    alignItems: 'center',
    marginBottom: 8,
  },
  noticeText: { fontSize: 14, fontWeight: '500', textAlign: 'center' },
  actions: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', gap: 16 },
  dismiss: { minHeight: 44, minWidth: 44, alignItems: 'center', justifyContent: 'center' },
  dismissText: { fontSize: 14, fontWeight: '600' },
  usageText: { fontSize: 13, fontWeight: '600', textAlign: 'center' },
  usageSub: { fontSize: 12, textAlign: 'center' },
});
