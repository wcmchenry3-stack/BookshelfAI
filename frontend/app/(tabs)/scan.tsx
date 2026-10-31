import React, { useState } from 'react';
import { Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';
import { useScanJobs } from '../../hooks/useScanJobs';
import { useBanner } from '../../hooks/useBanner';
import { useScanUsage } from '../../hooks/useScanUsage';
import { useNetworkStatus } from '../../hooks/useNetworkStatus';
import { useScanCapture } from '../../hooks/useScanCapture';
import { useScanProgress } from '../../hooks/useScanProgress';
import { isScanningLimited } from '../../lib/scanErrors';
import { isAwaitingRetry } from '../../lib/scanJob';
import { ScanModeToggle, type InputMode } from '../../components/scan/ScanModeToggle';
import { ScanProgressPanel } from '../../components/scan/ScanProgressPanel';

type CameraFacing = 'back' | 'front';

export default function ScanScreen() {
  const { theme } = useTheme();
  const { t } = useTranslation('scan');
  // Gold tertiary in dark mode, primary in light mode — active/CTA accent.
  const activeColor = theme.isDark ? theme.colors.tertiary : theme.colors.primary;
  const onActiveColor = theme.isDark ? theme.colors.onTertiary : theme.colors.onPrimary;
  const {
    jobs,
    startScan,
    resumeQueue,
    isQueueFull,
    pendingCount,
    scanNotice,
    dismissScanNotice,
    usageRefreshKey,
  } = useScanJobs();
  // The enhanced re-scan offer uses `enhanced_scans_remaining` from scan
  // responses (#582: the usage counters no longer carry an enhanced count).
  const usage = useScanUsage(usageRefreshKey ?? 0);
  const { isConnected } = useNetworkStatus();
  // While scanning is limited or paused, a full queue is not "wait for uploads":
  // the saved photos go out once scanning is available again.
  function queueFullMessage(): string {
    return t(isScanningLimited(scanNotice, jobs ?? []) ? 'queueFullLimited' : 'queueFull');
  }
  function handleRetryNotice() {
    // Offline: keep the notice and say why nothing was sent.
    if (!isConnected) {
      showBanner({
        message: t('requiresConnection', { ns: 'common' }),
        type: 'error',
        duration: 4000,
      });
      return;
    }
    resumeQueue?.();
  }
  const { showBanner } = useBanner();
  const [permission, requestPermission] = useCameraPermissions();
  const [inputMode, setInputMode] = useState<InputMode>('camera');
  const [query, setQuery] = useState('');
  const [facing, setFacing] = useState<CameraFacing>('back');
  const { cameraRef, webInputRef, capturing, handleCapture, handleWebFileChange } = useScanCapture({
    startScan,
    isQueueFull,
    queueFullMessage,
    facing,
  });

  function handleSearch() {
    const q = query.trim();
    if (!q) return;
    if (isQueueFull) {
      showBanner({ message: queueFullMessage(), type: 'error', duration: 4000 });
      return;
    }
    startScan('text', undefined, q);
    setQuery('');
  }

  const allJobs = jobs ?? [];
  const { waitingCount, failedCount, inProgress, progressText } = useScanProgress(
    allJobs,
    pendingCount,
    capturing
  );

  const modeToggle = (
    <ScanModeToggle
      inputMode={inputMode}
      onChange={setInputMode}
      activeColor={activeColor}
      onActiveColor={onActiveColor}
    />
  );
  const progressPanel = (
    <ScanProgressPanel
      waitingCount={waitingCount}
      failedCount={failedCount}
      inProgress={inProgress}
      progressText={progressText}
      isConnected={isConnected}
      activeColor={activeColor}
      onSendPending={handleRetryNotice}
      usage={usage}
      notice={scanNotice ?? null}
      onDismissNotice={dismissScanNotice ?? (() => {})}
      // No "Try again" while a photo waits on the retry schedule (#604): the
      // queue waits for that photo's next attempt, so it would send nothing.
      onRetryNotice={
        pendingCount > 0 && !(jobs ?? []).some(isAwaitingRetry) ? handleRetryNotice : undefined
      }
    />
  );

  // ── Search mode ──────────────────────────────────────────────────────────
  if (inputMode === 'search') {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        {modeToggle}
        {progressPanel}
        <View style={styles.searchContainer}>
          <TextInput
            style={[
              styles.searchInput,
              {
                backgroundColor: theme.colors.surfaceContainerHighest,
                color: theme.colors.onSurface,
                fontSize: theme.typography.fontSizeBase,
              },
            ]}
            value={query}
            onChangeText={setQuery}
            placeholder={t('searchPlaceholder')}
            placeholderTextColor={theme.colors.textSecondary}
            onSubmitEditing={handleSearch}
            returnKeyType="search"
            accessibilityLabel={t('searchInputA11y')}
          />
          <Pressable
            style={[styles.searchButton, { backgroundColor: activeColor }]}
            onPress={handleSearch}
            accessibilityRole="button"
            accessibilityLabel={t('searchButtonA11y')}
          >
            <Text style={[styles.searchButtonText, { color: onActiveColor }]}>
              {t('searchButton')}
            </Text>
          </Pressable>
        </View>
      </View>
    );
  }

  // ── Web camera mode ───────────────────────────────────────────────────────
  if (Platform.OS === 'web') {
    return (
      <>
        {React.createElement('input', {
          testID: 'web-file-input',
          ref: webInputRef,
          type: 'file',
          accept: 'image/*',
          capture: 'environment',
          style: { display: 'none' },
          onChange: handleWebFileChange,
        })}
        <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
          {modeToggle}
          {progressPanel}
          <Pressable
            style={[styles.webCaptureButton, { backgroundColor: activeColor }]}
            onPress={() => webInputRef.current?.click()}
            accessibilityRole="button"
            accessibilityLabel={t('captureA11y')}
            accessibilityHint={t('captureHint')}
          >
            <Text style={[styles.webCaptureButtonText, { color: onActiveColor }]}>
              {t('takePhoto')}
            </Text>
          </Pressable>
        </View>
      </>
    );
  }

  // ── Native camera mode — permission checks ────────────────────────────────
  if (!permission) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        {modeToggle}
      </View>
    );
  }

  if (!permission.granted) {
    return (
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        {modeToggle}
        <Text
          style={[
            styles.permissionText,
            { color: theme.colors.text, fontSize: theme.typography.fontSizeBase },
          ]}
        >
          {t('permissionText')}
        </Text>
        <Pressable
          style={[styles.permissionButton, { backgroundColor: activeColor }]}
          onPress={requestPermission}
          accessibilityRole="button"
          accessibilityLabel={t('allowCameraA11y')}
        >
          <Text
            style={[
              styles.permissionButtonText,
              { fontSize: theme.typography.fontSizeBase, color: onActiveColor },
            ]}
          >
            {t('allowCamera')}
          </Text>
        </Pressable>
      </View>
    );
  }

  // ── Native camera view ────────────────────────────────────────────────────
  return (
    <View style={styles.container}>
      <CameraView ref={cameraRef} style={styles.camera} facing={facing} />
      {/* box-none: overlay itself doesn't intercept touches, but its Pressable
          children still receive them. Without this, iOS was swallowing the
          shutter press on top of the CameraView. */}
      <View style={styles.overlay} pointerEvents="box-none">
        {/* Grouped so the badge appearing doesn't shift the space-between layout. */}
        <View style={styles.topGroup}>
          {modeToggle}
          {progressPanel}
        </View>

        {/* Viewfinder: corner-bracket markers + scanning line */}
        <View style={styles.frameWrapper} pointerEvents="none">
          {/* Top-left corner */}
          <View style={[styles.corner, styles.cornerTL, { borderColor: activeColor }]} />
          {/* Top-right corner */}
          <View style={[styles.corner, styles.cornerTR, { borderColor: activeColor }]} />
          {/* Bottom-left corner */}
          <View style={[styles.corner, styles.cornerBL, { borderColor: activeColor }]} />
          {/* Bottom-right corner */}
          <View style={[styles.corner, styles.cornerBR, { borderColor: activeColor }]} />
          {/* Scanning line — horizontal accent bar across the frame centre */}
          <View
            style={[
              styles.scanLine,
              {
                backgroundColor: activeColor,
                shadowColor: activeColor,
                shadowOffset: { width: 0, height: 0 },
                shadowOpacity: 0.6,
                shadowRadius: 4,
              },
            ]}
          />
        </View>

        <View style={styles.cameraControls}>
          <Pressable
            style={[styles.flipButton, { backgroundColor: theme.colors.surfaceContainerHighest }]}
            onPress={() => setFacing((f) => (f === 'back' ? 'front' : 'back'))}
            accessibilityRole="button"
            accessibilityLabel={t('flipCameraA11y')}
          >
            <Text style={[styles.flipButtonText, { color: theme.colors.onSurface }]}>
              {t('flipCamera')}
            </Text>
          </Pressable>
          {/* Shutter: outer ring in activeColor, inner disc in onSurface */}
          <Pressable
            style={[styles.captureButton, { borderColor: activeColor }]}
            onPress={handleCapture}
            disabled={capturing}
            accessibilityRole="button"
            accessibilityLabel={t('captureA11y')}
            accessibilityHint={t('captureHint')}
          >
            <View style={[styles.captureInner, { backgroundColor: theme.colors.onSurface }]} />
          </Pressable>
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  camera: { flex: 1, width: '100%' },
  overlay: {
    ...StyleSheet.absoluteFill,
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 60,
  },
  topGroup: { alignItems: 'center' },
  // Viewfinder frame area — contains corner brackets and scan line.
  frameWrapper: {
    width: 240,
    height: 340,
    position: 'relative',
  },
  // Each corner is a 40×40 view with only 2 border sides visible.
  corner: {
    position: 'absolute',
    width: 40,
    height: 40,
    borderWidth: 3,
  },
  cornerTL: { top: 0, left: 0, borderRightWidth: 0, borderBottomWidth: 0, borderTopLeftRadius: 8 },
  cornerTR: {
    top: 0,
    right: 0,
    borderLeftWidth: 0,
    borderBottomWidth: 0,
    borderTopRightRadius: 8,
  },
  cornerBL: {
    bottom: 0,
    left: 0,
    borderRightWidth: 0,
    borderTopWidth: 0,
    borderBottomLeftRadius: 8,
  },
  cornerBR: {
    bottom: 0,
    right: 0,
    borderLeftWidth: 0,
    borderTopWidth: 0,
    borderBottomRightRadius: 8,
  },
  // Horizontal scan line centred vertically in the frame.
  scanLine: {
    position: 'absolute',
    top: '50%',
    left: 8,
    right: 8,
    height: 1,
    opacity: 0.5,
  },
  cameraControls: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 24,
  },
  flipButton: {
    width: 48,
    height: 48,
    borderRadius: 24,
    alignItems: 'center',
    justifyContent: 'center',
  },
  flipButtonText: {
    fontWeight: '600',
    fontSize: 12,
  },
  captureButton: {
    width: 72,
    height: 72,
    borderRadius: 36,
    borderWidth: 3,
    backgroundColor: 'transparent',
    alignItems: 'center',
    justifyContent: 'center',
  },
  captureInner: {
    width: 56,
    height: 56,
    borderRadius: 28,
  },
  webCaptureButton: {
    paddingHorizontal: 32,
    paddingVertical: 16,
    borderRadius: 8,
    minHeight: 56,
    alignItems: 'center',
    justifyContent: 'center',
  },
  webCaptureButtonText: {
    fontWeight: '600',
    fontSize: 18,
  },
  searchContainer: {
    width: '100%',
    paddingHorizontal: 24,
    gap: 12,
  },
  searchInput: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 16,
    paddingVertical: 12,
    minHeight: 44,
  },
  searchButton: {
    borderRadius: 8,
    paddingVertical: 12,
    alignItems: 'center',
    minHeight: 44,
    justifyContent: 'center',
  },
  searchButtonText: {
    fontWeight: '600',
    fontSize: 16,
  },
  permissionText: {
    textAlign: 'center',
    marginHorizontal: 32,
    marginBottom: 24,
  },
  permissionButton: {
    paddingHorizontal: 24,
    paddingVertical: 12,
    borderRadius: 8,
    minHeight: 44,
    justifyContent: 'center',
  },
  permissionButtonText: {
    fontWeight: '600',
  },
});
