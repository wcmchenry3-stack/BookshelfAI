import React from 'react';
import { StyleSheet, View, type StyleProp, type ViewStyle } from 'react-native';

import { AppText } from './AppText';
import { Button } from './Button';

interface EmptyStateAction {
  label: string;
  onPress: () => void;
  testID?: string;
}

export interface EmptyStateProps {
  /** Illustration slot: any node (icon, image). Decorative, hidden from screen readers. */
  illustration?: React.ReactNode;
  title: string;
  body?: string;
  primaryAction?: EmptyStateAction;
  secondaryAction?: EmptyStateAction;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/** Centred empty or first-run state. Copy comes in as props. */
export function EmptyState({
  illustration,
  title,
  body,
  primaryAction,
  secondaryAction,
  testID,
  style,
}: EmptyStateProps) {
  return (
    <View style={[styles.wrap, style]} testID={testID}>
      {illustration ? (
        <View
          style={styles.illustration}
          accessibilityElementsHidden
          aria-hidden
          importantForAccessibility="no-hide-descendants"
          testID={testID ? `${testID}-illustration` : undefined}
        >
          {illustration}
        </View>
      ) : null}
      <AppText variant="sheetTitle" accessibilityRole="header" style={styles.center}>
        {title}
      </AppText>
      {body ? (
        <AppText variant="body" color="onSurfaceVariant" style={[styles.center, styles.body]}>
          {body}
        </AppText>
      ) : null}
      {primaryAction ? (
        <Button
          label={primaryAction.label}
          onPress={primaryAction.onPress}
          testID={primaryAction.testID}
          style={styles.action}
        />
      ) : null}
      {secondaryAction ? (
        <Button
          variant="text"
          label={secondaryAction.label}
          onPress={secondaryAction.onPress}
          testID={secondaryAction.testID}
          style={styles.secondary}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { alignItems: 'center', justifyContent: 'center', padding: 32 },
  illustration: { marginBottom: 24 },
  center: { textAlign: 'center' },
  body: { marginTop: 8 },
  action: { marginTop: 24, alignSelf: 'stretch' },
  secondary: { marginTop: 8 },
});
