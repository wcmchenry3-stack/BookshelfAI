import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useScanJobs } from '../../hooks/useScanJobs';

// ── Mocks ───────────────────────────────────────────────────────────────────

const mockPost = jest.fn();
const mockGet = jest.fn();

jest.mock('../../lib/api', () => ({
  api: {
    post: (...args: unknown[]) => mockPost(...args),
    get: (...args: unknown[]) => mockGet(...args),
  },
}));

const mockLoadJobs = jest.fn().mockResolvedValue([]);
const mockSaveJobs = jest.fn();
const mockClearJobs = jest.fn();
const mockSweep = jest.fn();
const mockDeleteScanImage = jest.fn();

jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: (...args: unknown[]) => mockLoadJobs(...args),
  saveJobs: (...args: unknown[]) => mockSaveJobs(...args),
  clearJobs: (...args: unknown[]) => mockClearJobs(...args),
  sweepOrphanedScanFiles: (...args: unknown[]) => mockSweep(...args),
  deleteScanImage: (...args: unknown[]) => mockDeleteScanImage(...args),
}));

const mockNetInfoFetch = jest.fn().mockResolvedValue({ isConnected: true });
const mockNetInfoAddEventListener = jest.fn().mockReturnValue(() => {});

jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => mockNetInfoFetch(),
    addEventListener: (cb: unknown) => mockNetInfoAddEventListener(cb),
  },
}));

let mockUuidCounter = 0;
jest.mock('expo-crypto', () => ({
  randomUUID: () => `test-uuid-${++mockUuidCounter}`,
}));

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#007AFF',
        success: '#16A34A',
        error: '#DC2626',
      },
      typography: { fontSizeBase: 16 },
    },
  }),
}));

// ── Helpers ──────────────────────────────────────────────────────────────────

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

async function renderScanJobs() {
  const hook = await renderHook(() => useScanJobs(), { wrapper });
  // Wait for mount effect (loadJobs) to complete and state to settle.
  await waitFor(() => {
    expect(mockSaveJobs).toHaveBeenCalled();
  });
  return hook;
}

// ── Tests ────────────────────────────────────────────────────────────────────

beforeEach(() => {
  jest.resetAllMocks();
  mockUuidCounter = 0;
  mockLoadJobs.mockResolvedValue([]);
  mockNetInfoFetch.mockResolvedValue({ isConnected: true });
  mockNetInfoAddEventListener.mockReturnValue(() => {});
  mockSaveJobs.mockResolvedValue(undefined);
  mockSweep.mockResolvedValue(0);
  mockDeleteScanImage.mockResolvedValue(undefined);
});

function scanResponse(books: { title: string; author: string }[], credits = 5) {
  return { data: { books, enhanced: false, enhanced_scan_credits: credits } };
}

const BOOK = {
  title: 'Dune',
  author: 'Herbert',
  subjects: [],
  confidence: 0.9,
  already_in_library: false,
  editions: [],
};

function queuedJob(i: number) {
  return {
    id: `queued-${i}`,
    type: 'image' as const,
    status: 'queued' as const,
    createdAt: 1000 + i,
    imageUri: `file:///docs/scan-queue/${1000 + i}-0.jpg`,
    retryCount: 0,
  };
}

