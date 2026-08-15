// Integration test for the camera-capture → scan handoff. Uses the REAL
// BannerProvider + InAppBanner so we assert that users actually see an error
// message on the screen when the capture pipeline fails — which is the class
// of silent-failure bug that issue #174 was a symptom of.
//
// expo-camera and expo-file-system are mocked (no native module in jest), but
// the i18n layer, BannerContext, InAppBanner, and useScanJobs are REAL.
import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import { BannerProvider } from '../../contexts/BannerContext';
import { InAppBanner } from '../../components/InAppBanner';

// ── expo-camera mock ────────────────────────────────────────────────────────
const mockTakePictureAsync = jest.fn();
jest.mock('expo-camera', () => {
  const React = require('react');
  const MockCameraView = React.forwardRef(
    ({ children }: { children: React.ReactNode }, ref: React.Ref<unknown>) => {
      React.useImperativeHandle(ref, () => ({ takePictureAsync: mockTakePictureAsync }));
      return <>{children}</>;
    }
  );
  MockCameraView.displayName = 'CameraView';
  return {
    CameraView: MockCameraView,
    useCameraPermissions: () => [{ granted: true }, jest.fn()],
  };
});

// ── expo-file-system mock (configurable per test) ───────────────────────────
const mockDirCreate = jest.fn();
const mockFileCopy = jest.fn();
jest.mock('expo-file-system', () => ({
  Paths: { document: 'file:///docs' },
  Directory: jest.fn().mockImplementation((...args: unknown[]) => {
    const parts = args.map((a) =>
      typeof a === 'object' && a !== null && 'uri' in a ? (a as { uri: string }).uri : String(a)
    );
    return { uri: parts.join('/'), exists: true, create: mockDirCreate };
  }),
  File: jest.fn().mockImplementation((...args: unknown[]) => {
    const parts = args.map((a) =>
      typeof a === 'object' && a !== null && 'uri' in a ? (a as { uri: string }).uri : String(a)
    );
    return {
      uri: parts.join('/'),
      exists: false,
      copy: mockFileCopy,
      delete: jest.fn(),
      create: jest.fn(),
    };
  }),
}));

// ── useScanJobs mock (we don't need the real scan orchestrator here — we're
//    verifying the capture-side banner surfaces, not the API layer). ────────
const mockStartScan = jest.fn();
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    jobs: [],
    reviewingJob: null,
    startScan: mockStartScan,
    retryScan: jest.fn(),
    queueForLater: jest.fn(),
    reviewJob: jest.fn(),
    dismissReview: jest.fn(),
    dismissJob: jest.fn(),
    handleSelectBook: jest.fn(),
  }),
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
        success: '#22c55e',
        error: '#ef4444',
      },
      typography: { fontSizeBase: 16 },
      spacing: { md: 16 },
    },
  }),
}));

jest.mock('../../lib/sentry', () => ({
  Sentry: { captureException: jest.fn(), addBreadcrumb: jest.fn(), captureMessage: jest.fn() },
}));

beforeEach(() => {
  jest.clearAllMocks();
});

async function renderWithBanner() {
  return await render(
    <BannerProvider>
      <ScanScreen />
      <InAppBanner />
    </BannerProvider>
  );
}

describe('Scan capture flow — integration with real BannerProvider', () => {
  it('renders an error banner on screen when Directory.create throws', async () => {
    // Simulate the exact failure mode from this bug: create() throws because a
    // stale file already exists at the path.
    mockDirCreate.mockImplementationOnce(() => {
      throw new Error('EEXIST: file with same path exists');
    });
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://tmp/photo.jpg' });

    const utils = await renderWithBanner();
    await act(async () => await fireEvent.press(utils.getByLabelText('Capture book cover')));

    // The real BannerProvider + InAppBanner must now show the error text.
    await waitFor(() => {
      expect(utils.getByText('Something went wrong. Please try again.')).toBeTruthy();
    });
    // Scan must NOT have started.
    expect(mockStartScan).not.toHaveBeenCalled();
  });

  it('renders an error banner when File.copy throws (disk full)', async () => {
    mockFileCopy.mockImplementationOnce(() => {
      throw new Error('ENOSPC: no space left on device');
    });
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://tmp/photo.jpg' });

    const utils = await renderWithBanner();
    await act(async () => await fireEvent.press(utils.getByLabelText('Capture book cover')));

    await waitFor(() => {
      expect(utils.getByText('Something went wrong. Please try again.')).toBeTruthy();
    });
    expect(mockStartScan).not.toHaveBeenCalled();
  });

  it('banner has a Retry action that triggers another capture attempt', async () => {
    // First attempt throws, second succeeds.
    mockDirCreate.mockImplementationOnce(() => {
      throw new Error('EEXIST');
    });
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://tmp/photo.jpg' });

    const utils = await renderWithBanner();
    await act(async () => await fireEvent.press(utils.getByLabelText('Capture book cover')));

    const retryButton = await waitFor(() => utils.getByLabelText('Retry'));
    await act(async () => await fireEvent.press(retryButton));

    // After retry, the happy path runs and startScan fires.
    await waitFor(() => expect(mockStartScan).toHaveBeenCalled());
  });

  it('happy path: no banner shown, scan started with a valid URI', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://tmp/photo.jpg' });

    const utils = await renderWithBanner();
    await act(async () => await fireEvent.press(utils.getByLabelText('Capture book cover')));

    await waitFor(() => expect(mockStartScan).toHaveBeenCalled());
    expect(mockStartScan).toHaveBeenCalledWith(
      'image',
      expect.stringMatching(/^file:\/\/\/docs\/scan-queue\/\d+-\d+\.jpg$/)
    );
    // No error text on screen.
    expect(utils.queryByText('Something went wrong. Please try again.')).toBeNull();
  });
});
