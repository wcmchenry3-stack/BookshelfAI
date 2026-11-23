// The Scan screen and AI consent (#1023): the shutter and the web button go
// through the consent-asking capture paths, "Send now" for saved photos asks
// first, and search works whatever the answer. useAiConsent is the shared
// Jest mock (helpers/aiConsentMock.ts); the capture hook is mocked (its own
// consent tests are in useScanCaptureAiConsent.test.tsx).
import React from 'react';
import { Platform } from 'react-native';
import { fireEvent, render } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import { aiConsentMock, resetAiConsentMock } from '../helpers/aiConsentMock';
import { routerState } from '../helpers/routerMock';

const mockStartScan = jest.fn();
const mockResumeQueue = jest.fn();
let mockJobs: { id: string; type: string; status: string }[] = [];
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    jobs: mockJobs,
    pendingCount: mockJobs.length,
    isQueueFull: false,
    startScan: mockStartScan,
    resumeQueue: mockResumeQueue,
    scanNotice: null,
    dismissScanNotice: jest.fn(),
    usageRefreshKey: 0,
  }),
}));
const mockRequestCapture = jest.fn(async () => {});
const mockRequestWebPick = jest.fn();
jest.mock('../../hooks/useScanCapture', () => ({
  useScanCapture: () => ({
    cameraRef: { current: null },
    webInputRef: { current: null },
    capturing: false,
    requestCapture: mockRequestCapture,
    requestWebPick: mockRequestWebPick,
    handleWebFileChange: jest.fn(),
  }),
}));
jest.mock('../../hooks/useScanUsage', () => ({ useScanUsage: () => null }));
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: true }),
}));
jest.mock('expo-camera', () => ({
  CameraView: () => null,
  useCameraPermissions: () => [{ granted: true }, jest.fn()],
}));
jest.mock('../../lib/sentry', () => ({ Sentry: { addBreadcrumb: jest.fn() } }));

beforeEach(() => {
  jest.clearAllMocks();
  resetAiConsentMock();
  routerState.params = {};
  mockJobs = [];
});
afterAll(resetAiConsentMock);

describe('the Scan screen asks for AI consent (#1023)', () => {
  it('the native shutter takes the consent-asking capture path', async () => {
    const utils = await render(<ScanScreen />);
    await fireEvent.press(utils.getByLabelText('Capture books'));
    expect(mockRequestCapture).toHaveBeenCalledTimes(1);
  });

  it('the web button takes the consent-asking picker path', async () => {
    const original = Platform.OS;
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
    try {
      const utils = await render(<ScanScreen />);
      await fireEvent.press(utils.getByLabelText('Capture books'));
      expect(mockRequestWebPick).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(Platform, 'OS', { value: original, configurable: true });
    }
  });

  it('"Send now" for saved photos asks first; Not now sends nothing', async () => {
    mockJobs = [{ id: 'q-1', type: 'image', status: 'queued' }];
    aiConsentMock.answer = false;
    const ensure = jest.spyOn(aiConsentMock, 'ensureConsent');
    const utils = await render(<ScanScreen />);
    await fireEvent.press(utils.getByLabelText('Send now'));
    expect(ensure).toHaveBeenCalledTimes(1);
    expect(mockResumeQueue).not.toHaveBeenCalled();
  });

  it('"Send now" with consent sends the saved photos', async () => {
    mockJobs = [{ id: 'q-1', type: 'image', status: 'queued' }];
    const utils = await render(<ScanScreen />);
    await fireEvent.press(utils.getByLabelText('Send now'));
    expect(mockResumeQueue).toHaveBeenCalledTimes(1);
  });

  it('search still works after Not now (no photo leaves the device)', async () => {
    aiConsentMock.status = 'none';
    aiConsentMock.answer = false;
    const ensure = jest.spyOn(aiConsentMock, 'ensureConsent');
    const utils = await render(<ScanScreen />);
    await fireEvent.press(utils.getByLabelText('Text search mode'));
    await fireEvent.changeText(utils.getByLabelText('Book title or author search'), 'Dune');
    await fireEvent.press(utils.getByLabelText('Search for book'));
    expect(mockStartScan).toHaveBeenCalledWith('text', undefined, 'Dune');
    expect(ensure).not.toHaveBeenCalled();
  });
});