describe('ScanJobContext — startScan', () => {
  it('creates a job and fires text search API call', async () => {
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });

    const job = result.current.jobs.find((j) => j.type === 'text');
    expect(job).toBeTruthy();
    expect(job?.status).toBe('complete');
    expect(job?.results).toHaveLength(1);
    expect(mockGet).toHaveBeenCalledWith('/books/search', { params: { q: 'Dune' } });
  });

  it('creates a job and fires image scan API call', async () => {
    mockPost.mockResolvedValueOnce(scanResponse([{ title: 'Dune', author: 'Herbert' }]));
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });

    const job = result.current.jobs.find((j) => j.type === 'image');
    expect(job?.status).toBe('complete');
    expect(mockPost).toHaveBeenCalledWith('/scan', expect.any(FormData), expect.any(Object));
  });

  it('keeps the image after a scan completes so an enhanced re-scan can use it', async () => {
    mockPost.mockResolvedValueOnce(scanResponse([{ title: 'Dune', author: 'Herbert' }]));
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });

    expect(mockDeleteScanImage).not.toHaveBeenCalled();
  });

  it('stores every book found in a multi-book photo and records credits', async () => {
    mockPost.mockResolvedValueOnce(
      scanResponse(
        [
          { title: 'Dune', author: 'Herbert' },
          { title: 'Emma', author: 'Austen' },
          { title: 'Ulysses', author: 'Joyce' },
        ],
        4
      )
    );
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });

    expect(result.current.jobs[0].results?.map((b) => b.title)).toEqual([
      'Dune',
      'Emma',
      'Ulysses',
    ]);
    expect(result.current.enhancedCredits).toBe(4);
  });

  it('does not delete the image when the scan fails (kept for retry)', async () => {
    mockGet.mockRejectedValueOnce(new Error('network'));
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });

    expect(mockDeleteScanImage).not.toHaveBeenCalled();
  });

  it('sets job status to failed on API error', async () => {
    mockGet.mockRejectedValueOnce(new Error('network'));
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('text', undefined, 'xyz');
    });

    const job = result.current.jobs[0];
    expect(job?.status).toBe('failed');
    expect(job?.error).toBe('network_or_server');
  });

  it('sets job status to failed when no results returned', async () => {
    mockGet.mockResolvedValueOnce({ data: [] });
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('text', undefined, 'nothing');
    });

    const job = result.current.jobs[0];
    expect(job?.status).toBe('failed');
    expect(job?.error).toBe('no_results');
  });

  it('queues job when offline', async () => {
    mockNetInfoFetch.mockResolvedValue({ isConnected: false });
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });

    const job = result.current.jobs[0];
    expect(job?.status).toBe('queued');
    expect(mockGet).not.toHaveBeenCalled();
  });
});

describe('ScanJobContext — retryScan', () => {
  it('increments retry count and re-fires with pre-loaded job', async () => {
    mockLoadJobs.mockResolvedValue([
      {
        id: 'retry-job',
        type: 'text' as const,
        status: 'failed' as const,
        createdAt: Date.now(),
        query: 'Dune',
        retryCount: 0,
        error: 'network_or_server',
      },
    ]);
    const { result } = await renderScanJobs();
    expect(result.current.jobs[0]?.status).toBe('failed');

    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });
    await act(async () => {
      await result.current.retryScan('retry-job');
    });

    await waitFor(() => {
      expect(result.current.jobs[0]?.status).toBe('complete');
    });
    expect(result.current.jobs[0]?.retryCount).toBe(1);
  });

  it('auto-queues when max retries reached', async () => {
    mockLoadJobs.mockResolvedValue([
      {
        id: 'existing-job',
        type: 'text',
        status: 'failed',
        createdAt: Date.now(),
        query: 'test',
        retryCount: 3,
      },
    ]);
    const { result } = await renderScanJobs();
    expect(result.current.jobs).toHaveLength(1);

    await act(async () => {
      await result.current.retryScan('existing-job');
    });

    const job = result.current.jobs.find((j) => j.id === 'existing-job');
    expect(job?.status).toBe('queued');
  });
});

describe('ScanJobContext — queueForLater', () => {
  it('sets job status to queued', async () => {
    const { result } = await renderScanJobs();

    mockGet.mockRejectedValueOnce(new Error('fail'));
    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });
    expect(result.current.jobs[0]?.status).toBe('failed');

    await act(() => {
      result.current.queueForLater(result.current.jobs[0].id);
    });

    expect(result.current.jobs[0]?.status).toBe('queued');
  });
});

describe('ScanJobContext — queue cap and pending count', () => {
  it('reports pendingCount for queued and pending jobs only', async () => {
    mockLoadJobs.mockResolvedValue([
      queuedJob(1),
      { ...queuedJob(2), status: 'pending' },
      { ...queuedJob(3), status: 'complete' },
      { ...queuedJob(4), status: 'failed' },
    ]);
    const { result } = await renderScanJobs();
    expect(result.current.pendingCount).toBe(2);
    expect(result.current.isQueueFull).toBe(false);
  });

  it('flags the queue as full at 50 pending jobs', async () => {
    mockLoadJobs.mockResolvedValue(Array.from({ length: 50 }, (_, i) => queuedJob(i)));
    const { result } = await renderScanJobs();
    expect(result.current.pendingCount).toBe(50);
    expect(result.current.isQueueFull).toBe(true);
  });

  it('does not count completed jobs toward the cap', async () => {
    mockLoadJobs.mockResolvedValue(
      Array.from({ length: 50 }, (_, i) => ({ ...queuedJob(i), status: 'complete' }))
    );
    const { result } = await renderScanJobs();
    expect(result.current.isQueueFull).toBe(false);
  });

  it('rejects startScan when full, deletes the copied image and does not add a job', async () => {
    mockLoadJobs.mockResolvedValue(Array.from({ length: 50 }, (_, i) => queuedJob(i)));
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/new.jpg');
    });

    expect(result.current.jobs).toHaveLength(50);
    expect(mockDeleteScanImage).toHaveBeenCalledWith('file:///docs/scan-queue/new.jpg');
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('accepts a scan when one slot is free', async () => {
    mockLoadJobs.mockResolvedValue(Array.from({ length: 49 }, (_, i) => queuedJob(i)));
    mockNetInfoFetch.mockResolvedValue({ isConnected: false });
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/new.jpg');
    });

    expect(result.current.jobs).toHaveLength(50);
    expect(result.current.isQueueFull).toBe(true);
    expect(mockDeleteScanImage).not.toHaveBeenCalled();
  });
});

