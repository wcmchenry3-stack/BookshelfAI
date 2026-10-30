import React, { useRef, useState } from 'react';
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../../hooks/useTheme';
import { useFeedbackSubmit, FeedbackType, DESCRIPTION_MAX } from './useFeedbackSubmit';

interface Props {
  visible: boolean;
  onClose: () => void;
  /** Called once feedback is sent, after the sheet has closed itself. */
  onSubmitted?: () => void;
}

export function FeedbackWidget({ visible, onClose, onSubmitted }: Props) {
  const { t } = useTranslation('feedback');
  const { theme } = useTheme();
  const { colors } = theme;
  const { status, error, submit, reset } = useFeedbackSubmit();

  const [description, setDescription] = useState('');
  const [type, setType] = useState<FeedbackType>('bug');
  const [descError, setDescError] = useState('');

  // Bumped on every close so a submit that resolves after the user already
  // dismissed the sheet does not close (and reset) it a second time.
  const closeCountRef = useRef(0);

  function handleClose() {
    closeCountRef.current += 1;
    reset();
    setDescription('');
    setType('bug');
    setDescError('');
    onClose();
  }

  function validate(): boolean {
    if (!description.trim()) {
      setDescError(t('error_description_required'));
      return false;
    }
    setDescError('');
    return true;
  }

  async function handleSubmit() {
    if (!validate()) return;
    // Success closes the sheet; the host shows the thank-you banner.
    const closesBefore = closeCountRef.current;
    if (await submit({ description: description.trim(), type })) {
      if (closeCountRef.current === closesBefore) handleClose();
      onSubmitted?.();
    }
  }

  const isSubmitting = status === 'submitting';
  const styles = makeStyles(theme);

  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={handleClose}
      accessibilityViewIsModal
    >
      {/* The description field sits at the bottom of the sheet, so on iOS the
          keyboard would cover it; Android resizes the window itself. */}
      <KeyboardAvoidingView
        style={styles.overlay}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <View style={styles.sheet} testID="feedback-sheet">
          {/* Header */}
          <View style={styles.header}>
            <Text style={styles.heading} accessibilityRole="header">
              {t('title')}
            </Text>
            <Pressable
              onPress={handleClose}
              style={styles.closeBtn}
              accessibilityRole="button"
              accessibilityLabel={t('close_label')}
            >
              <Text style={styles.closeBtnText}>✕</Text>
            </Pressable>
          </View>

          <ScrollView
            testID="feedback-body"
            style={styles.body}
            contentContainerStyle={styles.bodyContent}
            keyboardShouldPersistTaps="handled"
          >
            <>
              {/* Error banner */}
              {status === 'error' && error && (
                <View
                  style={[styles.errorBanner, { borderColor: colors.error }]}
                  accessibilityLiveRegion="assertive"
                  accessibilityRole="alert"
                >
                  <Text style={[styles.errorBannerText, { color: colors.error }]}>
                    {error.kind === 'rate_limit'
                      ? t('submit_error_rate_limit', {
                          seconds: error.retryAfterSeconds ?? 60,
                        })
                      : t('submit_error')}
                  </Text>
                </View>
              )}

              {/* Type selector */}
              <Text style={styles.label}>{t('type_label')}</Text>
              <View style={styles.typeRow}>
                {(['bug', 'feature'] as FeedbackType[]).map((ft) => (
                  <Pressable
                    key={ft}
                    style={[
                      styles.typeChip,
                      {
                        backgroundColor: type === ft ? colors.primary : colors.surface,
                        borderColor: type === ft ? colors.primary : colors.outline,
                      },
                    ]}
                    onPress={() => setType(ft)}
                    accessibilityRole="radio"
                    accessibilityState={{ selected: type === ft }}
                    accessibilityLabel={t(`type_${ft}`)}
                  >
                    <Text
                      style={[
                        styles.typeChipText,
                        {
                          color: type === ft ? colors.onPrimary : colors.onSurface,
                        },
                      ]}
                    >
                      {t(`type_${ft}`)}
                    </Text>
                  </Pressable>
                ))}
              </View>

              {/* Description field */}
              <Text style={styles.label} nativeID="feedback-desc-label">
                {t('label_description')}
              </Text>
              <TextInput
                style={[
                  styles.textarea,
                  {
                    color: colors.onSurface,
                    backgroundColor: colors.surfaceContainerHigh,
                    borderColor: descError ? colors.error : colors.outline,
                  },
                ]}
                value={description}
                onChangeText={(v) => {
                  setDescription(v.slice(0, DESCRIPTION_MAX));
                  if (descError) setDescError('');
                }}
                placeholder={t('placeholder_description')}
                placeholderTextColor={colors.onSurfaceVariant}
                multiline
                numberOfLines={5}
                maxLength={DESCRIPTION_MAX}
                textAlignVertical="top"
                accessibilityLabelledBy="feedback-desc-label"
              />
              {descError ? (
                <Text
                  style={[styles.fieldError, { color: colors.error }]}
                  accessibilityRole="alert"
                >
                  {descError}
                </Text>
              ) : (
                <Text style={[styles.charCount, { color: colors.onSurfaceVariant }]}>
                  {description.length}/{DESCRIPTION_MAX}
                </Text>
              )}

              {/* Submit */}
              <Pressable
                style={[
                  styles.primaryBtn,
                  { backgroundColor: isSubmitting ? colors.outline : colors.primary },
                ]}
                onPress={handleSubmit}
                disabled={isSubmitting}
                accessibilityRole="button"
                accessibilityState={{ disabled: isSubmitting, busy: isSubmitting }}
                accessibilityLabel={t('submit')}
              >
                {isSubmitting ? (
                  <ActivityIndicator color={colors.onPrimary} />
                ) : (
                  <Text style={[styles.primaryBtnText, { color: colors.onPrimary }]}>
                    {t('submit')}
                  </Text>
                )}
              </Pressable>
            </>
          </ScrollView>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>['theme']) {
  const { colors, typography } = theme;
  const body = { fontFamily: typography.fontFamilyBody };
  return StyleSheet.create({
    overlay: { flex: 1, justifyContent: 'flex-end', backgroundColor: colors.scrim },
    sheet: {
      backgroundColor: colors.surface,
      borderTopLeftRadius: 20,
      borderTopRightRadius: 20,
      maxHeight: '90%',
      // The sheet has no fixed height, so it must be allowed to shrink inside
      // the 90% cap rather than being sized purely by its content.
      flexShrink: 1,
    },
    header: {
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'space-between',
      paddingHorizontal: 20,
      paddingVertical: 16,
      borderBottomWidth: StyleSheet.hairlineWidth,
      borderBottomColor: colors.outline,
    },
    heading: {
      ...body,
      fontSize: typography.fontSizeLG,
      fontWeight: '600',
      color: colors.onSurface,
    },
    closeBtn: {
      minWidth: 44,
      minHeight: 44,
      alignItems: 'center',
      justifyContent: 'center',
      borderRadius: 22,
    },
    closeBtnText: { fontSize: typography.fontSizeBase, color: colors.onSurfaceVariant },
    // NOT `flex: 1`: the sheet's height is content-derived, so a flex-grow
    // child collapses to 0 (blank sheet). flexShrink takes content height and
    // gives way once the sheet hits maxHeight, which is when scrolling starts.
    body: { flexShrink: 1 },
    bodyContent: { padding: 20, paddingBottom: 36, gap: 6 },
    label: {
      ...body,
      fontSize: typography.fontSizeSM,
      fontWeight: '500',
      color: colors.onSurfaceVariant,
      marginTop: 12,
      marginBottom: 4,
    },
    typeRow: { flexDirection: 'row', gap: 10, marginBottom: 4 },
    typeChip: {
      minHeight: 44,
      justifyContent: 'center',
      paddingHorizontal: 16,
      paddingVertical: 8,
      borderRadius: 22,
      borderWidth: 1,
    },
    typeChipText: { ...body, fontSize: typography.fontSizeSM, fontWeight: '500' },
    textarea: {
      ...body,
      borderWidth: 1,
      borderRadius: 8,
      paddingHorizontal: 12,
      paddingVertical: 10,
      fontSize: typography.fontSizeBase,
      minHeight: 120,
    },
    charCount: { ...body, fontSize: typography.fontSizeXS, textAlign: 'right' },
    fieldError: { ...body, fontSize: typography.fontSizeXS },
    errorBanner: { borderWidth: 1, borderRadius: 8, padding: 12, marginBottom: 4 },
    errorBannerText: { ...body, fontSize: typography.fontSizeSM },
    primaryBtn: {
      marginTop: 20,
      minHeight: 48,
      paddingVertical: 14,
      borderRadius: 10,
      alignItems: 'center',
      justifyContent: 'center',
    },
    primaryBtnText: { ...body, fontSize: typography.fontSizeBase, fontWeight: '600' },
  });
}
