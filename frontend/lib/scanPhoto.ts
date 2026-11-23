import { ImageManipulator, SaveFormat } from 'expo-image-manipulator';
import { File } from 'expo-file-system';

/**
 * Scan photos are shrunk at capture (#668, docs/storage-and-retention.md G2).
 * The one place these sizes live.
 *
 * SCAN_PHOTO_MAX_LONG_EDGE_PX mirrors the server's `max_image_long_edge_px`
 * for the scan features (backend/app/ai/registry.py, 1568): the server
 * downsizes to that edge before the model sees the photo, so pixels beyond it
 * are never used. Keep the two in step. A 12 MP photo drops from about 2.5 MB
 * to 300-600 KB, which also keeps every upload under the server's 5 MB limit.
 */
export const SCAN_PHOTO_MAX_LONG_EDGE_PX = 1568;
/** JPEG quality of the saved scan photo (0 to 1). */
export const SCAN_PHOTO_JPEG_QUALITY = 0.8;

/**
 * Re-encode a photo as an upright JPEG with its long edge at most
 * SCAN_PHOTO_MAX_LONG_EDGE_PX (never upscaled) at SCAN_PHOTO_JPEG_QUALITY.
 * The result is a new temporary file; the input is left alone (see
 * `removeTempFile`). The pixels are rotated upright (EXIF orientation applied
 * on load) and the output carries no EXIF.
 *
 * On native the load itself is bounded (downsampled while decoding, so a
 * 48 MP photo is never held at full size). The web implementation ignores the
 * load bounds, so the render is checked and resized when still too large.
 */
export async function shrinkScanPhoto(uri: string): Promise<{ uri: string }> {
  const max = SCAN_PHOTO_MAX_LONG_EDGE_PX;
  const context = ImageManipulator.manipulate(uri, { maxWidth: max, maxHeight: max });
  try {
    let image = await context.renderAsync();
    if (Math.max(image.width, image.height) > max) {
      context.resize(image.width >= image.height ? { width: max } : { height: max });
      image = await context.renderAsync();
    }
    return await image.saveAsync({ format: SaveFormat.JPEG, compress: SCAN_PHOTO_JPEG_QUALITY });
  } finally {
    try {
      context.release();
    } catch {
      // Best effort: the garbage collector frees it otherwise.
    }
  }
}

/** Delete a temporary file (the camera original, a resized copy); never throws. */
export function removeTempFile(uri: string | undefined): void {
  if (!uri) return;
  try {
    const file = new File(uri);
    if (file.exists) file.delete();
  } catch {
    // Best effort: the OS clears the cache directory.
  }
}
