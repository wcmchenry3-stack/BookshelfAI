import { StyleSheet, View } from 'react-native';

import { useTheme } from '../../hooks/useTheme';

/**
 * Four book spines for the empty Wishlist: a soft one, a solid one with two
 * title bars, a dashed "still to come" one and a leaning blank one. Views and
 * token colours only. Decorative: `EmptyState` hides its illustration slot
 * from screen readers.
 */
export function WishlistEmptyIllustration() {
  const { theme } = useTheme();
  const { colors } = theme;
  return (
    <View style={styles.row}>
      <View
        style={[
          styles.spine,
          styles.soft,
          { backgroundColor: colors.primarySoft, borderColor: colors.primary },
        ]}
      />
      <View style={[styles.spine, styles.solid, { backgroundColor: colors.primary }]}>
        <View style={[styles.bar, styles.barUpper, { backgroundColor: colors.onPrimary }]} />
        <View style={[styles.bar, styles.barLower, { backgroundColor: colors.onPrimary }]} />
      </View>
      <View
        style={[
          styles.spine,
          styles.dashed,
          { backgroundColor: colors.background, borderColor: colors.onSurfaceVariant },
        ]}
      />
      <View
        style={[
          styles.spine,
          styles.leaning,
          { backgroundColor: colors.coverBlank, borderColor: colors.onSurfaceVariant },
        ]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  row: { height: 132, flexDirection: 'row', alignItems: 'flex-end', gap: 4 },
  spine: { borderTopLeftRadius: 3, borderTopRightRadius: 3 },
  soft: { width: 26, height: 108, borderWidth: 1.5 },
  solid: { width: 32, height: 124 },
  dashed: { width: 24, height: 96, borderWidth: 1.5, borderStyle: 'dashed', borderRadius: 3 },
  leaning: {
    width: 28,
    height: 116,
    borderWidth: 1.5,
    marginLeft: 6,
    transform: [{ rotate: '8deg' }],
  },
  bar: { position: 'absolute', left: 6, right: 6, height: 2, opacity: 0.6 },
  barUpper: { top: 14 },
  barLower: { top: 20 },
});
