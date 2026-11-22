import { useRef, useState } from 'react';
import { act, renderHook } from '@testing-library/react-native';

import type { EnrichedBook } from '../../components/BookCandidatePicker';
import type { PendingUnverifiedDraft } from '../../contexts/ScanJobContext';
import { candidateKey, useScanReview } from '../../hooks/useScanReview';
import type { BannerConfig } from '../../contexts/BannerContext';
import type { ScanJob } from '../../lib/scanJob';

// #805: add-books and the "Add it yourself" drafts (#616), moved out of
// ScanJobProvider, tested on their own. The drafts state lives in a small
// harness standing in for the provider.

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({ api: { post: (...a: unknown[]) => mockPost(...a) } }));
const mockNetFetch = jest.fn();
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: { fetch: () => mockNetFetch() },
}));
const mockCapture = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: (...a: unknown[]) => mockCapture(...a) },
}));

function book(title: string): EnrichedBook {
  return {
    title,
    author: `${title} author`,
    subjects: [],
    confidence: 0.9,
    already_in_library: false,
    editions: [],
  };
}

function refusal(status: number, detail: string) {
  return { response: { status, data: { detail }, headers: {} } };
}

const DUNE = book('Dune');
const EMMA = book('Emma');

function setup(initialJobs: ScanJob[], reviewing: string | null = 'job-1') {
  const jobsRef = { current: initialJobs };
  const updateJob = jest.fn((id: string, updates: Partial<ScanJob>) => {
    jobsRef.current = jobsRef.current.map((j) => (j.id === id ? { ...j, ...updates } : j));
  });
  const dismissJob = jest.fn((id: string) => {
    jobsRef.current = jobsRef.current.filter((j) => j.id !== id);
  });
  const showBanner = jest.fn<void, [BannerConfig]>();
  const generationRef = { current: 0 };
  const abortersRef = { current: new Set<AbortController>() };
  const draftSourcesRef = {
    current: new Map<string, { jobId: string | null; key: string }>(),
  };
  const hook = renderHook(() => {
    const [reviewingJobId, setReviewingJobId] = useState<string | null>(reviewing);
    const [unverifiedDrafts, setUnverifiedDrafts] = useState<PendingUnverifiedDraft[]>([]);
    const reviewingJobIdRef = useRef<string | null>(null);
    const reopenReviewJobIdRef = useRef<string | null>(null);
    const actions = useScanReview({
      jobsRef,
      updateJob,
      generationRef,
      abortersRef,
      showBanner,
      t: (key: string, opts?: Record<string, unknown>) =>
        opts && 'title' in opts ? `${key}:${String(opts.title)}` : key,
      reviewingJobId,
      setReviewingJobId,
      reviewingJobIdRef,
      dismissJob,
      unverifiedDrafts,
      setUnverifiedDrafts,
      reopenReviewJobIdRef,
      draftSourcesRef,
    });
    return { ...actions, reviewingJobId, unverifiedDrafts, reopenReviewJobIdRef };
  });
  return { hook, jobsRef, updateJob, dismissJob, showBanner, generationRef, abortersRef };
}

function reviewJob(results: EnrichedBook[], over: Partial<ScanJob> = {}): ScanJob {
  return {
    id: 'job-1',
    type: 'image',
    status: 'complete',
    createdAt: 1,
    imageUri: 'file:///job-1.jpg',
    retryCount: 0,
    results,
    ...over,
  };
}

beforeEach(() => {
  mockPost.mockReset();
  mockCapture.mockReset();
  mockNetFetch.mockReset().mockResolvedValue({ isConnected: true });
});

