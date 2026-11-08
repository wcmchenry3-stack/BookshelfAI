import React from 'react';
import { StyleSheet, View, type StyleProp, type ViewStyle } from 'react-native';

import { useTheme } from '../../hooks/useTheme';
import { SectionLabel } from './SectionLabel';

export interface ListSectionProps {
  /** Optional heading above the group. */
  title?: string;
  /** `ListRow` children. Inset dividers go between them. */
  children: React.ReactNode;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/** Grouped inset list: a rounded surface card holding rows with inset dividers. */
export function ListSection({ title, children, testID, style }: ListSectionProps) {
  const { theme } = useTheme();
  const rows = React.Children.toArray(children).filter(Boolean);
  return (
    <View style={style} testID={testID}>
      {title ? <SectionLabel style={styles.title}>{title}</SectionLabel> : null}
      <View
        style={[
          styles.card,
          {
            backgroundColor: theme.colors.surface,
            borderColor: theme.colors.outlineCard,
            borderRadius: theme.radius.card,
          },
        ]}
      >
        {rows.map((row, i) => (
          <React.Fragment key={i}>
            {i > 0 ? (
              <View
                testID="list-divider"
                style={[
                  styles.divider,
                  { backgroundColor: theme.colors.outline },
                  !hasIcon(row) && styles.dividerNoIcon,
                ]}
              />
            ) : null}
            {row}
          </React.Fragment>
        ))}
      </View>
    </View>
  );
}

const hasIcon = (row: React.ReactNode) =>
  React.isValidElement<{ icon?: unknown }>(row) && Boolean(row.props.icon);

const styles = StyleSheet.create({
  title: { marginBottom: 8, marginHorizontal: 16 },
  card: { borderWidth: 1, overflow: 'hidden' },
  // Inset past the icon tile (16 padding + 32 tile + 12 gap).
  divider: { height: StyleSheet.hairlineWidth, marginLeft: 60 },
  dividerNoIcon: { marginLeft: 16 },
});