describe('ScanJobContext — persistence', () => {
  it('persists jobs to storage on change', async () => {
    await renderScanJobs();

    await waitFor(() => {
      expect(mockSaveJobs).toHaveBeenCalled();
    });
  });

  it('loads persisted jobs on mount', async () => {
    mockLoadJobs.mockResolvedValue([
      {
        id: 'persisted-job',
        type: 'text',
        status: 'queued',
        createdAt: Date.now(),
        query: 'test',
        retryCount: 0,
      },
    ]);
    const { result } = await renderScanJobs();
    expect(result.current.jobs).toHaveLength(1);
    expect(result.current.jobs[0].id).toBe('persisted-job');
  });

  it('sweeps orphaned scan files against the restored jobs on mount', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1)]);
    await renderScanJobs();
    await waitFor(() => expect(mockSweep).toHaveBeenCalledTimes(1));
    const [jobs, cutoff] = mockSweep.mock.calls[0];
    expect(jobs).toHaveLength(1);
    expect(jobs[0].id).toBe('queued-1');
    expect(typeof cutoff).toBe('number');
  });

  it('does not sweep or immediately re-save when the initial load fails', async () => {
    // loadJobs() returns null (not []) when the persisted queue couldn't be
    // read — treating that as "known empty" would let the sweep delete
    // images for jobs we simply failed to read back.
    mockLoadJobs.mockResolvedValue(null);
    const { result } = await renderHook(() => useScanJobs(), { wrapper });

    await waitFor(() => expect(mockLoadJobs).toHaveBeenCalled());
    // Give the mount effect's microtasks a tick to settle.
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.jobs).toEqual([]);
    expect(mockSweep).not.toHaveBeenCalled();
    expect(mockSaveJobs).not.toHaveBeenCalled();
  });

  it('resumes normal persistence once jobs change after a failed load', async () => {
    mockLoadJobs.mockResolvedValue(null);
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });
    const { result } = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(mockLoadJobs).toHaveBeenCalled());

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });

    // A real state change after the failed load persists normally.
    await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  });

  it('resets interrupted searching jobs to pending on mount', async () => {
    mockLoadJobs.mockResolvedValue([
      {
        id: 'interrupted-job',
        type: 'text',
        status: 'searching',
        createdAt: Date.now(),
        query: 'test',
        retryCount: 0,
      },
    ]);
    const { result } = await renderScanJobs();
    expect(result.current.jobs[0].status).toBe('pending');
  });
});

describe('ScanJobContext — dismissJob', () => {
  it('removes job from list', async () => {
    const { result } = await renderScanJobs();

    mockGet.mockRejectedValueOnce(new Error('fail'));
    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });
    expect(result.current.jobs).toHaveLength(1);
    const jobId = result.current.jobs[0].id;

    await act(() => {
      result.current.dismissJob(jobId);
    });

    expect(result.current.jobs).toHaveLength(0);
  });

  it('deletes the persisted image when a job is dismissed', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1)]);
    const { result } = await renderScanJobs();

    await act(() => {
      result.current.dismissJob('queued-1');
    });

    expect(mockDeleteScanImage).toHaveBeenCalledWith(queuedJob(1).imageUri);
  });
});