describe('handleAddBooks', () => {
  it('adds every book, closes the review and dismisses the job', async () => {
    mockPost.mockResolvedValue({ data: {} });
    const s = setup([reviewJob([DUNE])]);
    const { result } = await s.hook;
    await act(async () => {
      await result.current.handleAddBooks([DUNE]);
    });
    expect(mockPost).toHaveBeenCalledWith('/wishlist', DUNE, expect.anything());
    expect(s.dismissJob).toHaveBeenCalledWith('job-1');
    expect(result.current.reviewingJobId).toBeNull();
    expect(s.showBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'addedMessage:Dune', type: 'success' })
    );
    expect(s.abortersRef.current.size).toBe(0);
  });

  it('does not write offline', async () => {
    mockNetFetch.mockResolvedValue({ isConnected: false });
    const s = setup([reviewJob([DUNE])]);
    const { result } = await s.hook;
    await act(async () => {
      await result.current.handleAddBooks([DUNE]);
    });
    expect(mockPost).not.toHaveBeenCalled();
    expect(s.showBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'requiresConnection', type: 'error' })
    );
    expect(s.dismissJob).not.toHaveBeenCalled();
  });

  it('says nothing when signed out while the books were sent', async () => {
    const s = setup([reviewJob([DUNE])]);
    mockPost.mockImplementation(async () => {
      s.generationRef.current += 1;
      return { data: {} };
    });
    const { result } = await s.hook;
    await act(async () => {
      await result.current.handleAddBooks([DUNE]);
    });
    expect(s.showBanner).not.toHaveBeenCalled();
    expect(s.dismissJob).not.toHaveBeenCalled();
  });

  it('keeps a retryable failure in the review and reports it', async () => {
    mockPost.mockImplementation(async (_p: string, b: EnrichedBook) => {
      if (b.title === 'Emma')
        throw Object.assign(new Error('Network Error'), { response: undefined });
      return { data: {} };
    });
    const s = setup([reviewJob([DUNE, EMMA])]);
    const { result } = await s.hook;
    await act(async () => {
      await result.current.handleAddBooks([DUNE, EMMA]);
    });
    expect(s.updateJob).toHaveBeenCalledWith('job-1', { results: [EMMA] });
    expect(s.dismissJob).not.toHaveBeenCalled();
    expect(mockCapture).toHaveBeenCalledTimes(1);
    expect(s.showBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'addedSomeMessage', type: 'error' })
    );
  });

  it('offers "Add it yourself" for a book no catalog matched, which opens its draft', async () => {
    mockPost.mockRejectedValue(refusal(422, 'book_not_in_catalog'));
    const s = setup([reviewJob([DUNE])]);
    const { result } = await s.hook;
    await act(async () => {
      await result.current.handleAddBooks([DUNE]);
    });
    expect(mockCapture).not.toHaveBeenCalled();
    const banner = s.showBanner.mock.calls[0][0];
    expect(banner.message).toBe('notInCatalogMessage');
    expect(banner.actions?.[0].label).toBe('addItYourself');
    await act(async () => {
      banner.actions![0].onPress();
    });
    expect(result.current.unverifiedDrafts).toEqual([
      expect.objectContaining({ id: 'draft-1', title: 'Dune', author: 'Dune author' }),
    ]);
  });
});

describe('"Add it yourself" drafts', () => {
  it('addYourself closes the review, keeps the job and opens one draft per book', async () => {
    const s = setup([reviewJob([DUNE, EMMA])]);
    const { result } = await s.hook;
    await act(async () => {
      result.current.addYourself([DUNE, EMMA]);
    });
    expect(result.current.reviewingJobId).toBeNull();
    expect(result.current.unverifiedDrafts.map((d) => d.id)).toEqual(['draft-1', 'draft-2']);
    expect(result.current.reopenReviewJobIdRef.current).toBe('job-1');
    expect(s.dismissJob).not.toHaveBeenCalled();
  });

  it('addYourself with no books does nothing', async () => {
    const s = setup([reviewJob([DUNE])]);
    const { result } = await s.hook;
    await act(async () => {
      result.current.addYourself([]);
    });
    expect(result.current.reviewingJobId).toBe('job-1');
    expect(result.current.unverifiedDrafts).toEqual([]);
  });

  it('unverifiedSaved takes that book out of its scan, drops the draft and confirms', async () => {
    const s = setup([reviewJob([DUNE, EMMA])]);
    const { result } = await s.hook;
    await act(async () => {
      result.current.addYourself([DUNE, EMMA]);
    });
    await act(async () => {
      result.current.unverifiedSaved('Emma', 'draft-2');
    });
    expect(s.updateJob).toHaveBeenCalledWith('job-1', { results: [DUNE] });
    expect(result.current.unverifiedDrafts.map((d) => d.id)).toEqual(['draft-1']);
    expect(s.showBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'unverifiedSavedMessage:Emma', type: 'success' })
    );
  });

  it('unverifiedSaved without an id saves the draft shown first', async () => {
    const s = setup([reviewJob([DUNE, EMMA])]);
    const { result } = await s.hook;
    await act(async () => {
      result.current.addYourself([DUNE, EMMA]);
    });
    await act(async () => {
      result.current.unverifiedSaved('Dune');
    });
    expect(s.updateJob).toHaveBeenCalledWith('job-1', { results: [EMMA] });
    expect(result.current.unverifiedDrafts.map((d) => d.id)).toEqual(['draft-2']);
  });

  it('dismissUnverified closes the forms and reopens the review that still has books', async () => {
    const s = setup([reviewJob([DUNE])]);
    const { result } = await s.hook;
    await act(async () => {
      result.current.addYourself([DUNE]);
    });
    await act(async () => {
      result.current.dismissUnverified();
    });
    expect(result.current.unverifiedDrafts).toEqual([]);
    expect(result.current.reviewingJobId).toBe('job-1');
    expect(result.current.reopenReviewJobIdRef.current).toBeNull();
  });

  it('a completed scan whose every book was saved this way is dismissed', async () => {
    const s = setup([reviewJob([DUNE])]);
    const { result } = await s.hook;
    await act(async () => {
      result.current.addYourself([DUNE]);
    });
    await act(async () => {
      result.current.unverifiedSaved('Dune', 'draft-1');
    });
    expect(s.dismissJob).toHaveBeenCalledWith('job-1');
    expect(result.current.reviewingJobId).toBeNull();
  });

  it('candidateKey tells books apart by title and author', () => {
    expect(candidateKey(DUNE)).not.toBe(candidateKey(EMMA));
    expect(candidateKey(DUNE)).toBe(candidateKey({ ...DUNE, confidence: 0.1 }));
  });
});
