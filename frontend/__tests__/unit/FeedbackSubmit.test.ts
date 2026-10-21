import { renderHook, act } from '@testing-library/react-native';
import {
  useFeedbackSubmit,
  _resetFeedbackThrottle,
  DESCRIPTION_MAX,
  SUBMIT_COOLDOWN_MS,
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

const basePayload = {
  title: 'Test title',
  description: 'Test description',
  type: 'bug' as const,
};

async function submitOnce(payload = basePayload) {
  const hook = await renderHook(() => useFeedbackSubmit());
  await act(async () => {
    await hook.result.current.submit(payload);
  });
  return hook;
}

describe('useFeedbackSubmit', () => {
  it('starts idle with no error', async () => {
    const { result } = await renderHook(() => useFeedbackSubmit());
    expect(result.current.status).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  describe('successful submission', () => {
    it('sends title, description and type to Sentry and reports success', async () => {
      const { result } = await submitOnce();

      expect(result.current.status).toBe('success');
      expect(result.current.error).toBeNull();
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(1);
      const [params] = mockCaptureFeedback.mock.calls[0];
      expect(params.message).toBe('Test title\n\nTest description');
      expect(params.tags).toEqual({ feedback_type: 'bug' });
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
      expect(params.message).toBe(`Test title\n\n${'x'.repeat(DESCRIPTION_MAX)}`);
    });

    it('attaches session logs when present', async () => {
      SessionLogger.init();
      console.warn('captured log');

      await submitOnce();

      const [, hint] = mockCaptureFeedback.mock.calls[0];
      expect(hint.attachments).toHaveLength(1);
      expect(hint.attachments[0].filename).toBe('session-logs.txt');
      expect(hint.attachments[0].data).toMatch(/captured log/);
    });

    it('omits the attachment when the log buffer is empty', async () => {
      await submitOnce();
      const [, hint] = mockCaptureFeedback.mock.calls[0];
      expect(hint.attachments).toBeUndefined();
    });
  });

  describe('client throttle', () => {
    it('rejects a second submission inside the cooldown with rate_limit', async () => {
      jest.useFakeTimers().setSystemTime(new Date('2026-01-01T00:00:00Z'));
      const { result } = await submitOnce();
      expect(result.current.status).toBe('success');

      jest.setSystemTime(new Date('2026-01-01T00:00:20Z'));
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'rate_limit', retryAfterSeconds: 40 });
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(1);
    });

    it('allows another submission after the cooldown', async () => {
      jest.useFakeTimers().setSystemTime(new Date('2026-01-01T00:00:00Z'));
      const { result } = await submitOnce();

      jest.setSystemTime(Date.now() + SUBMIT_COOLDOWN_MS + 1);
      await act(async () => {
        await result.current.submit(basePayload);
      });

      expect(result.current.status).toBe('success');
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(2);
    });
  });

  describe('failure', () => {
    it('reports an error and does not send when Sentry is not initialised', async () => {
      mockGetClient.mockReturnValue(undefined);

      const { result } = await submitOnce();

      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'unknown' });
      expect(mockCaptureFeedback).not.toHaveBeenCalled();
    });

    it('reports an error and skips the throttle when captureFeedback throws', async () => {
      mockCaptureFeedback.mockImplementationOnce(() => {
        throw new Error('boom');
      });

      const { result } = await submitOnce();
      expect(result.current.status).toBe('error');
      expect(result.current.error).toEqual({ kind: 'unknown' });

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
