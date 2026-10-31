import type { ReactNode } from 'react';
import { Modal, Pressable, StyleSheet, Text, View } from 'react-native';

import { useTheme } from '../hooks/useTheme';

interface SheetModalProps {
  visible: boolean;
  onDismiss: () => void;
  onHidden?: () => void;
  title: string;
  closeLabel: string;
  closeA11yLabel: string;
  /** Body between the header and the footer (usually a ScrollView). */
  children: ReactNode;
  /** Content of the bordered footer bar (usually the primary action button). */
  footer: ReactNode;
}

/**
 * Full-screen page-sheet modal scaffold shared by the scan candidate picker and
 * the unverified-book form: themed container, title + close header, body and
 * a bordered footer.
 */
export default function SheetModal({
  visible,
  onDismiss,
  onHidden,
  title,
  closeLabel,
  closeA11yLabel,
  children,
  footer,
}: SheetModalProps) {
  const { theme } = useTheme();
  const { colors } = theme;

  return (
    <Modal
      visible={visible}
      animationType="slide"
      presentationStyle="pageSheet"
      onRequestClose={onDismiss}
      onDismiss={onHidden}
      accessibilityViewIsModal
    >
      <View style={[styles.container, { backgroundColor: colors.background }]}>
        <View style={[styles.header, { borderBottomColor: colors.border }]}>
          <Text
            style={[styles.title, { color: colors.text, fontSize: theme.typography.fontSizeLG }]}
            accessibilityRole="header"
          >
            {title}
          </Text>
          <Pressable
            onPress={onDismiss}
            accessibilityRole="button"
            accessibilityLabel={closeA11yLabel}
            style={styles.closeButton}
            hitSlop={8}
          >
            <Text style={{ color: colors.primary, fontSize: theme.typography.fontSizeBase }}>
              {closeLabel}
            </Text>
          </Pressable>
        </View>

        {children}

        <View style={[styles.footer, { borderTopColor: colors.border }]}>{footer}</View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 16,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  title: { fontWeight: '700' },
  closeButton: { minWidth: 44, minHeight: 44, justifyContent: 'center', alignItems: 'flex-end' },
  footer: { padding: 16, borderTopWidth: StyleSheet.hairlineWidth },
});
