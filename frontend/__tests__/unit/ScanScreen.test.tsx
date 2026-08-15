import React from 'react';
import { Platform } from 'react-native';
import { act, fireEvent, render } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';

// ── Mocks ───────────────────────────────────────────────────────────────────

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

const mockTakePictureAsync = jest.fn();
const mockRequestPermission = jest.fn();

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
    useCameraPermissions: jest.fn(),
  };
});

const mockFileCopy = jest.fn();
const mockFileCreate = jest.fn();
const mockFileDelete = jest.fn();
const mockDirCreate = jest.fn();

jest.mock('expo-file-system', () => ({
  Paths: { document: 'file:///docs' },
  Directory: jest.fn().mockImplementation((...args: unknown[]) => {
    const parts = args.map((a) =>
      typeof a === 'object' && a !== null && 'uri' in a ? (a as { uri: string }).uri : String(a)
    );
    return {
      uri: parts.join('/'),
      exists: true,
      create: mockDirCreate,
    };
  }),
  File: jest.fn().mockImplementation((...args: unknown[]) => {
    // Mimic expo-file-system File: resolve uri from parent (string or File/Directory) + name
    const parts = args.map((a) =>
      typeof a === 'object' && a !== null && 'uri' in a ? (a as { uri: string }).uri : String(a)
    );
    return {
      uri: parts.join('/'),
      // Default to `false` so the stale-file cleanup path is a no-op by default.
      // Tests that simulate the pre-#174 corruption override this.
      exists: false,
      create: mockFileCreate,
      copy: mockFileCopy,
      delete: mockFileDelete,
    };
  }),
}));

const mockCaptureException = jest.fn();
const mockAddBreadcrumb = jest.fn();
const mockCaptureMessage = jest.fn();

jest.mock('../../lib/sentry', () => ({
  Sentry: {
    captureException: (...args: unknown[]) => mockCaptureException(...args),
    addBreadcrumb: (...args: unknown[]) => mockAddBreadcrumb(...args),
    captureMessage: (...args: unknown[]) => mockCaptureMessage(...args),
  },
}));

const mockShowBanner = jest.fn();
jest.mock('../../hooks/useBanner', () => ({
  useBanner: () => ({
    showBanner: mockShowBanner,
    hideBanner: jest.fn(),
    banner: null,
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
        iconActive: '#000',
      },
      typography: { fontSizeBase: 16 },
      spacing: { md: 16 },
    },
  }),
}));

const { useCameraPermissions } = require('expo-camera');

