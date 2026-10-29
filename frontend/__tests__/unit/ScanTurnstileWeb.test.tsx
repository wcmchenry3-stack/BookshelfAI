/**
 * @jest-environment jsdom
 */
import React from 'react';
import { Platform } from 'react-native';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { BannerProvider } from '../../contexts/BannerContext';
import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { TurnstileProvider } from '../../contexts/TurnstileContext';
import { useScanJobs } from '../../hooks/useScanJobs';

// #740, web: with Cloudflare's script loader mocked, the invisible widget's
// token is sent with POST /scan as cf-turnstile-response.

const mockLoad = jest.fn();
jest.mock('../../lib/turnstile/webScript', () => ({
  loadTurnstileScript: () => mockLoad(),
}));
// The web widget, as Metro picks it on web.
jest.mock('../../components/TurnstileWidget', () =>
  jest.requireActual('../../components/TurnstileWidget.web')
);

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));
jest.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ isAuthenticated: true }) }));
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => Promise.resolve([]),
  saveJobs: () => Promise.resolve(),
  clearJobs: () => Promise.resolve(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: () => Promise.resolve(),
}));
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => Promise.resolve({ isConnected: true }),
    addEventListener: () => () => {},
  },
}));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <TurnstileProvider>
        <ScanJobProvider>{children}</ScanJobProvider>
      </TurnstileProvider>
    </BannerProvider>
  );
}

const originalOS = Platform.OS;

beforeEach(() => {
  process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = '1x00000000000000000000AA';
  Object.defineProperty(Platform, 'OS', { configurable: true, get: () => 'web' });
  let n = 0;
  let callback: (t: string) => void = () => {};
  mockLoad.mockResolvedValue({
    render: (_c: unknown, o: { callback: (t: string) => void }) => {
      callback = o.callback;
      return 'w1';
    },
    reset: jest.fn(),
    remove: jest.fn(),
    execute: () => {
      n += 1;
      const token = `web-${n}`;
      Promise.resolve().then(() => callback(token));
    },
  });
  mockPost.mockResolvedValue({
    data: { books: [{ title: 'Dune', author: 'Herbert' }], enhanced_scans_remaining: null },
  });
});

afterEach(() => {
  Object.defineProperty(Platform, 'OS', { configurable: true, get: () => originalOS });
  delete process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY;
});

it('sends the web widget’s token, a fresh one per scan', async () => {
  const { result } = await renderHook(() => useScanJobs(), { wrapper });
  await waitFor(() => expect(mockLoad).toHaveBeenCalledTimes(1));
  for (let i = 0; i < 2; i++) {
    // On web the photo goes into the form as a Blob.
    const photo = new Blob(['jpeg'], { type: 'image/jpeg' }) as unknown as string;
    await act(async () => {
      await result.current.startScan('image', photo);
    });
  }
  expect(mockPost).toHaveBeenCalledTimes(2);
  const tokens = mockPost.mock.calls.map(([, form]) =>
    (form as FormData).get('cf-turnstile-response')
  );
  expect(tokens).toEqual(['web-1', 'web-2']);
  // The script is loaded once.
  expect(mockLoad).toHaveBeenCalledTimes(1);
});
