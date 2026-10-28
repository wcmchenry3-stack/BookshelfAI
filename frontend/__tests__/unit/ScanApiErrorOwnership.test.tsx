import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import type { EnrichedBook } from '../../components/BookCandidatePicker';
import { api } from '../../lib/api';
import { resetApiErrorReporting } from '../../lib/apiErrorReporting';

// #724 review: each API failure has exactly one Sentry owner, the central
// reporter in lib/api.ts. These tests use the REAL api instance (only the
// transport is faked) so the interceptor and ScanJobContext both run.

jest.mock('../../lib/storage', () => ({
  getItem: jest.fn().mockResolvedValue('SECRET-ACCESS-TOKEN'),
  setItem: jest.fn(),
  deleteItem: jest.fn(),
}));
jest.mock('axios', () => {
  const savedRS = globalThis.ReadableStream;
  // @ts-expect-error avoid axios fetch adapter probe conflict
  delete globalThis.ReadableStream;
  const actual = jest.requireActual('axios');
  globalThis.ReadableStream = savedRS;
  return actual;
});
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => Promise.resolve([]),
  saveJobs: jest.fn().mockResolvedValue(undefined),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: jest.fn(),
}));
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => Promise.resolve({ isConnected: true }),
    addEventListener: () => () => {},
  },
}));
const mockCaptureException = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: {
    addBreadcrumb: jest.fn(),
    captureException: (...a: unknown[]) => mockCaptureException(...a),
    captureMessage: jest.fn(),
  },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

const book = (title: string): EnrichedBook => ({
  open_library_work_id: `OL-${title}`,
  title,
  author: 'Frank Herbert',
  subjects: [],
  confidence: 0.9,
  already_in_library: false,
  in_library: false,
  ownership: 'not_owned',
  editions: [{ isbn_13: '9780441013593' }],
  catalog_token: 'p.mac',
});

type Reply = { status: number; data: unknown };
let replies: (method: string, url: string) => Reply;

beforeEach(() => {
  jest.clearAllMocks();
  mockUuid = 0;
  resetApiErrorReporting();
  api.defaults.adapter = async (config) => {
    const { status, data } = replies((config.method ?? 'get').toLowerCase(), config.url ?? '');
    const response = { status, data, headers: {}, config, statusText: '' };
    if (status === 0) {
      throw Object.assign(new Error('Network Error'), { isAxiosError: true, config });
    }
    if (status < 400) return response;
    throw Object.assign(new Error(`Request failed with status code ${status}`), {
      isAxiosError: true,
      config,
      response,
    });
  };
});

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

async function setup() {
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), { wrapper });
  await waitFor(() => expect(hook.result.current.scan.jobs).toEqual([]));
  return hook;
}

describe('one Sentry owner per API failure (#724)', () => {
  it('403 forbidden on POST /scan: captured once by the central reporter, banner says service unavailable', async () => {
    replies = () => ({ status: 403, data: { detail: 'forbidden' } });
    const hook = await setup();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await waitFor(() =>
      expect(hook.result.current.banner.banner?.message).toBe(
        "Can't reach BookshelfAI right now. Please try again later."
      )
    );
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
    const [captured, ctx] = mockCaptureException.mock.calls[0];
    expect(captured).toBeInstanceOf(Error);
    expect(ctx.tags).toMatchObject({
      'http.status': '403',
      'api.path': '/scan',
      'api.detail': 'forbidden',
    });
    expect(JSON.stringify(mockCaptureException.mock.calls)).not.toContain('SECRET-ACCESS-TOKEN');
  });

  it('a repeat 403 forbidden on POST /scan is not captured again', async () => {
    replies = () => ({ status: 403, data: { detail: 'forbidden' } });
    const hook = await setup();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///b.jpg');
    });
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
  });

  it('not lost: a no-response network error on POST /scan is still captured once by ScanJobContext', async () => {
    replies = () => ({ status: 0, data: null });
    const hook = await setup();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await waitFor(() => expect(mockCaptureException).toHaveBeenCalledTimes(1));
    const [captured, ctx] = mockCaptureException.mock.calls[0];
    expect(captured).toBeInstanceOf(Error);
    expect(ctx.tags).toMatchObject({ feature: 'scan', action: 'execute_scan' });
    expect(JSON.stringify(mockCaptureException.mock.calls)).not.toContain('SECRET-ACCESS-TOKEN');
  });

  it('not lost: a 422 with another detail on POST /scan is still captured once by ScanJobContext', async () => {
    replies = () => ({ status: 422, data: { detail: 'other' } });
    const hook = await setup();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await waitFor(() => expect(mockCaptureException).toHaveBeenCalledTimes(1));
    expect(mockCaptureException.mock.calls[0][1].tags).toMatchObject({
      feature: 'scan',
      action: 'execute_scan',
      'http.status': '422',
    });
  });

  it('a handled 503 scan_disabled is not captured by anyone (as before)', async () => {
    replies = () => ({ status: 503, data: { detail: 'scan_disabled' } });
    const hook = await setup();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await waitFor(() => expect(hook.result.current.scan.jobs[0]?.error).toBe('paused'));
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('a 5xx on POST /wishlist is captured once, not again by ScanJobContext', async () => {
    replies = (method, url) =>
      method === 'post' && url.includes('wishlist')
        ? { status: 500, data: { detail: 'boom' } }
        : { status: 200, data: { books: [book('Dune')], enhanced: false } };
    const hook = await setup();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await act(() => {
      hook.result.current.scan.reviewJob(hook.result.current.scan.jobs[0].id);
    });
    await act(async () => {
      await hook.result.current.scan.handleAddBooks([book('Dune')]);
    });
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
    expect(mockCaptureException.mock.calls[0][1].tags).toMatchObject({
      'http.status': '500',
      'api.path': '/wishlist',
    });
  });
});