function setPlatform(os: string) {
  let original: string;
  beforeEach(() => {
    original = Platform.OS;
    Object.defineProperty(Platform, 'OS', { value: os, configurable: true });
  });
  afterEach(() => {
    Object.defineProperty(Platform, 'OS', { value: original, configurable: true });
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  useCameraPermissions.mockReturnValue([{ granted: true }, mockRequestPermission]);
});

afterEach(() => {
  jest.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Search mode
// ---------------------------------------------------------------------------
describe('ScanScreen — search mode', () => {
  async function renderInSearchMode() {
    const utils = await render(<ScanScreen />);
    await fireEvent.press(utils.getByLabelText('Text search mode'));
    return utils;
  }

  it('renders search input and button after switching to search mode', async () => {
    const { getByLabelText } = await renderInSearchMode();
    expect(getByLabelText('Book title or author search')).toBeTruthy();
    expect(getByLabelText('Search for book')).toBeTruthy();
  });

  it('calls startScan with text type when search is pressed', async () => {
    const { getByLabelText } = await renderInSearchMode();
    await fireEvent.changeText(getByLabelText('Book title or author search'), 'Dune');
    await fireEvent.press(getByLabelText('Search for book'));
    expect(mockStartScan).toHaveBeenCalledWith('text', undefined, 'Dune');
  });

  it('clears query after search', async () => {
    const { getByLabelText } = await renderInSearchMode();
    await fireEvent.changeText(getByLabelText('Book title or author search'), 'Dune');
    await fireEvent.press(getByLabelText('Search for book'));
    expect(getByLabelText('Book title or author search').props.value).toBe('');
  });

  it('does nothing if search query is empty', async () => {
    const { getByLabelText } = await renderInSearchMode();
    await fireEvent.press(getByLabelText('Search for book'));
    expect(mockStartScan).not.toHaveBeenCalled();
  });

  it('does not show a loading spinner (search runs in background)', async () => {
    const { getByLabelText, queryByTestId } = await renderInSearchMode();
    await fireEvent.changeText(getByLabelText('Book title or author search'), 'Dune');
    await fireEvent.press(getByLabelText('Search for book'));
    expect(queryByTestId('loading-spinner')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Native camera mode — permissions
// ---------------------------------------------------------------------------
describe('ScanScreen — native camera permissions', () => {
  it('renders capture button and flip button when permission is granted', async () => {
    const { getByLabelText } = await render(<ScanScreen />);
    expect(getByLabelText('Capture book cover')).toBeTruthy();
    expect(getByLabelText('Flip camera')).toBeTruthy();
  });

  it('shows permission prompt when camera permission is not granted', async () => {
    useCameraPermissions.mockReturnValue([{ granted: false }, mockRequestPermission]);
    const { getByLabelText } = await render(<ScanScreen />);
    expect(getByLabelText('Grant camera permission')).toBeTruthy();
  });

  it('requests permission when allow button is pressed', async () => {
    useCameraPermissions.mockReturnValue([{ granted: false }, mockRequestPermission]);
    const { getByLabelText } = await render(<ScanScreen />);
    await fireEvent.press(getByLabelText('Grant camera permission'));
    expect(mockRequestPermission).toHaveBeenCalled();
  });

  it('renders mode toggle when permission is loading (null)', async () => {
    useCameraPermissions.mockReturnValue([null, mockRequestPermission]);
    const { getByLabelText } = await render(<ScanScreen />);
    expect(getByLabelText('Camera scan mode')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Native camera mode — facing toggle
// ---------------------------------------------------------------------------
describe('ScanScreen — native camera facing toggle', () => {
  it('toggles facing state when flip button is pressed', async () => {
    const { getByLabelText } = await render(<ScanScreen />);
    const flipBtn = getByLabelText('Flip camera');
    await fireEvent.press(flipBtn);
    await fireEvent.press(flipBtn);
    expect(getByLabelText('Flip camera')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Native camera mode — capture flow (background)
// ---------------------------------------------------------------------------
describe('ScanScreen — native capture flow', () => {
  it('calls startScan with image type after photo capture', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockStartScan).toHaveBeenCalledWith(
      'image',
      expect.stringMatching(/^file:\/\/\/docs\/scan-queue\/\d+-\d+\.jpg$/)
    );
  });

  it('copies photo to document directory before starting scan', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://tmp/photo.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockFileCopy).toHaveBeenCalled();
  });

  it('does not show loading spinner (search runs in background)', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText, queryByTestId } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(queryByTestId('loading-spinner')).toBeNull();
  });

  it('exits early without starting scan when takePictureAsync returns no uri', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: null });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockStartScan).not.toHaveBeenCalled();
  });

  it('camera controls remain visible after capture (no loading overlay)', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(getByLabelText('Capture book cover')).toBeTruthy();
    expect(getByLabelText('Flip camera')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Web camera mode
// ---------------------------------------------------------------------------
describe('ScanScreen — web camera mode', () => {
  setPlatform('web');

  it('renders Scan Book Cover button instead of CameraView controls', async () => {
    const { getByLabelText, queryByLabelText } = await render(<ScanScreen />);
    expect(getByLabelText('Capture book cover')).toBeTruthy();
    expect(queryByLabelText('Flip camera')).toBeNull();
  });

  async function getWebInput(utils: ReturnType<typeof render>) {
    // The input is style={{ display: 'none' }}, and v14 excludes hidden
    // elements from queries by default.
    return utils.findByTestId('web-file-input', { includeHiddenElements: true });
  }

  it('renders hidden file input with correct attributes', async () => {
    const utils = await render(<ScanScreen />);
    const input = await getWebInput(utils);
    expect(input.props.type).toBe('file');
    expect(input.props.accept).toBe('image/*');
    expect(input.props.capture).toBe('environment');
  });

  it('calls startScan with image type after file selection', async () => {
    const mockFile = new File(['img'], 'scan.jpg', { type: 'image/jpeg' });
    const utils = await render(<ScanScreen />);
    const input = await getWebInput(utils);
    await act(async () => await fireEvent(input, 'change', { target: { files: [mockFile] } }));
    expect(mockStartScan).toHaveBeenCalledWith('image', expect.any(String));
  });

  it('does nothing when change fires with no file selected', async () => {
    const utils = await render(<ScanScreen />);
    const input = await getWebInput(utils);
    await act(async () => await fireEvent(input, 'change', { target: { files: [] } }));
    expect(mockStartScan).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Mode switch
// ---------------------------------------------------------------------------
describe('ScanScreen — mode switch hides camera controls (native)', () => {
  it('hides capture and flip buttons after switching to search mode', async () => {
    const { getByLabelText, queryByLabelText } = await render(<ScanScreen />);
    expect(getByLabelText('Capture book cover')).toBeTruthy();
    await fireEvent.press(getByLabelText('Text search mode'));
    expect(queryByLabelText('Capture book cover')).toBeNull();
    expect(queryByLabelText('Flip camera')).toBeNull();
  });

  it('shows capture and flip buttons after switching back to camera mode', async () => {
    const { getByLabelText } = await render(<ScanScreen />);
    await fireEvent.press(getByLabelText('Text search mode'));
    await fireEvent.press(getByLabelText('Camera scan mode'));
    expect(getByLabelText('Capture book cover')).toBeTruthy();
    expect(getByLabelText('Flip camera')).toBeTruthy();
  });
});

describe('ScanScreen — mode switch hides camera controls (web)', () => {
  setPlatform('web');

  it('hides capture button after switching to search mode', async () => {
    const utils = await render(<ScanScreen />);
    expect(utils.getByLabelText('Capture book cover')).toBeTruthy();
    await fireEvent.press(utils.getByLabelText('Text search mode'));
    expect(utils.queryByLabelText('Capture book cover')).toBeNull();
  });

  it('shows capture button after switching back to camera mode', async () => {
    const utils = await render(<ScanScreen />);
    await fireEvent.press(utils.getByLabelText('Text search mode'));
    await fireEvent.press(utils.getByLabelText('Camera scan mode'));
    expect(utils.getByLabelText('Capture book cover')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Sentry logging
// ---------------------------------------------------------------------------
describe('ScanScreen — Sentry logging', () => {
  it('adds breadcrumb when capture starts', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockAddBreadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ category: 'scan', message: 'Camera capture started' })
    );
  });

  it('adds breadcrumb when photo is saved successfully', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockAddBreadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ category: 'scan', message: 'Photo saved, starting scan' })
    );
  });

  it('adds warning breadcrumb when takePictureAsync returns no URI', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: null });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockAddBreadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({
        category: 'scan',
        message: 'takePictureAsync returned no URI',
        level: 'warning',
      })
    );
  });

  it('captures exception to Sentry when file copy fails', async () => {
    const copyError = new Error('File copy failed');
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    mockFileCopy.mockImplementationOnce(() => {
      throw copyError;
    });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockCaptureException).toHaveBeenCalledWith(
      copyError,
      expect.objectContaining({
        tags: expect.objectContaining({ feature: 'camera-capture' }),
      })
    );
  });

  it('does not call startScan when capture throws', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    mockFileCopy.mockImplementationOnce(() => {
      throw new Error('disk full');
    });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockStartScan).not.toHaveBeenCalled();
  });

  it('re-enables capture button after an error', async () => {
    mockTakePictureAsync.mockRejectedValueOnce(new Error('camera error'));
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(getByLabelText('Capture book cover').props.accessibilityState).not.toEqual(
      expect.objectContaining({ disabled: true })
    );
  });
});

// ---------------------------------------------------------------------------
// File system — scan-queue handling (every capture attempts create idempotently)
// ---------------------------------------------------------------------------
describe('ScanScreen — scan-queue directory handling', () => {
  it('always calls Directory.create with idempotent + intermediates', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockDirCreate).toHaveBeenCalledWith({ intermediates: true, idempotent: true });
  });

  it('deletes a stale File at the scan-queue path before creating the Directory', async () => {
    const FileSystem = require('expo-file-system');
    // Override the first File() call (the stale-file check) to claim it exists.
    // Subsequent File() calls (destFile, sourceFile) return exists: false as usual.
    FileSystem.File.mockImplementationOnce((...args: unknown[]) => {
      const parts = args.map((a: unknown) =>
        typeof a === 'object' && a !== null && 'uri' in a ? (a as { uri: string }).uri : String(a)
      );
      return {
        uri: parts.join('/'),
        exists: true,
        create: mockFileCreate,
        copy: mockFileCopy,
        delete: mockFileDelete,
      };
    });

    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    // Healing: stale file deleted, directory created, scan proceeds.
    expect(mockFileDelete).toHaveBeenCalled();
    expect(mockDirCreate).toHaveBeenCalled();
    expect(mockStartScan).toHaveBeenCalled();
    expect(mockShowBanner).not.toHaveBeenCalled();
  });

  it('skips delete when no stale File exists at scan-queue path', async () => {
    // Default File mock returns exists: false → no delete should fire.
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockFileDelete).not.toHaveBeenCalled();
    expect(mockDirCreate).toHaveBeenCalled();
  });

  it('still proceeds if stale-file delete throws (best-effort cleanup)', async () => {
    const FileSystem = require('expo-file-system');
    FileSystem.File.mockImplementationOnce((...args: unknown[]) => {
      const parts = args.map((a: unknown) =>
        typeof a === 'object' && a !== null && 'uri' in a ? (a as { uri: string }).uri : String(a)
      );
      return {
        uri: parts.join('/'),
        exists: true,
        create: mockFileCreate,
        copy: mockFileCopy,
        delete: jest.fn(() => {
          throw new Error('EACCES');
        }),
      };
    });

    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    // Cleanup failed, but create() is idempotent so it still runs. If it succeeds,
    // the scan proceeds.
    expect(mockDirCreate).toHaveBeenCalled();
    expect(mockStartScan).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Failure-path matrix: every throw site must (a) block startScan,
// (b) show the user an error banner, (c) tag Sentry with the stage.
// This matrix is the guardrail against regressions of #174 and the silent-
// failure class of bug.
// ---------------------------------------------------------------------------
describe('ScanScreen — capture failure paths surface banners', () => {
  it('Directory.create() throws → banner shown, stage=dir_create, no scan', async () => {
    mockDirCreate.mockImplementationOnce(() => {
      throw new Error('EEXIST: file with same path exists');
    });
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));

    expect(mockStartScan).not.toHaveBeenCalled();
    expect(mockShowBanner).toHaveBeenCalledWith(expect.objectContaining({ type: 'error' }));
    expect(mockCaptureException).toHaveBeenCalledWith(
      expect.any(Error),
      expect.objectContaining({
        tags: expect.objectContaining({ stage: 'dir_create' }),
      })
    );
  });

  it('File.copy() throws (disk full) → banner shown, stage=file_copy, no scan', async () => {
    mockFileCopy.mockImplementationOnce(() => {
      throw new Error('ENOSPC: no space left on device');
    });
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));

    expect(mockStartScan).not.toHaveBeenCalled();
    expect(mockShowBanner).toHaveBeenCalledWith(expect.objectContaining({ type: 'error' }));
    expect(mockCaptureException).toHaveBeenCalledWith(
      expect.any(Error),
      expect.objectContaining({
        tags: expect.objectContaining({ stage: 'file_copy' }),
      })
    );
  });

  it('takePictureAsync rejects → banner shown, stage=take_picture, no scan', async () => {
    mockTakePictureAsync.mockRejectedValueOnce(new Error('camera hardware error'));
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));

    expect(mockStartScan).not.toHaveBeenCalled();
    expect(mockShowBanner).toHaveBeenCalledWith(expect.objectContaining({ type: 'error' }));
    expect(mockCaptureException).toHaveBeenCalledWith(
      expect.any(Error),
      expect.objectContaining({
        tags: expect.objectContaining({ stage: 'take_picture' }),
      })
    );
  });

  it('error banner includes a retry action that re-runs capture', async () => {
    mockDirCreate.mockImplementationOnce(() => {
      throw new Error('EEXIST');
    });
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));

    const bannerCall = mockShowBanner.mock.calls[0][0];
    expect(bannerCall.actions).toBeDefined();
    expect(bannerCall.actions.length).toBeGreaterThan(0);
    // Invoking the retry action should trigger capture again.
    await act(async () => await bannerCall.actions[0].onPress());
    expect(mockTakePictureAsync).toHaveBeenCalledTimes(2);
  });

  it('capture button is re-enabled after a failure (finally runs)', async () => {
    mockDirCreate.mockImplementationOnce(() => {
      throw new Error('EEXIST');
    });
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    // Second press must actually invoke takePictureAsync (not be blocked by
    // capturing=true lingering after the throw).
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockTakePictureAsync).toHaveBeenCalledTimes(2);
  });

  it('happy path does NOT show an error banner', async () => {
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });
    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    expect(mockShowBanner).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Double-tap filename collision guard
// ---------------------------------------------------------------------------
describe('ScanScreen — double-tap filename collisions', () => {
  it('uses a sequence counter so two captures in the same ms get unique filenames', async () => {
    // Freeze Date.now() so only the sequence counter differentiates the URIs.
    const nowSpy = jest.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000);
    mockTakePictureAsync.mockResolvedValue({ uri: 'file://test.jpg' });

    const { getByLabelText } = await render(<ScanScreen />);
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));
    await act(async () => await fireEvent.press(getByLabelText('Capture book cover')));

    const uris = mockStartScan.mock.calls.map((c) => c[1]);
    expect(uris).toHaveLength(2);
    expect(uris[0]).not.toEqual(uris[1]);
    nowSpy.mockRestore();
  });
});
