import { useEffect, useRef, type ReactNode } from 'react';
import { AccessibilityInfo, findNodeHandle, Modal, Platform, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useTheme } from '../hooks/useTheme';
import { AppText } from './ui/AppText';
import { Button } from './ui/Button';

interface SheetModalProps {
  visible: boolean;
  onDismiss: () => void;
  onHidden?: () => void;
  title: string;
  /** Optional line under the title. */
  subtitle?: string;
  /** Visible text of the close/cancel action (translated by the caller). */
  closeLabel: string;
  /** Spoken label of the close/cancel action (translated by the caller). */
  closeA11yLabel: string;
  /** Body between the header and the footer (usually a ScrollView). */
  children: ReactNode;
  /** Content of the pinned footer bar (usually the primary action button). */
  footer: ReactNode;
  /** Lines the title may take before it is cut. Defaults to 2. */
  titleLines?: number;
}

/**
 * Shared sheet anatomy (#903): scrim backdrop, grabber, header (serif
 * `sheetTitle` + optional subtitle + close action), a body that takes the
 * remaining height, and a footer pinned below it with safe-area bottom
 * padding. The body wrapper is `flexShrink: 1` (never `flex: 1` in a
 * content-sized parent) so it cannot collapse. Focus moves to the title when
 * the sheet opens.
 */
export default function SheetModal({
  visible,
  onDismiss,
  onHidden,
  title,
  subtitle,
  closeLabel,
  closeA11yLabel,
  children,
  footer,
  titleLines = 2,
}: SheetModalProps) {
  const { theme } = useTheme();
  const { colors } = theme;
  const insets = useSafeAreaInsets();
  const titleRef = useRef<View>(null);
  const isIOS = Platform.OS === 'ios';

  useEffect(() => {
    if (!visible) return;
    // Wait a beat so the node is mounted inside the Modal before focusing.
    const timer = setTimeout(() => {
      const node = titleRef.current ? findNodeHandle(titleRef.current) : null;
      if (node) AccessibilityInfo.setAccessibilityFocus(node);
    }, 100);
    return () => clearTimeout(timer);
  }, [visible]);

  return (
    <Modal
      visible={visible}
      animationType="slide"
      presentationStyle="pageSheet"
      onRequestClose={onDismiss}
      onDismiss={onHidden}
      accessibilityViewIsModal
    >
      <View style={[styles.scrim, { backgroundColor: colors.scrim }]} testID="sheet-scrim">
        <View
          style={[
            styles.sheet,
            { backgroundColor: colors.background },
            !isIOS && styles.sheetRounded,
          ]}
        >
          <View
            style={[styles.grabber, { backgroundColor: colors.outline }]}
            testID="sheet-grabber"
            accessibilityElementsHidden
            importantForAccessibility="no-hide-descendants"
          />
          <View style={styles.header}>
            <View style={styles.titleBlock}>
              <View ref={titleRef} accessible accessibilityRole="header">
                <AppText variant="sheetTitle" accessibilityRole="header" numberOfLines={titleLines}>
                  {title}
                </AppText>
              </View>
              {subtitle ? (
                <AppText variant="secondary" color="onSurfaceVariant" numberOfLines={2}>
                  {subtitle}
                </AppText>
              ) : null}
            </View>
            <Button
              variant="text"
              label={closeLabel}
              accessibilityLabel={closeA11yLabel}
              onPress={onDismiss}
              style={styles.close}
            />
          </View>

          <View style={styles.body} testID="sheet-body">
            {children}
          </View>

          <View
            style={[
              styles.footer,
              {
                borderTopColor: colors.outline,
                backgroundColor: colors.surfaceBar,
                paddingBottom: 16 + insets.bottom,
              },
            ]}
            testID="sheet-footer"
          >
            {footer}
          </View>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: { flex: 1 },
  // Fills the Modal (a page sheet on iOS); shrinks, never `flex: 1` alone.
  sheet: { flexGrow: 1, flexShrink: 1 },
  sheetRounded: { marginTop: 24, borderTopLeftRadius: 20, borderTopRightRadius: 20 },
  grabber: { alignSelf: 'center', width: 36, height: 5, borderRadius: 3, marginTop: 8 },
  header: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    paddingHorizontal: 20,
    paddingTop: 8,
    paddingBottom: 12,
    gap: 8,
  },
  titleBlock: { flex: 1, flexShrink: 1, gap: 2, paddingTop: 6 },
  close: { paddingHorizontal: 8 },
  body: { flexGrow: 1, flexShrink: 1 },
  footer: {
    paddingHorizontal: 20,
    paddingTop: 12,
    gap: 8,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
});
