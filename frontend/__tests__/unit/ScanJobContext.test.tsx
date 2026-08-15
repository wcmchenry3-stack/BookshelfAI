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

jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: (...args: unknown[]) => mockLoadJobs(...args),
  saveJobs: (...args: unknown[]) => mockSaveJobs(...args),
  clearJobs: (...args: unknown[]) => mockClearJobs(...args),
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

jest.mock('expo-file-system', () => ({
  deleteAsync: jest.fn().mockResolvedValue(undefined),
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
});

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
    mockPost.mockResolvedValueOnce({ data: [{ title: 'Dune', author: 'Herbert' }] });
    const { result } = await renderScanJobs();

    await act(async () => {
      await result.current.startScan('image', 'file:///docs/scan-queue/photo.jpg');
    });

    const job = result.current.jobs.find((j) => j.type === 'image');
    expect(job?.status).toBe('complete');
    expect(mockPost).toHaveBeenCalledWith('/scan', expect.any(FormData), expect.any(Object));
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

describe('ScanJobContext — handleSelectBook', () => {
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
      await result.current.handleSelectBook({
        title: 'Dune',
        author: 'Herbert',
        subjects: [],
        confidence: 0.9,
        already_in_library: false,
        editions: [],
      });
    });

    expect(mockPost).toHaveBeenCalledWith('/wishlist', expect.objectContaining({ title: 'Dune' }));
    expect(result.current.reviewingJob).toBeNull();
  });
});
