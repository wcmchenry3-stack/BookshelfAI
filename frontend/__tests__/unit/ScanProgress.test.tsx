import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import ScanQueueScreen from '../../app/scan-queue';
import { BannerProvider } from '../../contexts/BannerContext';
import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { useScanJobs } from '../../hooks/useScanJobs';

// #742: in-progress state while a scan request is pending, on the scan
// screen and in the scan queue.

jest.mock('@expo/vector-icons', () => ({ MaterialIcons: () => null }));

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => Promise.resolve([]),
  saveJobs: () => Promise.resolve(undefined),
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
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `uuid-${++mockUuid}` }));
jest.mock('expo-router', () => ({
  router: { push: jest.fn(), back: jest.fn(), replace: jest.fn(), canGoBack: () => true },
}));
jest.mock('../../hooks/useScanUsage', () => ({ useScanUsage: () => null }));
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: true }),
}));
jest.mock('expo-camera', () => ({
  CameraView: () => null,
  useCameraPermissions: () => [{ granted: true }, jest.fn()],
}));
jest.mock('expo-file-system', () => ({ Directory: jest.fn(), File: jest.fn(), Paths: {} }));

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

const grabbed: { start?: (type: 'image', uri: string) => void } = {};
const setStart = (fn: (type: 'image', uri: string) => void) => {
  grabbed.start = fn;
};
function Grab() {
  const { startScan } = useScanJobs();
  React.useEffect(() => setStart(startScan), [startScan]);
  return null;
}

async function renderBoth() {
  const utils = await render(
    <>
      <ScanScreen />
      <ScanQueueScreen />
      <Grab />
    </>,
    { wrapper }
  );
  await act(async () => {});
  return utils;
}

async function startPhoto() {
  await act(async () => {
    grabbed.start!('image', 'file:///docs/scan-queue/p.jpg');
    await new Promise((r) => setTimeout(r, 0));
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUuid = 0;
});

describe('scan progress (#742)', () => {
  it('shows the progress indicator while the request is pending, then clears it on success', async () => {
    let resolve!: (v: unknown) => void;
    mockPost.mockReturnValue(new Promise((r) => (resolve = r)));
    const { queryByTestId, getByTestId, getAllByText } = await renderBoth();
    expect(queryByTestId('scan-progress')).toBeNull();

    await startPhoto();
    const progress = getByTestId('scan-progress');
    expect(progress.props.accessibilityRole).toBe('progressbar');
    expect(progress.props.accessibilityLiveRegion).toBe('polite');
    expect(getAllByText('Reading your books and looking up matches…')).toHaveLength(1);
    // Queue row: sending state with its own progress indicator.
    expect(getAllByText('Sending…')).toHaveLength(1);
    expect(getByTestId('queue-progress-uuid-1').props.accessibilityRole).toBe('progressbar');

    await act(async () => {
      resolve({
        data: { books: [], enhanced: false, enhanced_scans_remaining: null, no_match: 'free' },
      });
    });
    await waitFor(() => expect(queryByTestId('scan-progress')).toBeNull());
    expect(queryByTestId('queue-progress-uuid-1')).toBeNull();
  });

  it('replaces the indicator with the error state when the request fails', async () => {
    let reject!: (e: unknown) => void;
    mockPost.mockReturnValue(new Promise((_, r) => (reject = r)));
    const { queryByTestId, getByTestId } = await renderBoth();
    await startPhoto();
    expect(getByTestId('scan-progress')).toBeTruthy();

    await act(async () => {
      reject({ response: { status: 400, data: { detail: 'bad image' } } });
    });
    await waitFor(() => expect(queryByTestId('scan-progress')).toBeNull());
    expect(queryByTestId('queue-progress-uuid-1')).toBeNull();
  });
});
