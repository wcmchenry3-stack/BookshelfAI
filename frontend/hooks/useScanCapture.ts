import { useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import type { CameraView } from 'expo-camera';
import { Directory, File, Paths } from 'expo-file-system';
import { useTranslation } from 'react-i18next';

import { Sentry } from '../lib/sentry';
import { useBanner } from './useBanner';

interface Options {
  startScan: (type: 'image' | 'text', imageUri?: string, query?: string) => void;
  isQueueFull: boolean | undefined;
  /** Copy for the "queue is full" banner (depends on the scan notice / jobs). */
  queueFullMessage: () => string;
  facing: 'back' | 'front';
}

/**
 * Photo capture for the Scan screen: the native camera shutter sequence and
 * the web file input. Both refuse while the queue is full and hand the photo
 * to `startScan`.
 */
export function useScanCapture({ startScan, isQueueFull, queueFullMessage, facing }: Options) {
  const { t } = useTranslation('scan');
  const { showBanner } = useBanner();
  const [capturing, setCapturing] = useState(false);
  const cameraRef = useRef<CameraView>(null);
  const captureSeq = useRef(0);
  const webInputRef = useRef<HTMLInputElement>(null);

  // Web: file input onChange — receives a File object directly from the browser.
  async function handleWebFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e?.target?.files?.[0];
    if (!file) return;
    if (webInputRef.current) webInputRef.current.value = '';
    if (isQueueFull) {
      showBanner({ message: queueFullMessage(), type: 'error', duration: 4000 });
      return;
    }

    // On web, pass the File object URL. The ScanJobContext handles FormData.
    let uri: string;
    try {
      uri = URL.createObjectURL(file);
    } catch {
      uri = file.name;
    }
    startScan('image', uri);
  }

  // Native: camera shutter — take photo, persist to document dir, start background scan.
  async function handleCapture() {
    if (!cameraRef.current || capturing) return;
    // Check before copying to scan-queue/ so a rejected capture leaves no file behind.
    if (isQueueFull) {
      showBanner({ message: queueFullMessage(), type: 'error', duration: 4000 });
      return;
    }
    setCapturing(true);
    Sentry.addBreadcrumb({
      category: 'scan',
      message: 'Camera capture started',
      level: 'info',
    });
    // Track which pipeline stage is executing so the catch block can tag the
    // failure precisely. Each silent failure previously looked identical in Sentry.
    let stage: 'take_picture' | 'dir_create' | 'file_copy' | 'start_scan' = 'take_picture';
    try {
      // Wrap takePictureAsync in a 10s timeout — without this, a hung native
      // promise makes the whole flow silently stall with no Sentry signal.
      const photo = await Promise.race([
        cameraRef.current.takePictureAsync({
          quality: 0.7,
          base64: false,
        }),
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error('takePictureAsync timeout after 10s')), 10000)
        ),
      ]);
      if (!photo?.uri) {
        Sentry.addBreadcrumb({
          category: 'scan',
          message: 'takePictureAsync returned no URI',
          level: 'warning',
        });
        return;
      }

      // Heal corruption from the pre-#174 bug: if an earlier version of the app
      // wrote a File (not a Directory) at the scan-queue path, delete it so the
      // Directory.create() call below doesn't collide with it.
      stage = 'dir_create';
      try {
        const stalePath = new File(Paths.document, 'scan-queue');
        if (stalePath.exists) {
          stalePath.delete();
          Sentry.addBreadcrumb({
            category: 'scan',
            message: 'Removed stale scan-queue file from pre-#174 state',
            level: 'info',
          });
        }
      } catch {
        // Best-effort cleanup — if this throws we'll still attempt create() below
        // and surface that failure to the user.
      }

      // Copy temp file to persistent document directory. idempotent: true means
      // create() is a no-op when the directory already exists (expo-file-system
      // otherwise throws). intermediates: true guards first-launch edge cases.
      const destDir = new Directory(Paths.document, 'scan-queue');
      destDir.create({ intermediates: true, idempotent: true });

      stage = 'file_copy';
      // Sequence counter prevents filename collisions if two captures land in
      // the same millisecond (double-tap race).
      const seq = captureSeq.current++;
      const destFile = new File(destDir, `${Date.now()}-${seq}.jpg`);
      const sourceFile = new File(photo.uri);
      sourceFile.copy(destFile);

      stage = 'start_scan';
      Sentry.addBreadcrumb({
        category: 'scan',
        message: 'Photo saved, starting scan',
        level: 'info',
        data: { destUri: destFile.uri },
      });
      startScan('image', destFile.uri);
    } catch (error) {
      Sentry.captureException(error, {
        tags: { feature: 'camera-capture', stage },
        extra: { facing },
      });
      // Give the user visible feedback instead of just logging to Sentry. The
      // original bug shipped precisely because this catch was silent.
      showBanner({
        message: t('scanFailedMessage'),
        type: 'error',
        actions: [{ label: t('retryNow'), onPress: () => handleCapture() }],
        duration: 8000,
      });
    } finally {
      setCapturing(false);
    }
  }

  return { cameraRef, webInputRef, capturing, handleCapture, handleWebFileChange };
}
