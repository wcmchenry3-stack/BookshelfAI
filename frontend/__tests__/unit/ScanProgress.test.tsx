import React from 'react';
import { AccessibilityInfo } from 'react-native';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import ScanQueueScreen from '../../app/scan-queue';
import { BannerProvider } from '../../contexts/BannerContext';
import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { useScanJobs } from '../../hooks/useScanJobs';

// #742: in-progress state while a scan request is pending, on the scan
// screen and in the scan queue.

jest.mock('@expo/vector-icons', () => ({ MaterialIcons: () => null }));

const mockPost = jest.fn();
const mockGet = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: (...a: unknown[]) => mockGet(...a) },
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
const mockTakePicture = jest.fn();
jest.mock('expo-camera', () => {
  const R = require('react');
  const CameraView = R.forwardRef(({ children }: { children: React.ReactNode }, ref: unknown) => {
    R.useImperativeHandle(ref, () => ({ takePictureAsync: mockTakePicture }));
    return <>{children}</>;
  });
  CameraView.displayName = 'CameraView';
  return { CameraView, useCameraPermissions: () => [{ granted: true }, jest.fn()] };
});
jest.mock('expo-file-system', () => ({ Directory: jest.fn(), File: jest.fn(), Paths: {} }));

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

const grabbed: { start?: (type: 'image' | 'text', uri?: string, query?: string) => void } = {};
const setStart = (fn: NonNullable<typeof grabbed.start>) => {
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

async function startText() {
  await act(async () => {
    grabbed.start!('text', undefined, 'Dune');
    await new Promise((r) => setTimeout(r, 0));
  });
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
    const { queryByTestId, getByTestId, getAllByText, getByLabelText } = await renderBoth();
    expect(queryByTestId('scan-progress')).toBeNull();

    await startPhoto();
    const progress = getByTestId('scan-progress');
    expect(progress.props.accessibilityRole).toBe('progressbar');
    expect(getAllByText('Reading your books and looking up matches…')).toHaveLength(1);
    // Queue row: sending state with its own progress indicator.
    expect(getAllByText('Sending…')).toHaveLength(1);
    expect(getByLabelText(/: Sending…$/).props.accessibilityState).toEqual({ busy: true });
    expect(
      getByTestId('queue-progress-uuid-1', { includeHiddenElements: true }).props
        .importantForAccessibility
    ).toBe('no-hide-descendants');

    await act(async () => {
      resolve({
        data: { books: [], enhanced: false, enhanced_scans_remaining: null, no_match: 'free' },
      });
    });
    await waitFor(() => expect(queryByTestId('scan-progress')).toBeNull());
    expect(queryByTestId('queue-progress-uuid-1', { includeHiddenElements: true })).toBeNull();
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
    expect(queryByTestId('queue-progress-uuid-1', { includeHiddenElements: true })).toBeNull();
    // The error state replaces it: the job is failed or back in the queue.
    expect(
      queryByTestId('queue-waiting-uuid-1') ?? queryByTestId('queue-failed-uuid-1')
    ).toBeTruthy();
  });

  it('keeps the live-region container mounted and announces once per transition', async () => {
    const announce = jest.spyOn(AccessibilityInfo, 'announceForAccessibility').mockImplementation();
    let resolve!: (v: unknown) => void;
    mockPost.mockReturnValue(new Promise((r) => (resolve = r)));
    const { getByTestId, queryByTestId } = await renderBoth();
    expect(getByTestId('scan-progress-region').props.accessibilityLiveRegion).toBe('polite');
    expect(announce).not.toHaveBeenCalled();

    await startPhoto();
    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce).toHaveBeenCalledWith('Reading your books and looking up matches…');
    expect(getByTestId('scan-progress').props.accessible).toBe(true);

    await act(async () => {
      resolve({ data: { books: [], enhanced: false, enhanced_scans_remaining: null } });
    });
    await waitFor(() => expect(queryByTestId('scan-progress')).toBeNull());
    expect(getByTestId('scan-progress-region')).toBeTruthy();
    expect(announce).toHaveBeenCalledTimes(1);

    mockPost.mockReturnValue(new Promise(() => {}));
    await startPhoto();
    expect(announce).toHaveBeenCalledTimes(2);
    announce.mockRestore();
  });

  it('shows the row right after the shutter press, before the request starts', async () => {
    mockTakePicture.mockReturnValue(new Promise(() => {}));
    const { getByLabelText, getByTestId, queryByText } = await renderBoth();
    await act(async () => {
      fireEvent.press(getByLabelText('Capture books'));
    });
    expect(getByTestId('scan-progress')).toBeTruthy();
    expect(mockPost).not.toHaveBeenCalled();
    expect(queryByText(/Pending uploads/)).toBeNull();
  });

  it('does not flash the pending-uploads badge for a job being sent', async () => {
    mockPost.mockReturnValue(new Promise(() => {}));
    const { queryByText } = await renderBoth();
    await startPhoto();
    expect(queryByText(/Pending uploads/)).toBeNull();
  });

  it('shows a search message for a text search', async () => {
    mockPost.mockReturnValue(new Promise(() => {}));
    mockGet.mockReturnValue(new Promise(() => {}));
    const { getByTestId, queryByText } = await renderBoth();
    await startText();
    expect(getByTestId('scan-progress')).toBeTruthy();
    expect(queryByText('Searching…', { exact: true })).toBeTruthy();
    expect(queryByText(/Reading your books/)).toBeNull();
  });
});
