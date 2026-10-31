/**
 * Shared mocks and fixtures for the ScanJobProvider test files (#816).
 *
 * This module must NOT import app code: the `jest.mock` factories below are
 * `require`d from inside the mock factories of the test files, which run while
 * the app modules are still loading. Provider wrappers live in `providers.tsx`.
 *
 * Usage in a test file:
 *
 *   jest.mock('../../hooks/useAuth', () => require('../helpers/scanJobMocks').authedModule());
 *   jest.mock('../../lib/api', () => require('../helpers/scanJobMocks').scanApiModule());
 *   jest.mock('../../lib/scanJobStorage', () => require('../helpers/scanJobMocks').scanStorageModule());
 *   jest.mock('@react-native-community/netinfo', () => require('../helpers/scanJobMocks').netInfoModule());
 *   jest.mock('expo-crypto', () => require('../helpers/scanJobMocks').cryptoModule());
 *
 * `jest.mock` calls stay in the test file because babel-jest hoists them.
 */

export type NetListener = (s: { isConnected: boolean | null }) => void;

/** One set of spies shared by the factories below and by the test files. */
export const scanMocks = {
  post: jest.fn(),
  get: jest.fn(),
  loadJobs: jest.fn(),
  saveJobs: jest.fn(),
  netFetch: jest.fn(),
  captureMessage: jest.fn(),
  captureException: jest.fn(),
  /** The callback the provider registered with NetInfo.addEventListener. */
  netListener: null as NetListener | null,
  /** Counter behind the mocked expo-crypto randomUUID. */
  uuid: 0,
};

/**
 * Put the spies back to their defaults: an empty stored queue, a save that
 * resolves, an online device and a fresh uuid counter. Call it from
 * `beforeEach` right after `jest.resetAllMocks()`.
 */
export function resetScanMocks() {
  scanMocks.uuid = 0;
  scanMocks.netListener = null;
  scanMocks.saveJobs.mockResolvedValue(undefined);
  scanMocks.loadJobs.mockResolvedValue([]);
  scanMocks.netFetch.mockResolvedValue({ isConnected: true });
}

// ── jest.mock factories ─────────────────────────────────────────────────────

export const authedModule = () => ({ useAuth: () => ({ isAuthenticated: true }) });

export const scanApiModule = () => ({
  api: {
    post: (...a: unknown[]) => scanMocks.post(...a),
    get: scanMocks.get,
  },
});

export const scanStorageModule = () => ({
  loadJobs: () => scanMocks.loadJobs(),
  saveJobs: (...a: unknown[]) => scanMocks.saveJobs(...a),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: jest.fn(),
});

export const netInfoModule = () => ({
  __esModule: true,
  default: {
    fetch: () => scanMocks.netFetch(),
    addEventListener: (cb: NetListener) => {
      scanMocks.netListener = cb;
      return () => {};
    },
  },
});

export const sentryModule = () => ({
  Sentry: {
    addBreadcrumb: jest.fn(),
    captureException: (...a: unknown[]) => scanMocks.captureException(...a),
    captureMessage: (...a: unknown[]) => scanMocks.captureMessage(...a),
  },
});

export const cryptoModule = () => ({ randomUUID: () => `mockUuid-${++scanMocks.uuid}` });

// ── Fixtures ────────────────────────────────────────────────────────────────

/** An axios-style refusal: `{ response: { status, data: { detail, ...extra }, headers } }`. */
export function refusal(
  status: number,
  detail: string,
  extra: Record<string, unknown> = {},
  headers: Record<string, string> = {}
) {
  return { response: { status, data: { detail, ...extra }, headers } };
}

/** A successful POST /scan reply carrying one book. */
export function scanOk(remaining: number | null = 5) {
  return {
    data: {
      books: [{ title: 'Dune', author: 'Herbert' }],
      enhanced: false,
      enhanced_scans_remaining: remaining,
    },
  };
}

/** A stored queued image job. Defaults match the ids and uris the drain tests use. */
export function queuedImageJob(
  id: string,
  createdAt: number,
  imageUri: string,
  extra: Record<string, unknown> = {}
) {
  return {
    id,
    type: 'image' as const,
    status: 'queued' as const,
    createdAt,
    imageUri,
    retryCount: 0,
    ...extra,
  };
}

/** `q-<i>` / `file:///q<i>.jpg`: the numbered queued job used by the drain tests. */
export function queuedJob(i: number, extra: Record<string, unknown> = {}) {
  return queuedImageJob(`q-${i}`, i, `file:///q${i}.jpg`, extra);
}
