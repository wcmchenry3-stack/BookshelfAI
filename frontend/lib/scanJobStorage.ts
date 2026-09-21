import { Platform } from 'react-native';
import { Directory, File, Paths } from 'expo-file-system';

import type { ScanJob } from './scanJob';

const STORAGE_KEY = 'scan_job_queue';
// Directory under Paths.document where captures are persisted (see scan.tsx).
export const SCAN_QUEUE_DIR = 'scan-queue';

// Web: in-memory only (no offline persistence needed).
const _webStore = new Map<string, string>();

async function getStorage() {
  if (Platform.OS === 'web') {
    return {
      getItem: (key: string) => _webStore.get(key) ?? null,
      setItem: (key: string, value: string) => {
        _webStore.set(key, value);
      },
      removeItem: (key: string) => {
        _webStore.delete(key);
      },
    };
  }
  const AsyncStorage = (await import('@react-native-async-storage/async-storage')).default;
  return AsyncStorage;
}

export async function saveJobs(jobs: ScanJob[]): Promise<void> {
  const storage = await getStorage();
  // Strip results from persisted jobs to keep storage size small.
  // Results are only useful for the current session.
  const stripped = jobs.map((j) => ({ ...j, results: undefined }));
  storage.setItem(STORAGE_KEY, JSON.stringify(stripped));
}

export async function loadJobs(): Promise<ScanJob[]> {
  try {
    const storage = await getStorage();
    const raw = await storage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed;
  } catch {
    return [];
  }
}

export async function clearJobs(): Promise<void> {
  const storage = await getStorage();
  storage.removeItem(STORAGE_KEY);
}

function fileNameOf(uri: string): string {
  return uri.split('/').pop() ?? uri;
}

/** Delete a persisted scan image. Best-effort: never throws. No-op on web. */
export async function deleteScanImage(uri: string): Promise<void> {
  if (Platform.OS === 'web') return;
  try {
    const file = new File(uri);
    if (file.exists) file.delete();
  } catch {
    // Best-effort — the next launch's orphan sweep will retry.
  }
}

/**
 * Remove files in the document-directory `scan-queue/` that no job refers to
 * (e.g. left behind by a crash between copying a capture and saving its job).
 * Files are named `<Date.now()>-<seq>.jpg` (see scan.tsx); any file stamped at or
 * after `cutoff` is left alone so a capture taken while the sweep runs isn't
 * deleted before its job is saved. Returns the number of files removed.
 * Best-effort: never throws. No-op on web.
 */
export async function sweepOrphanedScanFiles(
  jobs: ScanJob[],
  cutoff: number = Date.now()
): Promise<number> {
  if (Platform.OS === 'web') return 0;
  try {
    const dir = new Directory(Paths.document, SCAN_QUEUE_DIR);
    if (!dir.exists) return 0;

    const referenced = new Set(jobs.filter((j) => j.imageUri).map((j) => fileNameOf(j.imageUri!)));
    let removed = 0;
    for (const entry of dir.list()) {
      if (!(entry instanceof File)) continue;
      const name = fileNameOf(entry.uri);
      if (referenced.has(name) || Number.parseInt(name, 10) >= cutoff) continue;
      try {
        entry.delete();
        removed++;
      } catch {
        // Skip files we can't delete; they'll be retried on the next launch.
      }
    }
    return removed;
  } catch {
    return 0;
  }
}
