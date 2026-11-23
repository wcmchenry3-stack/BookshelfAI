/**
 * The default `expo-image-manipulator` under Jest (#668, installed by
 * jest.setup.ts): the native module cannot load, so a photo "renders" at
 * `imageManipulatorMock.size` and saves to `file:///cache/shrunk-N.jpg`. Tests
 * of the resize read `calls` and change `size` or `failure`, then call
 * `resetImageManipulatorMock()` afterwards.
 *
 * Plain functions, not `jest.fn()`: a file that calls `jest.resetAllMocks()`
 * would otherwise wipe their answers. Must NOT import app code (it is loaded
 * by a jest.mock factory).
 */
export interface ManipulatorCalls {
  manipulate: { uri: string; options?: { maxWidth?: number; maxHeight?: number } }[];
  resize: { width?: number; height?: number }[];
  save: { format?: string; compress?: number }[];
  released: number;
}

const freshCalls = (): ManipulatorCalls => ({ manipulate: [], resize: [], save: [], released: 0 });

export const imageManipulatorMock = {
  /** The size a render reports (before any resize). */
  size: { width: 1568, height: 1176 },
  /** Set to make `saveAsync` reject. */
  failure: null as Error | null,
  calls: freshCalls(),
  seq: 0,
};

export function resetImageManipulatorMock() {
  imageManipulatorMock.size = { width: 1568, height: 1176 };
  imageManipulatorMock.failure = null;
  imageManipulatorMock.calls = freshCalls();
  imageManipulatorMock.seq = 0;
}

export function imageManipulatorModule() {
  const state = imageManipulatorMock;
  return {
    SaveFormat: { JPEG: 'jpeg', PNG: 'png', WEBP: 'webp' },
    ImageManipulator: {
      manipulate(uri: string, options?: { maxWidth?: number; maxHeight?: number }) {
        state.calls.manipulate.push({ uri, options });
        let { width, height } = state.size;
        const context = {
          resize(target: { width?: number; height?: number }) {
            state.calls.resize.push(target);
            if (target.width) {
              height = Math.round((height * target.width) / width);
              width = target.width;
            } else if (target.height) {
              width = Math.round((width * target.height) / height);
              height = target.height;
            }
            return context;
          },
          async renderAsync() {
            return {
              width,
              height,
              async saveAsync(opts?: { format?: string; compress?: number }) {
                state.calls.save.push({ ...opts });
                if (state.failure) throw state.failure;
                return { uri: `file:///cache/shrunk-${state.seq++}.jpg`, width, height };
              },
            };
          },
          release() {
            state.calls.released += 1;
          },
        };
        return context;
      },
    },
  };
}
