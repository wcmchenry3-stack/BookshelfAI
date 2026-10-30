import { renderHook, act } from '@testing-library/react-native';
import {
  useFeedbackSubmit,
  type FeedbackPayload,
  _resetFeedbackThrottle,
  DESCRIPTION_MAX,
  FEEDBACK_RATE_LIMIT_MAX,
  FEEDBACK_RATE_LIMIT_WINDOW_MS,
} from '../../components/FeedbackWidget/useFeedbackSubmit';
import { SessionLogger } from '../../components/FeedbackWidget/SessionLogger';
import { Sentry } from '../../lib/sentry';

jest.mock('../../lib/sentry', () => ({
  Sentry: { getClient: jest.fn(), captureFeedback: jest.fn() },
}));

const mockGetClient = Sentry.getClient as jest.Mock;
const mockCaptureFeedback = Sentry.captureFeedback as jest.Mock;

beforeEach(() => {
  mockGetClient.mockReset().mockReturnValue({});
  mockCaptureFeedback.mockReset();
  SessionLogger._reset();
  _resetFeedbackThrottle();
});

afterEach(() => {
  SessionLogger._reset();
  jest.useRealTimers();
});

const basePayload: FeedbackPayload = {
  description: 'Test description',
  type: 'bug',
};

async function submitOnce(payload = basePayload) {
  const hook = await renderHook(() => useFeedbackSubmit());
  let ok = false;
  await act(async () => {
    ok = await hook.result.current.submit(payload);
  });
  return { ...hook, ok };
}

describe('useFeedbackSubmit', () => {
  it('starts idle with no error', async () => {
    const { result } = await renderHook(() => useFeedbackSubmit());
    expect(result.current.status).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  describe('successful submission', () => {
    it('resolves true and sends description, source and type tag to Sentry', async () => {
      const { result, ok } = await submitOnce();

      expect(ok).toBe(true);
      expect(result.current.status).toBe('success');
      expect(result.current.error).toBeNull();
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(1);
      const [params] = mockCaptureFeedback.mock.calls[0];
      expect(params.message).toBe('Test description');
      expect(params.source).toBe('in_app_feedback');
      expect(params.tags).toEqual({ 'feedback.type': 'bug' });
    });

    it('sends no name or email', async () => {
      await submitOnce();
      const [params] = mockCaptureFeedback.mock.calls[0];
      expect(params.name).toBeUndefined();
      expect(params.email).toBeUndefined();
    });

    it('truncates the description to DESCRIPTION_MAX', async () => {
      await submitOnce({ ...basePayload, description: 'x'.repeat(DESCRIPTION_MAX + 500) });
      const [params] = mockCaptureFeedback.mock.calls[0];
      expect(params.message).toBe('x'.repeat(DESCRIPTION_MAX));
    });

    it('attaches session logs to a bug report', async () => {
      SessionLogger.init();
      console.warn('captured log');

      await submitOnce();

      const [, hint] = mockCaptureFeedback.mock.calls[0];
      expect(hint.attachments).toHaveLength(1);
      expect(hint.attachments[0].filename).toBe('session-logs.txt');
      expect(hint.attachments[0].data).toMatch(/captured log/);
    });

    it('does not attach session logs to a feature request', async () => {
      SessionLogger.init();
      console.warn('captured log');

      await submitOnce({ ...basePayload, type: 'feature' });

      const [params, hint] = mockCaptureFeedback.mock.calls[0];
      expect(params.tags).toEqual({ 'feedback.type': 'feature' });
      expect(hint?.attachments).toBeUndefined();
    });

    it('omits the attachment when the log buffer is empty', async () => {
      await submitOnce();
      const [, hint] = mockCaptureFeedback.mock.calls[0];
      expect(hint?.attachments).toBeUndefined();
    });
  });

  describe('client throttle (5 per 10 minutes)', () => {
    async function submitN(
      result: { current: ReturnType<typeof useFeedbackSubmit> },
      n: number
    ): Promise<boolean> {
      let ok = false;
      for (let i = 0; i < n; i++) {
        await act(async () => {
          ok = await result.current.submit(basePayload);
        });
      }
      return ok;
    }

    it('allows 5 submissions then rejects the 6th with retry seconds', async () => {
      jest.useFakeTimers().setSystemTime(new Date('2026-01-01T00:00:00Z'));
      const { result } = await renderHook(() => useFeedbackSubmit());

      expect(await submitN(result, FEEDBACK_RATE_LIMIT_MAX)).toBe(true);
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(FEEDBACK_RATE_LIMIT_MAX);

      jest.setSystemTime(new Date('2026-01-01T00:04:00Z'));
      expect(await submitN(result, 1)).toBe(false);
      expect(result.current.status).toBe('error');
      // Oldest submission was at t=0, so 10 min - 4 min = 360 s remain.
      expect(result.current.error).toEqual({ kind: 'rate_limit', retryAfterSeconds: 360 });
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(FEEDBACK_RATE_LIMIT_MAX);
    });

    it('allows another submission once the window has passed', async () => {
      jest.useFakeTimers().setSystemTime(new Date('2026-01-01T00:00:00Z'));
      const { result } = await renderHook(() => useFeedbackSubmit());
      await submitN(result, FEEDBACK_RATE_LIMIT_MAX);

      jest.setSystemTime(Date.now() + FEEDBACK_RATE_LIMIT_WINDOW_MS + 1);
      expect(await submitN(result, 1)).toBe(true);
      expect(result.current.status).toBe('success');
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(FEEDBACK_RATE_LIMIT_MAX + 1);
    });
  });

  describe('failure', () => {
    it('resolves false and does not send when Sentry is not initialised', async () => {
      mockGetClient.mockReturnValue(undefined);

      const { result, ok } = await submitOnce();

      expect(ok).toBe(false);
      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'unavailable' });
      expect(mockCaptureFeedback).not.toHaveBeenCalled();
    });

    it('resolves false and skips the throttle when captureFeedback throws', async () => {
      mockCaptureFeedback.mockImplementationOnce(() => {
        throw new Error('boom');
      });

      const { result, ok } = await submitOnce();
      expect(ok).toBe(false);
      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'unavailable' });

      await act(async () => {
        await result.current.submit(basePayload);
      });
      expect(result.current.status).toBe('success');
    });
  });

  describe('reset()', () => {
    it('clears status and error back to initial state', async () => {
      mockGetClient.mockReturnValue(undefined);
      const { result } = await submitOnce();
      expect(result.current.status).toBe('error');

      await act(() => {
        result.current.reset();
      });

      expect(result.current.status).toBe('idle');
      expect(result.current.error).toBeNull();
    });
  });
});
