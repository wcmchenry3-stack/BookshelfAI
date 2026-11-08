import React from 'react';
import { StyleSheet, View, type StyleProp, type ViewStyle } from 'react-native';

import { useTheme } from '../../hooks/useTheme';

export interface ProgressBarProps {
  value: number;
  min?: number;
  max?: number;
  /** Spoken label. Callers pass translated copy. */
  accessibilityLabel: string;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/** Determinate progress bar. The fill is clamped to the min..max range. */
export function ProgressBar({
  value,
  min = 0,
  max = 100,
  accessibilityLabel,
  testID,
  style,
}: ProgressBarProps) {
  const { theme } = useTheme();
  const span = max - min;
  const clamped = Math.min(Math.max(value, min), max);
  const fraction = span > 0 ? (clamped - min) / span : 0;
  return (
    <View
      accessible
      accessibilityRole="progressbar"
      accessibilityLabel={accessibilityLabel}
      accessibilityValue={{ min, max, now: clamped }}
      testID={testID}
      style={[styles.track, { backgroundColor: theme.colors.primarySoft }, style]}
    >
      <View
        testID={testID ? `${testID}-fill` : undefined}
        style={[
          styles.fill,
          { width: `${fraction * 100}%`, backgroundColor: theme.colors.primary },
        ]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  track: { height: 6, borderRadius: 3, overflow: 'hidden' },
  fill: { height: '100%', borderRadius: 3 },
});
