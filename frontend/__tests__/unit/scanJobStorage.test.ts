import { Platform } from 'react-native';
import {
  saveJobs,
  loadJobs,
  clearJobs,
  sweepOrphanedScanFiles,
  deleteScanImage,
  checkCaptureStorage,
} from '../../lib/scanJobStorage';
import {
  ENFORCE_QUEUE_BYTE_CAP,
  isPendingUpload,
  LOW_FREE_SPACE_BYTES,
  MAX_QUEUE_BYTES,
  MAX_QUEUE_SIZE,
  type ScanJob,
} from '../../lib/scanJob';

// Minimal in-memory stand-in for the expo-file-system Directory/File API.
// `mockDirEntries` is the current contents of scan-queue/ (file names).
let mockDirExists = true;
let mockDirEntries: string[] = [];
const mockDeleted: string[] = [];
const mockUndeletable = new Set<string>();
let mockFreeSpace = 10 * 1024 * 1024 * 1024;
const mockFileSizes: Record<string, number> = {};

jest.mock('expo-file-system', () => {
  class MockFile {
    uri: string;
    exists = true;
    constructor(...parts: unknown[]) {
      this.uri = parts
        .map((p) => (typeof p === 'object' && p !== null && 'uri' in p ? (p as MockFile).uri : p))
        .join('/');
    }
    get size() {
      return mockFileSizes[this.uri.split('/').pop() as string] ?? 0;
    }
    delete() {
      if (mockUndeletable.has(this.uri)) throw new Error('EPERM');
      mockDeleted.push(this.uri);
    }
  }
  class MockDirectory {
    uri: string;
    constructor(...parts: unknown[]) {
      this.uri = parts.join('/');
    }
    get exists() {
      return mockDirExists;
    }
    list() {
      return [
        ...mockDirEntries.map((n) => new MockFile(this.uri, n)),
        // A nested directory must never be deleted by the sweep.
        new MockDirectory(this.uri, 'nested'),
      ];
    }
  }
  return {
    File: MockFile,
    Directory: MockDirectory,
    Paths: {
      document: 'file:///docs',
      get availableDiskSpace() {
        return mockFreeSpace;
      },
    },
  };
});

// Force web platform so we use the in-memory store (no AsyncStorage needed).
beforeAll(() => {
  Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
});

afterAll(() => {
  Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
});

const sampleJob: ScanJob = {
  id: 'test-1',
  type: 'text',
  status: 'queued',
  createdAt: 1234567890,
  query: 'Dune',
  retryCount: 0,
};

describe('scanJobStorage', () => {
  beforeEach(async () => {
    await clearJobs();
  });

  it('round-trips save and load', async () => {
    await saveJobs([sampleJob]);
    const loaded = await loadJobs();
    if (!loaded) throw new Error('expected loadJobs to succeed');
    expect(loaded).toHaveLength(1);
    expect(loaded[0].id).toBe('test-1');
    expect(loaded[0].query).toBe('Dune');
  });

  it('returns empty array when nothing stored', async () => {
    const loaded = await loadJobs();
    expect(loaded).toEqual([]);
  });

  it('strips results from persisted jobs to save space', async () => {
    const jobWithResults: ScanJob = {
      ...sampleJob,
      status: 'complete',
      results: [
        {
          title: 'Dune',
          author: 'Herbert',
          subjects: [],
          confidence: 0.9,
          already_in_library: false,
          editions: [],
        },
      ],
    };
    await saveJobs([jobWithResults]);
    const loaded = await loadJobs();
    if (!loaded) throw new Error('expected loadJobs to succeed');
    expect(loaded[0].results).toBeUndefined();
  });

  it('clearJobs removes all stored data', async () => {
    await saveJobs([sampleJob]);
    await clearJobs();
    const loaded = await loadJobs();
    expect(loaded).toEqual([]);
  });

  it('handles multiple jobs', async () => {
    const jobs = [sampleJob, { ...sampleJob, id: 'test-2', query: 'Foundation' }];
    await saveJobs(jobs);
    const loaded = await loadJobs();
    expect(loaded).toHaveLength(2);
  });

  it('returns null (not []) when the stored JSON is corrupt', async () => {
    await saveJobs([sampleJob]);
    jest.spyOn(JSON, 'parse').mockImplementationOnce(() => {
      throw new SyntaxError('Unexpected token');
    });
    expect(await loadJobs()).toBeNull();
    jest.restoreAllMocks();
  });

  it('returns null (not []) when the stored value is not an array', async () => {
    await saveJobs([sampleJob]);
    jest.spyOn(JSON, 'parse').mockReturnValueOnce({ not: 'an array' });
    expect(await loadJobs()).toBeNull();
    jest.restoreAllMocks();
  });
});

