import React, { useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../../hooks/useTheme';
import { useFeedbackSubmit, FeedbackType } from './useFeedbackSubmit';

const TITLE_MAX = 120;
const DESCRIPTION_MAX = 2000;

interface Props {
  visible: boolean;
  onClose: () => void;
}
const styles = makeBookStyles();

export function FeedbackWidget({ visible, onClose }: Props) {
  const { t } = useTranslation('feedback');
  const { theme } = useTheme();
  const { colors } = theme;
  const { status, result, error, submit, reset } = useFeedbackSubmit();

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [type, setType] = useState<FeedbackType>('bug');
  const [titleError, setTitleError] = useState('');
  const [descError, setDescError] = useState('');

  function handleClose() {
    reset();
    setTitle('');
    setDescription('');
    setType('bug');
    setTitleError('');
    setDescError('');
    onClose();
  }

  function validate(): boolean {
    let valid = true;
    if (!title.trim()) {
      setTitleError(t('error_title_required'));
      valid = false;
    } else {
      setTitleError('');
    }
    if (!description.trim()) {
      setDescError(t('error_description_required'));
      valid = false;
    } else {
      setDescError('');
    }
    return valid;
  }

  async function handleSubmit() {
    if (!validate()) return;
    await submit({ title: title.trim(), description: description.trim(), type });
  }

  const isSubmitting = status === 'submitting';

  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={handleClose}
      accessibilityViewIsModal
    >
      <View style={styles.overlay}>
        <View style={styles.sheet}>
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
            style={styles.body}
            contentContainerStyle={styles.bodyContent}
            keyboardShouldPersistTaps="handled"
          >
            {status === 'success' ? (
              /* ── Success state ── */
              <View style={styles.successContainer} accessibilityLiveRegion="polite">
                <Text style={styles.successTitle}>{t('submit_success')}</Text>
                {result && (
                  <Text style={styles.successSub}>
                    {t('submit_success_issue', { number: result.issueNumber })}
                  </Text>
                )}
                <Pressable
                  style={[styles.primaryBtn, { backgroundColor: colors.primary }]}
                  onPress={handleClose}
                  accessibilityRole="button"
                >
                  <Text style={[styles.primaryBtnText, { color: colors.onPrimary }]}>
                    {t('close_label')}
                  </Text>
                </Pressable>
              </View>
            ) : (
              /* ── Form state ── */
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
                        : error.kind === 'rejected'
                          ? t('submit_error_rejected')
                          : error.kind === 'network'
                            ? t('submit_error_network')
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

                {/* Title field */}
                <Text style={styles.label} nativeID="feedback-title-label">
                  {t('label_title')}
                </Text>
                <TextInput
                  style={[
                    styles.input,
                    {
                      color: colors.onSurface,
                      backgroundColor: colors.surfaceContainerHigh,
                      borderColor: titleError ? colors.error : colors.outline,
                    },
                  ]}
                  value={title}
                  onChangeText={(v) => {
                    setTitle(v.slice(0, TITLE_MAX));
                    if (titleError) setTitleError('');
                  }}
                  placeholder={t('placeholder_title')}
                  placeholderTextColor={colors.onSurfaceVariant}
                  maxLength={TITLE_MAX}
                  returnKeyType="next"
                  accessibilityLabelledBy="feedback-title-label"
                />
                {titleError ? (
                  <Text
                    style={[styles.fieldError, { color: colors.error }]}
                    accessibilityRole="alert"
                  >
                    {titleError}
                  </Text>
                ) : (
                  <Text style={[styles.charCount, { color: colors.onSurfaceVariant }]}>
                    {title.length}/{TITLE_MAX}
                  </Text>
                )}

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
            )}
          </ScrollView>
        </View>
      </View>
    </Modal>
  );
}

function makeBookStyles() {
  return StyleSheet.create({
    overlay: { flex: 1, justifyContent: 'flex-end', backgroundColor: 'rgba(0,0,0,0.55)' },
    sheet: { borderTopLeftRadius: 20, borderTopRightRadius: 20, maxHeight: '90%' },
    header: {
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'space-between',
      paddingHorizontal: 20,
      paddingVertical: 16,
      borderBottomWidth: StyleSheet.hairlineWidth,
    },
    heading: { fontWeight: '600' },
    closeBtn: { padding: 6, borderRadius: 16 },
    closeBtnText: { fontSize: 16 },
    body: { flex: 1 },
    bodyContent: { padding: 20, paddingBottom: 36 },
    label: { fontWeight: '500', marginTop: 12, marginBottom: 4 },
    typeRow: { flexDirection: 'row', gap: 10, marginBottom: 4 },
    typeChip: { paddingHorizontal: 16, paddingVertical: 8, borderRadius: 20, borderWidth: 1 },
    typeChipText: { fontWeight: '500' },
    input: { borderWidth: 1, borderRadius: 8, paddingHorizontal: 12, paddingVertical: 10 },
    textarea: {
      borderWidth: 1,
      borderRadius: 8,
      paddingHorizontal: 12,
      paddingVertical: 10,
      minHeight: 120,
    },
    charCount: { textAlign: 'right' },
    fieldError: {},
    errorBanner: { borderWidth: 1, borderRadius: 8, padding: 12, marginBottom: 4 },
    errorBannerText: {},
    primaryBtn: {
      marginTop: 20,
      paddingVertical: 14,
      borderRadius: 10,
      alignItems: 'center',
      justifyContent: 'center',
    },
    primaryBtnText: { fontWeight: '600' },
    successContainer: { alignItems: 'center', paddingVertical: 24, gap: 12 },
    successTitle: { fontWeight: '600', textAlign: 'center' },
    successSub: { textAlign: 'center' },
  });
}
