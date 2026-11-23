// useScanCapture asks for AI consent before a native photo is taken (#1023);
// the web picker opens at once and its upload asks. useAiConsent is the
// shared Jest mock (helpers/aiConsentMock.ts).
import { act, renderHook } from '@testing-library/react-native';

import { useScanCapture } from '../../hooks/useScanCapture';
import { aiConsentMock, resetAiConsentMock } from '../helpers/aiConsentMock';

jest.mock('../../hooks/useBanner', () => ({
  useBanner: () => ({ showBanner: jest.fn(), hideBanner: jest.fn(), banner: null }),
}));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn() },
}));
jest.mock('expo-file-system', () => ({
  Paths: { document: 'file:///docs' },
  Directory: jest
    .fn()
    .mockImplementation(() => ({ uri: 'file:///docs/scan-queue', create: jest.fn() })),
  File: jest.fn().mockImplementation(() => ({
    uri: 'file:///docs/scan-queue/1-0.jpg',
    exists: false,
    copy: jest.fn(),
    delete: jest.fn(),
  })),
}));
jest.mock('../../lib/scanJobStorage', () => ({ checkCaptureStorage: () => 'ok' }));

const startScan = jest.fn();

async function setup() {
  const view = await renderHook(() =>
    useScanCapture({
      startScan,
      isQueueFull: false,
      queueFullMessage: () => 'queue full',
      facing: 'back',
    })
  );
  const takePictureAsync = jest.fn().mockResolvedValue({ uri: 'file:///tmp/photo.jpg' });
  (view.result.current.cameraRef as { current: unknown }).current = { takePictureAsync };
  const click = jest.fn();
  (view.result.current.webInputRef as { current: unknown }).current = { click };
  return { ...view, takePictureAsync, click };
}

beforeEach(() => {
  jest.clearAllMocks();
  resetAiConsentMock();
});
afterAll(resetAiConsentMock);

describe('native shutter', () => {
  it('Not now takes no photo and starts no scan', async () => {
    aiConsentMock.answer = false;
    const ensure = jest.spyOn(aiConsentMock, 'ensureConsent');
    const { result, takePictureAsync } = await setup();
    await act(async () => {
      await result.current.requestCapture();
    });
    expect(ensure).toHaveBeenCalledWith({ allowUnknown: true });
    expect(takePictureAsync).not.toHaveBeenCalled();
    expect(startScan).not.toHaveBeenCalled();
  });

  it('with consent the photo is taken and the scan starts', async () => {
    const { result, takePictureAsync } = await setup();
    await act(async () => {
      await result.current.requestCapture();
    });
    expect(takePictureAsync).toHaveBeenCalledTimes(1);
    expect(startScan).toHaveBeenCalledWith('image', expect.any(String));
  });
});

describe('native shutter, double tap (round 1 #3)', () => {
  it('a second tap while consent is being asked takes no second photo', async () => {
    let answer!: (ok: boolean) => void;
    jest
      .spyOn(aiConsentMock, 'ensureConsent')
      .mockReturnValue(new Promise<boolean>((r) => (answer = r)));
    const { result, takePictureAsync } = await setup();
    let first!: Promise<void>;
    await act(async () => {
      first = result.current.requestCapture();
    });
    await act(async () => {
      await result.current.requestCapture();
    });
    await act(async () => {
      answer(true);
      await first;
    });
    expect(aiConsentMock.ensureConsent).toHaveBeenCalledTimes(1);
    expect(takePictureAsync).toHaveBeenCalledTimes(1);
    expect(startScan).toHaveBeenCalledTimes(1);
  });

  it('the shutter works again after Not now', async () => {
    aiConsentMock.answer = false;
    const { result, takePictureAsync } = await setup();
    await act(async () => {
      await result.current.requestCapture();
    });
    aiConsentMock.answer = true;
    await act(async () => {
      await result.current.requestCapture();
    });
    expect(takePictureAsync).toHaveBeenCalledTimes(1);
  });
});

describe('web picker', () => {
  it('known consent opens the picker in the same tap', async () => {
    const ensure = jest.spyOn(aiConsentMock, 'ensureConsent');
    const { result, click } = await setup();
    await act(async () => result.current.requestWebPick());
    expect(click).toHaveBeenCalledTimes(1);
    expect(ensure).not.toHaveBeenCalled();
  });

  it('without consent the picker still opens in the same tap, synchronously (round 1 #4)', async () => {
    // Browsers only open a picker from the tap itself; the upload asks later
    // (startScan's interactive send, ScanAiConsentFlow.test.tsx).
    aiConsentMock.status = 'none';
    aiConsentMock.answer = false;
    const ensure = jest.spyOn(aiConsentMock, 'ensureConsent');
    const { result, click } = await setup();
    result.current.requestWebPick();
    expect(click).toHaveBeenCalledTimes(1);
    expect(ensure).not.toHaveBeenCalled();
    expect(startScan).not.toHaveBeenCalled();
  });
});