describe('sweepOrphanedScanFiles', () => {
  const dir = 'file:///docs/scan-queue';
  const jobWithImage = (name: string, id = name): ScanJob => ({
    ...sampleJob,
    id,
    type: 'image',
    imageUri: `${dir}/${name}`,
  });

  beforeAll(() => {
    Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
  });
  afterAll(() => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
  });
  beforeEach(() => {
    mockDirExists = true;
    mockDirEntries = [];
    mockDeleted.length = 0;
    mockUndeletable.clear();
  });

  it('deletes files that no job refers to and keeps referenced ones', async () => {
    mockDirEntries = ['100-0.jpg', '200-0.jpg', '300-0.jpg'];
    const removed = await sweepOrphanedScanFiles([jobWithImage('200-0.jpg')], 10_000);
    expect(removed).toBe(2);
    expect(mockDeleted.sort()).toEqual([`${dir}/100-0.jpg`, `${dir}/300-0.jpg`]);
  });

  it('keeps files for every job status (completed jobs still own their image)', async () => {
    mockDirEntries = ['100-0.jpg', '200-0.jpg'];
    const jobs = [
      { ...jobWithImage('100-0.jpg'), status: 'complete' as const },
      { ...jobWithImage('200-0.jpg'), status: 'failed' as const },
    ];
    expect(await sweepOrphanedScanFiles(jobs, 10_000)).toBe(0);
    expect(mockDeleted).toEqual([]);
  });

  it('deletes everything when there are no jobs', async () => {
    mockDirEntries = ['100-0.jpg', '200-0.jpg'];
    expect(await sweepOrphanedScanFiles([], 10_000)).toBe(2);
  });

  it('leaves files stamped at or after the cutoff (captured mid-sweep)', async () => {
    mockDirEntries = ['9999-0.jpg', '10000-0.jpg', '10001-0.jpg'];
    const removed = await sweepOrphanedScanFiles([], 10_000);
    expect(removed).toBe(1);
    expect(mockDeleted).toEqual([`${dir}/9999-0.jpg`]);
  });

  it('never deletes sub-directories', async () => {
    mockDirEntries = [];
    expect(await sweepOrphanedScanFiles([], 10_000)).toBe(0);
    expect(mockDeleted).toEqual([]);
  });

  it('is a no-op when scan-queue/ does not exist', async () => {
    mockDirExists = false;
    mockDirEntries = ['100-0.jpg'];
    expect(await sweepOrphanedScanFiles([], 10_000)).toBe(0);
    expect(mockDeleted).toEqual([]);
  });

  it('keeps going when a single file cannot be deleted', async () => {
    mockDirEntries = ['100-0.jpg', '200-0.jpg'];
    mockUndeletable.add(`${dir}/100-0.jpg`);
    expect(await sweepOrphanedScanFiles([], 10_000)).toBe(1);
    expect(mockDeleted).toEqual([`${dir}/200-0.jpg`]);
  });

  it('is a no-op on web', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
    mockDirEntries = ['100-0.jpg'];
    expect(await sweepOrphanedScanFiles([], 10_000)).toBe(0);
    expect(mockDeleted).toEqual([]);
    Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
  });

  it('syncs with persisted metadata: a job saved then loaded keeps its file', async () => {
    mockDirEntries = ['100-0.jpg', '200-0.jpg'];
    // Persist via the in-memory (web) store, then sweep as native.
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
    await saveJobs([jobWithImage('100-0.jpg')]);
    const loaded = await loadJobs();
    Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
    if (!loaded) throw new Error('expected loadJobs to succeed');
    expect(await sweepOrphanedScanFiles(loaded, 10_000)).toBe(1);
    expect(mockDeleted).toEqual([`${dir}/200-0.jpg`]);
  });
});

