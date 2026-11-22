import React from 'react';
import type { StyleProp, TextStyle } from 'react-native';

import { AppText } from './AppText';

export interface SectionLabelProps {
  children: string;
  testID?: string;
  style?: StyleProp<TextStyle>;
}

/** Small uppercase label above a group of content. Announced as a header. */
export function SectionLabel({ children, testID, style }: SectionLabelProps) {
  return (
    <AppText
      variant="sectionLabel"
      color="onSurfaceVariant"
      accessibilityRole="header"
      testID={testID}
      style={style}
    >
      {children}
    </AppText>
  );
}
