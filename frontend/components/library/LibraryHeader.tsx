import type { ReactNode } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { useTheme } from '../../hooks/useTheme';
import { useLibraryAccent } from './useLibraryAccent';

interface LibraryHeaderProps {
  title: string;
  subtitle: string;
  count: number;
  summaryLabel: string;
  /** Rendered above the hero (Wishlist puts its offline notice here). */
  leading?: ReactNode;
}

/** Hero title, subtitle and the "N books" archive summary card. */
export function LibraryHeader({
  title,
  subtitle,
  count,
  summaryLabel,
  leading,
}: LibraryHeaderProps) {
  const { theme } = useTheme();
  const { activeColor } = useLibraryAccent();
  return (
    <View style={styles.listHeader}>
      {leading}
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
        {title}
      </Text>
      <Text
        style={[
          styles.heroSubtitle,
          { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
        ]}
      >
        {subtitle}
      </Text>

      {/* Archive summary card */}
      <View style={[styles.summaryCard, { backgroundColor: theme.colors.surfaceContainerHigh }]}>
        <View style={[styles.summaryAccent, { backgroundColor: activeColor }]} />
        <Text style={[styles.summaryCount, { color: theme.colors.text }]}>{count}</Text>
        <Text
          style={[
            styles.summaryLabel,
            { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
          ]}
        >
          {summaryLabel}
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
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
});