describe('ScanJobContext — reviewJob', () => {
  it('sets reviewingJob when called with valid job id', async () => {
    const { result } = await renderScanJobs();
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });
    expect(result.current.jobs[0]?.status).toBe('complete');

    await act(() => {
      result.current.reviewJob(result.current.jobs[0].id);
    });

    expect(result.current.reviewingJob).toBeTruthy();
    expect(result.current.reviewingJob?.id).toBe(result.current.jobs[0].id);
  });

  it('dismissReview clears reviewingJob', async () => {
    const { result } = await renderScanJobs();
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });

    await act(() => {
      result.current.reviewJob(result.current.jobs[0].id);
    });
    expect(result.current.reviewingJob).toBeTruthy();

    await act(() => {
      result.current.dismissReview();
    });
    expect(result.current.reviewingJob).toBeNull();
  });
});

describe('ScanJobContext — handleAddBooks', () => {
  it('posts to wishlist and removes job on success', async () => {
    const { result } = await renderScanJobs();
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });
    mockPost.mockResolvedValueOnce({});

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });
    expect(result.current.jobs[0]?.status).toBe('complete');

    await act(() => {
      result.current.reviewJob(result.current.jobs[0].id);
    });

    await act(async () => {
      await result.current.handleAddBooks([BOOK]);
    });

    expect(mockPost).toHaveBeenCalledWith('/wishlist', expect.objectContaining({ title: 'Dune' }));
    expect(result.current.reviewingJob).toBeNull();
  });

  it('does not post to the wishlist while offline and keeps the job under review', async () => {
    const { result } = await renderScanJobs();
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });
    await act(() => {
      result.current.reviewJob(result.current.jobs[0].id);
    });

    // Connection drops after results arrive, before the user picks a book.
    mockNetInfoFetch.mockResolvedValue({ isConnected: false });
    await act(async () => {
      await result.current.handleAddBooks([BOOK]);
    });

    expect(mockPost).not.toHaveBeenCalled();
    expect(result.current.reviewingJob).not.toBeNull();
    expect(result.current.jobs).toHaveLength(1);
  });

  it('treats an unknown connection state (isConnected: null) as online, not offline', async () => {
    const { result } = await renderScanJobs();
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });

    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });
    await act(() => {
      result.current.reviewJob(result.current.jobs[0].id);
    });

    mockNetInfoFetch.mockResolvedValue({ isConnected: null });
    mockPost.mockResolvedValueOnce({});
    await act(async () => {
      await result.current.handleAddBooks([BOOK]);
    });

    expect(mockPost).toHaveBeenCalledWith('/wishlist', expect.objectContaining({ title: 'Dune' }));
  });
});

describe('ScanJobContext — multi-book review', () => {
  async function scanPhoto(result: { current: ReturnType<typeof useScanJobs> }, books: string[]) {
    mockPost.mockResolvedValueOnce(scanResponse(books.map((title) => ({ title, author: 'A' }))));
    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });
    await act(() => {
      result.current.reviewJob(result.current.jobs[0].id);
    });
  }

  it('adds every ticked book to the wishlist and discards the job and its image', async () => {
    const { result } = await renderScanJobs();
    await scanPhoto(result, ['Dune', 'Emma']);
    mockPost.mockResolvedValue({});

    await act(async () => {
      await result.current.handleAddBooks([
        { ...BOOK, title: 'Dune' },
        { ...BOOK, title: 'Emma' },
      ]);
    });

    expect(mockPost).toHaveBeenCalledWith('/wishlist', expect.objectContaining({ title: 'Dune' }));
    expect(mockPost).toHaveBeenCalledWith('/wishlist', expect.objectContaining({ title: 'Emma' }));
    expect(result.current.jobs).toHaveLength(0);
    expect(result.current.reviewingJob).toBeNull();
    expect(mockDeleteScanImage).toHaveBeenCalledWith('file:///docs/scan-queue/photo.jpg');
  });

  it('keeps only the books that failed to save under review', async () => {
    const { result } = await renderScanJobs();
    await scanPhoto(result, ['Dune', 'Emma']);
    mockPost.mockImplementation((_url: string, book: { title: string }) =>
      book.title === 'Emma' ? Promise.reject(new Error('500')) : Promise.resolve({})
    );

    await act(async () => {
      await result.current.handleAddBooks([
        { ...BOOK, title: 'Dune' },
        { ...BOOK, title: 'Emma' },
      ]);
    });

    expect(result.current.reviewingJob?.results?.map((b) => b.title)).toEqual(['Emma']);
    expect(mockDeleteScanImage).not.toHaveBeenCalled();
  });

  it('closing the review discards a completed job and frees its image', async () => {
    const { result } = await renderScanJobs();
    await scanPhoto(result, ['Dune']);

    await act(() => {
      result.current.dismissReview();
    });

    expect(result.current.jobs).toHaveLength(0);
    expect(mockDeleteScanImage).toHaveBeenCalledWith('file:///docs/scan-queue/photo.jpg');
  });

  it('drops restored completed jobs — their results are not persisted', async () => {
    mockLoadJobs.mockResolvedValue([
      { ...queuedJob(1), id: 'done', status: 'complete' },
      queuedJob(2),
    ]);
    const { result } = await renderScanJobs();

    await waitFor(() => {
      expect(result.current.jobs.map((j) => j.id)).toEqual(['queued-2']);
    });
    expect(mockSweep).toHaveBeenCalledWith(
      [expect.objectContaining({ id: 'queued-2' })],
      expect.any(Number)
    );
  });
});

