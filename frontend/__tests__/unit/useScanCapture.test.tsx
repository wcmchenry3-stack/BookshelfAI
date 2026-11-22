// Unit tests for hooks/useScanCapture: the native capture sequence and the
// web file-input path. expo-file-system and Sentry are mocked; the banner is
// the real useBanner context default replaced by a spy.
import { act, renderHook } from '@testing-library/react-native';

import { useScanCapture } from '../../hooks/useScanCapture';

const mockShowBanner = jest.fn();
jest.mock('../../hooks/useBanner', () => ({
  useBanner: () => ({ showBanner: mockShowBanner, hideBanner: jest.fn(), banner: null }),
}));

const mockCaptureException = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: {
    addBreadcrumb: jest.fn(),
    captureException: (...a: unknown[]) => mockCaptureException(...a),
  },
}));

const mockDirCreate = jest.fn();
const mockFileCopy = jest.fn();
const mockFileUris: string[] = [];
jest.mock('expo-file-system', () => ({
  Paths: { document: 'file:///docs' },
  Directory: jest.fn().mockImplementation((...args: unknown[]) => ({
    uri: args.map((a) => (typeof a === 'object' && a ? (a as { uri: string }).uri : a)).join('/'),
    create: mockDirCreate,
  })),
  File: jest.fn().mockImplementation((...args: unknown[]) => {
    const uri = args
      .map((a) => (typeof a === 'object' && a ? (a as { uri: string }).uri : a))
      .join('/');
    mockFileUris.push(uri);
    return { uri, exists: false, copy: mockFileCopy, delete: jest.fn() };
  }),
}));

const startScan = jest.fn();

async function setup(opts: { isQueueFull?: boolean } = {}) {
  const view = await renderHook(() =>
    useScanCapture({
      startScan,
      isQueueFull: opts.isQueueFull,
      queueFullMessage: () => 'queue full',
      facing: 'back',
    })
  );
  const takePictureAsync = jest.fn().mockResolvedValue({ uri: 'file:///tmp/photo.jpg' });
  (view.result.current.cameraRef as { current: unknown }).current = { takePictureAsync };
  return { ...view, takePictureAsync };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockFileUris.length = 0;
});

describe('useScanCapture — native capture', () => {
  it('copies the photo into scan-queue/ and starts a scan', async () => {
    const { result, takePictureAsync } = await setup();
    await act(async () => {
      await result.current.handleCapture();
    });
    expect(takePictureAsync).toHaveBeenCalledWith({ quality: 0.7, base64: false });
    expect(mockDirCreate).toHaveBeenCalledWith({ intermediates: true, idempotent: true });
    expect(mockFileCopy).toHaveBeenCalledTimes(1);
    expect(startScan).toHaveBeenCalledWith(
      'image',
      expect.stringMatching(/^file:\/\/\/docs\/scan-queue\/\d+-0\.jpg$/)
    );
    expect(result.current.capturing).toBe(false);
  });

  it('uses a fresh sequence number for each capture so filenames never collide', async () => {
    const { result } = await setup();
    await act(async () => {
      await result.current.handleCapture();
    });
    await act(async () => {
      await result.current.handleCapture();
    });
    const uris = startScan.mock.calls.map((c) => c[1] as string);
    expect(uris[0]).toMatch(/-0\.jpg$/);
    expect(uris[1]).toMatch(/-1\.jpg$/);
  });

  it('ignores a second press while a capture is still in flight', async () => {
    const { result, takePictureAsync } = await setup();
    let resolvePhoto: (v: { uri: string }) => void = () => {};
    takePictureAsync.mockReturnValueOnce(new Promise((res) => (resolvePhoto = res)));
    let first: Promise<void> = Promise.resolve();
    await act(async () => {
      first = result.current.handleCapture();
    });
    expect(result.current.capturing).toBe(true);
    await act(async () => {
      await result.current.handleCapture();
    });
    expect(takePictureAsync).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolvePhoto({ uri: 'file:///tmp/a.jpg' });
      await first;
    });
    expect(startScan).toHaveBeenCalledTimes(1);
    expect(result.current.capturing).toBe(false);
  });

  it('refuses with the queue-full banner before taking a photo', async () => {
    const { result, takePictureAsync } = await setup({ isQueueFull: true });
    await act(async () => {
      await result.current.handleCapture();
    });
    expect(takePictureAsync).not.toHaveBeenCalled();
    expect(mockFileCopy).not.toHaveBeenCalled();
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'queue full', type: 'error' })
    );
  });

  it('does nothing when the camera ref is not attached', async () => {
    const { result, takePictureAsync } = await setup();
    (result.current.cameraRef as { current: unknown }).current = null;
    await act(async () => {
      await result.current.handleCapture();
    });
    expect(takePictureAsync).not.toHaveBeenCalled();
    expect(startScan).not.toHaveBeenCalled();
  });

  it('reports the failing stage to Sentry and shows a retry banner', async () => {
    const { result } = await setup();
    mockFileCopy.mockImplementationOnce(() => {
      throw new Error('disk full');
    });
    await act(async () => {
      await result.current.handleCapture();
    });
    expect(startScan).not.toHaveBeenCalled();
    expect(mockCaptureException).toHaveBeenCalledWith(
      expect.any(Error),
      expect.objectContaining({ tags: { feature: 'camera-capture', stage: 'file_copy' } })
    );
    const banner = mockShowBanner.mock.calls[0][0];
    expect(banner.type).toBe('error');
    expect(banner.actions).toHaveLength(1);
    expect(result.current.capturing).toBe(false);
  });
});

describe('useScanCapture — web file input', () => {
  const fileEvent = (file?: { name: string }) =>
    ({ target: { files: file ? [file] : [] } }) as unknown as Parameters<
      ReturnType<typeof useScanCapture>['handleWebFileChange']
    >[0];

  it('starts a scan from the picked file and clears the input', async () => {
    const { result } = await setup();
    const input = { value: 'C:\\fake\\a.jpg' } as HTMLInputElement;
    (result.current.webInputRef as { current: unknown }).current = input;
    const create = jest.fn().mockReturnValue('blob:abc');
    (globalThis.URL as unknown as { createObjectURL: unknown }).createObjectURL = create;
    await act(async () => {
      await result.current.handleWebFileChange(fileEvent({ name: 'a.jpg' }));
    });
    expect(startScan).toHaveBeenCalledWith('image', 'blob:abc');
    expect(input.value).toBe('');
  });

  it('falls back to the file name when createObjectURL throws', async () => {
    const { result } = await setup();
    (globalThis.URL as unknown as { createObjectURL: unknown }).createObjectURL = () => {
      throw new Error('nope');
    };
    await act(async () => {
      await result.current.handleWebFileChange(fileEvent({ name: 'b.jpg' }));
    });
    expect(startScan).toHaveBeenCalledWith('image', 'b.jpg');
  });

  it('ignores an event with no file', async () => {
    const { result } = await setup();
    await act(async () => {
      await result.current.handleWebFileChange(fileEvent());
    });
    expect(startScan).not.toHaveBeenCalled();
  });

  it('shows the queue-full banner instead of starting a scan', async () => {
    const { result } = await setup({ isQueueFull: true });
    await act(async () => {
      await result.current.handleWebFileChange(fileEvent({ name: 'c.jpg' }));
    });
    expect(startScan).not.toHaveBeenCalled();
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'queue full', type: 'error' })
    );
  });
});