describe('deleteScanImage', () => {
  beforeAll(() => {
    Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
  });
  afterAll(() => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
  });
  beforeEach(() => {
    mockDeleted.length = 0;
    mockUndeletable.clear();
  });

  it('deletes the file at the given uri', async () => {
    await deleteScanImage('file:///docs/scan-queue/100-0.jpg');
    expect(mockDeleted).toEqual(['file:///docs/scan-queue/100-0.jpg']);
  });

  it('swallows deletion errors', async () => {
    mockUndeletable.add('file:///docs/scan-queue/100-0.jpg');
    await expect(deleteScanImage('file:///docs/scan-queue/100-0.jpg')).resolves.toBeUndefined();
  });
});

describe('queue cap helpers', () => {
  it('caps the queue at 50 scans', () => {
    expect(MAX_QUEUE_SIZE).toBe(50);
  });

  it.each([
    ['queued', true],
    ['pending', true],
    ['searching', false],
    ['complete', false],
    ['failed', false],
  ] as const)('isPendingUpload(%s) is %s', (status, expected) => {
    expect(isPendingUpload({ ...sampleJob, status })).toBe(expected);
  });
});

describe('checkCaptureStorage (#670, G5)', () => {
  const MB = 1024 * 1024;
  beforeEach(() => {
    Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
    mockDirExists = true;
    mockDirEntries = ['1-0.jpg', '2-0.jpg'];
    for (const k of Object.keys(mockFileSizes)) delete mockFileSizes[k];
    mockFreeSpace = 10 * 1024 * MB;
  });
  afterEach(() => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
    mockDirEntries = [];
  });

  it('uses the 100 MB and 50 MB budgets', () => {
    expect(LOW_FREE_SPACE_BYTES).toBe(100 * MB);
    expect(MAX_QUEUE_BYTES).toBe(50 * MB);
  });

  it('is ok with plenty of space and a small queue', () => {
    mockFileSizes['1-0.jpg'] = 2 * MB;
    expect(checkCaptureStorage()).toBe('ok');
  });

  it('refuses below the free-space threshold', () => {
    mockFreeSpace = LOW_FREE_SPACE_BYTES - 1;
    expect(checkCaptureStorage()).toBe('low_space');
  });

  it('allows exactly the free-space threshold', () => {
    mockFreeSpace = LOW_FREE_SPACE_BYTES;
    expect(checkCaptureStorage()).toBe('ok');
  });

  it('refuses once the queue folder holds 50 MB, summing every photo', () => {
    mockFileSizes['1-0.jpg'] = 30 * MB;
    mockFileSizes['2-0.jpg'] = 20 * MB;
    expect(checkCaptureStorage({ enforceByteCap: true })).toBe('queue_full');
    mockFileSizes['2-0.jpg'] = 20 * MB - 1;
    expect(checkCaptureStorage({ enforceByteCap: true })).toBe('ok');
  });

  it('does not enforce the 50 MB cap by default (off until #668, decision #1037)', () => {
    expect(ENFORCE_QUEUE_BYTE_CAP).toBe(false);
    mockFileSizes['1-0.jpg'] = 80 * MB;
    expect(checkCaptureStorage()).toBe('ok');
  });

  it('still refuses on low free space when the byte cap is off', () => {
    mockFreeSpace = LOW_FREE_SPACE_BYTES - 1;
    expect(checkCaptureStorage()).toBe('low_space');
    expect(checkCaptureStorage({ enforceByteCap: true })).toBe('low_space');
  });

  it('is ok when the queue folder does not exist yet', () => {
    mockDirExists = false;
    expect(checkCaptureStorage({ enforceByteCap: true })).toBe('ok');
  });

  it('does not block a capture when the disk APIs throw', () => {
    mockDirEntries = null as unknown as string[];
    expect(checkCaptureStorage({ enforceByteCap: true })).toBe('ok');
  });

  it('skips both checks on web', () => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
    mockFreeSpace = 0;
    mockFileSizes['1-0.jpg'] = 100 * MB;
    expect(checkCaptureStorage({ enforceByteCap: true })).toBe('ok');
  });
});
