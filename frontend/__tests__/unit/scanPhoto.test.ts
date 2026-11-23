import {
  removeTempFile,
  SCAN_PHOTO_JPEG_QUALITY,
  SCAN_PHOTO_MAX_LONG_EDGE_PX,
  shrinkScanPhoto,
} from '../../lib/scanPhoto';
import { imageManipulatorMock, resetImageManipulatorMock } from '../helpers/imageManipulatorMock';

const mockDelete = jest.fn();
let mockExists = true;
let mockThrow = false;
jest.mock('expo-file-system', () => ({
  File: jest.fn().mockImplementation(() => {
    if (mockThrow) throw new Error('bad uri');
    return {
      get exists() {
        return mockExists;
      },
      delete: mockDelete,
    };
  }),
}));

beforeEach(() => {
  jest.clearAllMocks();
  resetImageManipulatorMock();
  mockExists = true;
  mockThrow = false;
});

describe('shrinkScanPhoto (#668)', () => {
  it('uses the server long edge (1568 px) and JPEG quality 0.8', () => {
    expect(SCAN_PHOTO_MAX_LONG_EDGE_PX).toBe(1568);
    expect(SCAN_PHOTO_JPEG_QUALITY).toBe(0.8);
  });

  it('bounds the load and saves a JPEG at quality 0.8', async () => {
    const out = await shrinkScanPhoto('file:///tmp/p.jpg');
    expect(out.uri).toBe('file:///cache/shrunk-0.jpg');
    expect(imageManipulatorMock.calls.manipulate).toEqual([
      { uri: 'file:///tmp/p.jpg', options: { maxWidth: 1568, maxHeight: 1568 } },
    ]);
    expect(imageManipulatorMock.calls.save).toEqual([{ format: 'jpeg', compress: 0.8 }]);
    expect(imageManipulatorMock.calls.released).toBe(1);
  });

  it('does not upscale or resize a photo already within the edge', async () => {
    imageManipulatorMock.size = { width: 800, height: 600 };
    await shrinkScanPhoto('file:///tmp/small.jpg');
    expect(imageManipulatorMock.calls.resize).toEqual([]);
  });

  it('resizes the long edge when the platform ignored the load bounds (web), landscape', async () => {
    imageManipulatorMock.size = { width: 4000, height: 3000 };
    const out = await shrinkScanPhoto('blob:big');
    expect(imageManipulatorMock.calls.resize).toEqual([{ width: 1568 }]);
    expect(out).toEqual(expect.objectContaining({ width: 1568, height: 1176 }));
  });

  it('resizes by height for a portrait photo', async () => {
    imageManipulatorMock.size = { width: 3000, height: 4000 };
    const out = await shrinkScanPhoto('blob:big');
    expect(imageManipulatorMock.calls.resize).toEqual([{ height: 1568 }]);
    expect(out).toEqual(expect.objectContaining({ width: 1176, height: 1568 }));
  });

  it('releases the context and rethrows when saving fails', async () => {
    imageManipulatorMock.failure = new Error('disk full');
    await expect(shrinkScanPhoto('file:///tmp/p.jpg')).rejects.toThrow('disk full');
    expect(imageManipulatorMock.calls.released).toBe(1);
  });
});

describe('removeTempFile', () => {
  it('deletes an existing file', () => {
    removeTempFile('file:///tmp/p.jpg');
    expect(mockDelete).toHaveBeenCalledTimes(1);
  });

  it('does nothing for no uri or a missing file', () => {
    removeTempFile(undefined);
    mockExists = false;
    removeTempFile('file:///tmp/gone.jpg');
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('never throws', () => {
    mockThrow = true;
    expect(() => removeTempFile('nope')).not.toThrow();
    mockThrow = false;
    mockDelete.mockImplementationOnce(() => {
      throw new Error('locked');
    });
    expect(() => removeTempFile('file:///tmp/p.jpg')).not.toThrow();
  });
});