describe('ScanJobContext — enhanced scan', () => {
  function enhancedField(call: unknown[]): unknown {
    const form = call[1] as FormData & { _parts?: [string, unknown][] };
    if (typeof form.get === 'function') return form.get('enhanced') ?? undefined;
    return Object.fromEntries(form._parts ?? []).enhanced;
  }

  it('re-runs the photo with the enhanced flag and replaces the results', async () => {
    const { result } = await renderScanJobs();
    mockPost.mockResolvedValueOnce(scanResponse([{ title: 'Dune', author: 'A' }], 5));
    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });
    expect(enhancedField(mockPost.mock.calls[0])).toBeUndefined();

    mockPost.mockResolvedValueOnce({
      data: {
        books: [
          { title: 'Dune', author: 'A' },
          { title: 'Emma', author: 'B' },
        ],
        enhanced: true,
        enhanced_scan_credits: 4,
      },
    });
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });

    expect(enhancedField(mockPost.mock.calls[1])).toBe('true');
    expect(result.current.jobs[0].status).toBe('complete');
    expect(result.current.jobs[0].results).toHaveLength(2);
    expect(result.current.enhancedCredits).toBe(4);
  });

  it('keeps the standard results when the enhanced scan finds nothing', async () => {
    const { result } = await renderScanJobs();
    mockPost.mockResolvedValueOnce(scanResponse([{ title: 'Dune', author: 'A' }]));
    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });

    mockPost.mockResolvedValueOnce(scanResponse([]));
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });

    expect(result.current.jobs[0].status).toBe('complete');
    expect(result.current.jobs[0].results?.map((b) => b.title)).toEqual(['Dune']);
    // The sheet closed when the re-scan started — it must reopen on the earlier books.
    expect(result.current.reviewingJob?.id).toBe(result.current.jobs[0].id);
  });

  it('reopens the earlier results when the enhanced scan errors', async () => {
    const { result } = await renderScanJobs();
    mockPost.mockResolvedValueOnce(scanResponse([{ title: 'Dune', author: 'A' }]));
    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });

    mockPost.mockRejectedValueOnce({ response: { status: 503 } });
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });

    expect(result.current.jobs[0]).toEqual(
      expect.objectContaining({ status: 'complete', enhanced: false })
    );
    expect(result.current.reviewingJob?.results?.map((b) => b.title)).toEqual(['Dune']);
  });

  it('reopens the earlier results when out of credits', async () => {
    const { result } = await renderScanJobs();
    mockPost.mockResolvedValueOnce(scanResponse([{ title: 'Dune', author: 'A' }]));
    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });

    mockPost.mockRejectedValueOnce({ response: { status: 402 } });
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });

    expect(result.current.enhancedCredits).toBe(0);
    expect(result.current.reviewingJob?.status).toBe('complete');
    expect(result.current.reviewingJob?.results?.map((b) => b.title)).toEqual(['Dune']);
  });

  it('falls back and records zero credits when the server says none are left', async () => {
    const { result } = await renderScanJobs();
    mockPost.mockResolvedValueOnce(scanResponse([]));
    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });
    expect(result.current.jobs[0].status).toBe('failed');

    mockPost.mockRejectedValueOnce({ response: { status: 402 } });
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });

    expect(result.current.enhancedCredits).toBe(0);
    expect(result.current.jobs[0]).toEqual(
      expect.objectContaining({ status: 'failed', enhanced: false, error: 'no_results' })
    );
  });

  it('ignores enhanced requests for text searches', async () => {
    const { result } = await renderScanJobs();
    mockGet.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });
    await act(async () => {
      await result.current.startScan('text', undefined, 'Dune');
    });

    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });

    expect(mockPost).not.toHaveBeenCalled();
  });
});
