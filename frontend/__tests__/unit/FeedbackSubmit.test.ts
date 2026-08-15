import { renderHook, act } from '@testing-library/react-native';
import { useFeedbackSubmit } from '../../components/FeedbackWidget/useFeedbackSubmit';
import { SessionLogger } from '../../components/FeedbackWidget/SessionLogger';

// Ensure the global fetch mock is available
const mockFetch = jest.fn() as jest.MockedFunction<typeof fetch>;
global.fetch = mockFetch;

const WORKER_URL = 'https://feedback-worker.wcmchenry3.workers.dev';

function mockEnv(url: string) {
  process.env.EXPO_PUBLIC_FEEDBACK_WORKER_URL = url;
}

beforeEach(() => {
  mockFetch.mockReset();
  SessionLogger._reset();
  mockEnv(WORKER_URL);
});

afterEach(() => {
  delete process.env.EXPO_PUBLIC_FEEDBACK_WORKER_URL;
  SessionLogger._reset();
});

const basePayload = {
  title: 'Test title',
  description: 'Test description',
  type: 'bug' as const,
};

describe('useFeedbackSubmit', () => {
  describe('initial state', () => {
    it('starts idle with no result or error', async () => {
      const { result } = await renderHook(() => useFeedbackSubmit());
      expect(result.current.status).toBe('idle');
      expect(result.current.result).toBeNull();
      expect(result.current.error).toBeNull();
    });
  });

  describe('successful submission', () => {
    it('transitions idle → submitting → success', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 201,
        json: async () => ({ issueNumber: 42, issueUrl: 'https://github.com/issues/42' }),
        headers: { get: () => null },
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());

      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.status).toBe('success');
      expect(result.current.result).toEqual({
        issueNumber: 42,
        issueUrl: 'https://github.com/issues/42',
      });
      expect(result.current.error).toBeNull();
    });

    it('sends appId: gaming_app in request body', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 201,
        json: async () => ({ issueNumber: 1, issueUrl: 'https://github.com/issues/1' }),
        headers: { get: () => null },
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.appId).toBe('book_app');
    });

    it('attaches session logs when present', async () => {
      SessionLogger.init();
      console.warn('captured log');

      mockFetch.mockResolvedValueOnce({
        status: 201,
        json: async () => ({ issueNumber: 1, issueUrl: '' }),
        headers: { get: () => null },
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.sessionLogs).toMatch(/captured log/);
    });

    it('omits sessionLogs key when buffer is empty', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 201,
        json: async () => ({ issueNumber: 1, issueUrl: '' }),
        headers: { get: () => null },
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      const [, init] = mockFetch.mock.calls[0];
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.sessionLogs).toBeUndefined();
    });
  });

  describe('rate limit (429)', () => {
    it('sets error kind: rate_limit with Retry-After from header', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 429,
        headers: { get: (h: string) => (h === 'Retry-After' ? '120' : null) },
        json: async () => ({}),
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'rate_limit', retryAfterSeconds: 120 });
    });

    it('defaults retryAfterSeconds to 60 when Retry-After header absent', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 429,
        headers: { get: () => null },
        json: async () => ({}),
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.error?.retryAfterSeconds).toBe(60);
    });
  });

  describe('rejected (422)', () => {
    it('sets error kind: rejected', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 422,
        headers: { get: () => null },
        json: async () => ({}),
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'rejected' });
    });
  });

  describe('network error', () => {
    it('sets error kind: network when fetch throws', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network request failed'));

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'network' });
    });
  });

  describe('unknown error', () => {
    it('sets error kind: unknown for unexpected status codes', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 500,
        headers: { get: () => null },
        json: async () => ({}),
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'unknown' });
    });
  });

  describe('no-op when WORKER_URL unset', () => {
    it('returns without calling fetch', async () => {
      delete process.env.EXPO_PUBLIC_FEEDBACK_WORKER_URL;

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(mockFetch).not.toHaveBeenCalled();
      // Status stays idle (no transition since the hook bails early before setStatus)
      expect(result.current.status).toBe('idle');
    });
  });

  describe('reset()', () => {
    it('clears status, result, and error back to initial state', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 201,
        json: async () => ({ issueNumber: 5, issueUrl: '' }),
        headers: { get: () => null },
      } as unknown as Response);

      const { result } = await renderHook(() => useFeedbackSubmit());
      await act(async () => {
        await result.current.submit(basePayload);
      });
      expect(result.current.status).toBe('success');

      await act(() => {
        result.current.reset();
      });

      expect(result.current.status).toBe('idle');
      expect(result.current.result).toBeNull();
      expect(result.current.error).toBeNull();
    });
  });
});
