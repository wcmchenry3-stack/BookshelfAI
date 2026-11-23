/**
 * The AI consent prompt and its state (#1023): AiConsentProvider with the
 * real prompt, the API mocked at lib/api.
 */
import React from 'react';
import { Linking } from 'react-native';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { AiConsentProvider, type AiConsentContextValue } from '../../contexts/AiConsentContext';
import { AuthContext } from '../../contexts/AuthContext';
import { useAiConsent } from '../../hooks/useAiConsent';
import {
  aiConsentFromBody,
  grantAiConsent,
  PRIVACY_POLICY_URL,
  withdrawAiConsent,
} from '../../lib/aiConsent';

jest.unmock('../../hooks/useAiConsent');

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockDelete = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: (...a: unknown[]) => mockGet(...a),
    post: (...a: unknown[]) => mockPost(...a),
    delete: (...a: unknown[]) => mockDelete(...a),
  },
}));
jest.mock('../../lib/sentry', () => ({ Sentry: { addBreadcrumb: jest.fn() } }));

const GIVEN = { ai_consent: true, ai_consent_at: '2026-11-22T10:00:00+00:00' };
const NONE = { ai_consent: false, ai_consent_at: null };
const WITHDRAWN = { ai_consent: false, ai_consent_at: '2026-11-22T10:00:00+00:00' };

let consent: AiConsentContextValue;
// Each test's tree writes `consent`; a tree left mounted by an earlier test
// must not overwrite it.
let currentTree = 0;
function Probe({ tree: id }: { tree: number }) {
  const value = useAiConsent();
  // eslint-disable-next-line react-hooks/globals -- test probe, as in AuthContext.test.tsx
  if (id === currentTree) consent = value;
  return null;
}

function authValue(isAuthenticated: boolean) {
  return {
    isAuthenticated,
    isLoading: false,
    defaultTab: null,
    login: async () => {},
    testLogin: async () => {},
    logout: async () => {},
  };
}

function tree(isAuthenticated = true) {
  return (
    <AuthContext.Provider value={authValue(isAuthenticated)}>
      <AiConsentProvider>
        <Probe tree={currentTree} />
      </AiConsentProvider>
    </AuthContext.Provider>
  );
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  currentTree += 1;
  jest.resetAllMocks();
  mockGet.mockResolvedValue({ data: NONE });
  mockPost.mockResolvedValue({ data: GIVEN });
  mockDelete.mockResolvedValue({ data: WITHDRAWN });
});

describe('lib/aiConsent', () => {
  it('reads the state from a GET /auth/me or consent body', () => {
    expect(aiConsentFromBody(GIVEN)).toEqual({ given: true, givenAt: GIVEN.ai_consent_at });
    expect(aiConsentFromBody(WITHDRAWN)).toEqual({
      given: false,
      givenAt: WITHDRAWN.ai_consent_at,
    });
    expect(aiConsentFromBody({})).toBeNull();
    expect(aiConsentFromBody(null)).toBeNull();
    expect(aiConsentFromBody('x')).toBeNull();
  });

  it('grants with POST and withdraws with DELETE, sending no timestamp', async () => {
    await expect(grantAiConsent()).resolves.toEqual({ given: true, givenAt: GIVEN.ai_consent_at });
    expect(mockPost).toHaveBeenCalledWith('/users/me/ai-consent');
    await expect(withdrawAiConsent()).resolves.toMatchObject({ given: false });
    expect(mockDelete).toHaveBeenCalledWith('/users/me/ai-consent');
  });

  it('reads the state back when the write was applied without its response (#685)', async () => {
    mockPost.mockRejectedValue({
      response: { status: 409, data: { detail: 'idempotency_outcome_unknown' } },
    });
    mockDelete.mockRejectedValue({
      response: { status: 409, data: { detail: 'idempotency_outcome_unknown' } },
    });
    mockGet.mockResolvedValueOnce({ data: GIVEN }).mockResolvedValueOnce({ data: WITHDRAWN });
    await expect(grantAiConsent()).resolves.toEqual({ given: true, givenAt: GIVEN.ai_consent_at });
    await expect(withdrawAiConsent()).resolves.toMatchObject({ given: false });
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('rejects when that read-back fails, and on any other error', async () => {
    const unknown = { response: { status: 409, data: { detail: 'idempotency_outcome_unknown' } } };
    mockPost.mockRejectedValue(unknown);
    mockGet.mockRejectedValue(new Error('offline'));
    await expect(grantAiConsent()).rejects.toBe(unknown);
    const other = { response: { status: 409, data: { detail: 'idempotency_in_progress' } } };
    mockDelete.mockRejectedValue(other);
    await expect(withdrawAiConsent()).rejects.toBe(other);
  });

  it('rejects an answer without the consent state', async () => {
    mockPost.mockResolvedValue({ data: {} });
    await expect(grantAiConsent()).rejects.toThrow();
  });
});

describe('AiConsentProvider', () => {
  it('never reads the state or shows the prompt on its own (no pop-up at launch)', async () => {
    await render(tree());
    await act(async () => {});
    expect(mockGet).not.toHaveBeenCalled();
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
    expect(consent.status).toBe('unknown');
  });

  it('a user who already consented is not asked', async () => {
    mockGet.mockResolvedValue({ data: GIVEN });
    await render(tree());
    let ok: boolean | undefined;
    await act(async () => {
      ok = await consent.ensureConsent();
    });
    expect(ok).toBe(true);
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
    expect(consent.status).toBe('given');
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('asks once: Continue records consent, then the scan may go and is never asked again', async () => {
    await render(tree());
    let answer!: Promise<boolean>;
    await act(async () => {
      answer = consent.ensureConsent();
    });
    expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
    expect(screen.getByText('Scanning uses AI')).toBeTruthy();
    expect(screen.getByText(/sends your photo to Anthropic, an AI provider/)).toBeTruthy();

    await act(async () => {
      await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    });
    await expect(answer).resolves.toBe(true);
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(consent.status).toBe('given');
    expect(consent.givenAt).toBe(GIVEN.ai_consent_at);
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();

    let again: boolean | undefined;
    await act(async () => {
      again = await consent.ensureConsent();
    });
    expect(again).toBe(true);
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('waits for the grant call: nothing resolves before the server stored consent', async () => {
    const grant = deferred<{ data: unknown }>();
    mockPost.mockReturnValue(grant.promise);
    await render(tree());
    let settled = false;
    let answer!: Promise<boolean>;
    await act(async () => {
      answer = consent.ensureConsent();
      void answer.then(() => (settled = true));
    });
    await act(async () => {
      await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    });
    expect(settled).toBe(false);
    expect(consent.status).toBe('none');
    await act(async () => {
      grant.resolve({ data: GIVEN });
    });
    await expect(answer).resolves.toBe(true);
  });

  it('a failed grant keeps the prompt open with an announced error; retry works', async () => {
    mockPost.mockRejectedValueOnce(new Error('offline'));
    await render(tree());
    let answer!: Promise<boolean>;
    await act(async () => {
      answer = consent.ensureConsent();
    });
    await act(async () => {
      await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    });
    const error = screen.getByTestId('ai-consent-error');
    expect(error.props.accessibilityRole).toBe('alert');
    expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
    expect(consent.status).toBe('none');

    await act(async () => {
      await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    });
    await expect(answer).resolves.toBe(true);
    expect(mockPost).toHaveBeenCalledTimes(2);
  });

  it('an answer that is not consent keeps the prompt open', async () => {
    mockPost.mockResolvedValueOnce({ data: NONE });
    await render(tree());
    await act(async () => {
      void consent.ensureConsent();
    });
    await act(async () => {
      await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    });
    expect(screen.getByTestId('ai-consent-error')).toBeTruthy();
  });

  it('Not now records nothing, resolves false, and the next scan asks again', async () => {
    await render(tree());
    let answer!: Promise<boolean>;
    await act(async () => {
      answer = consent.ensureConsent();
    });
    await act(async () => {
      await fireEvent.press(screen.getByTestId('ai-consent-not-now'));
    });
    await expect(answer).resolves.toBe(false);
    expect(mockPost).not.toHaveBeenCalled();
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();

    await act(async () => {
      void consent.ensureConsent();
    });
    expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
  });

  it('callers while the prompt is open share its answer', async () => {
    await render(tree());
    let a!: Promise<boolean>;
    let b!: Promise<boolean>;
    await act(async () => {
      a = consent.ensureConsent();
    });
    await act(async () => {
      b = consent.ensureConsent();
    });
    await act(async () => {
      await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    });
    await expect(Promise.all([a, b])).resolves.toEqual([true, true]);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('checkConsent never asks', async () => {
    await render(tree());
    let ok: boolean | undefined;
    await act(async () => {
      ok = await consent.checkConsent();
    });
    expect(ok).toBe(false);
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
  });

  describe('when the state cannot be read (offline)', () => {
    beforeEach(() => mockGet.mockRejectedValue(new Error('offline')));

    it('allowUnknown lets a photo be taken without asking (it only waits on the device)', async () => {
      await render(tree());
      let ok: boolean | undefined;
      await act(async () => {
        ok = await consent.ensureConsent({ allowUnknown: true });
      });
      expect(ok).toBe(true);
      expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
      expect(consent.status).toBe('unknown');
    });

    it('an upload is never allowed: checkConsent is false and ensureConsent asks', async () => {
      await render(tree());
      let ok: boolean | undefined;
      await act(async () => {
        ok = await consent.checkConsent();
      });
      expect(ok).toBe(false);
      await act(async () => {
        void consent.ensureConsent();
      });
      expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
    });

    it('tries again next time instead of remembering the failure', async () => {
      await render(tree());
      await act(async () => {
        await consent.checkConsent();
      });
      mockGet.mockResolvedValue({ data: GIVEN });
      let ok: boolean | undefined;
      await act(async () => {
        ok = await consent.checkConsent();
      });
      expect(ok).toBe(true);
      expect(mockGet).toHaveBeenCalledTimes(2);
    });
  });

  it('markRequired (the server refused a scan) makes the next scan ask again', async () => {
    mockGet.mockResolvedValue({ data: GIVEN });
    await render(tree());
    await act(async () => {
      await consent.checkConsent();
    });
    await act(async () => consent.markRequired());
    expect(consent.status).toBe('none');
    await act(async () => {
      void consent.ensureConsent();
    });
    expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
  });

  it('withdraw stores the withdrawal and the next scan asks again', async () => {
    mockGet.mockResolvedValue({ data: GIVEN });
    await render(tree());
    await act(async () => {
      await consent.checkConsent();
    });
    let ok: boolean | undefined;
    await act(async () => {
      ok = await consent.withdraw();
    });
    expect(ok).toBe(true);
    expect(mockDelete).toHaveBeenCalledWith('/users/me/ai-consent');
    expect(consent.status).toBe('none');
    expect(consent.givenAt).toBeNull();
    await act(async () => {
      void consent.ensureConsent();
    });
    expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
  });

  it('a failed withdrawal keeps the consent and resolves false', async () => {
    mockGet.mockResolvedValue({ data: GIVEN });
    mockDelete.mockRejectedValue(new Error('offline'));
    await render(tree());
    await act(async () => {
      await consent.checkConsent();
    });
    let ok: boolean | undefined;
    await act(async () => {
      ok = await consent.withdraw();
    });
    expect(ok).toBe(false);
    expect(consent.status).toBe('given');
  });

  it('sign-out forgets the account and closes an open prompt as Not now', async () => {
    mockGet.mockResolvedValue({ data: NONE });
    const view = await render(tree(true));
    let answer!: Promise<boolean>;
    await act(async () => {
      answer = consent.ensureConsent();
    });
    await view.rerender(tree(false));
    await expect(answer).resolves.toBe(false);
    expect(consent.status).toBe('unknown');
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
  });

  it('an answer for the signed-out account is dropped', async () => {
    const read = deferred<{ data: unknown }>();
    mockGet.mockReturnValue(read.promise);
    const view = await render(tree(true));
    let check!: Promise<boolean>;
    await act(async () => {
      check = consent.checkConsent();
    });
    await view.rerender(tree(false));
    await act(async () => {
      read.resolve({ data: GIVEN });
    });
    await expect(check).resolves.toBe(false);
    expect(consent.status).toBe('unknown');
  });

  it('signed out while the state is read: resolves false and never asks (round 1 #2)', async () => {
    const read = deferred<{ data: unknown }>();
    mockGet.mockReturnValue(read.promise);
    const view = await render(tree(true));
    let answer!: Promise<boolean>;
    await act(async () => {
      answer = consent.ensureConsent();
    });
    await view.rerender(tree(false));
    await act(async () => {
      read.resolve({ data: NONE });
    });
    await expect(answer).resolves.toBe(false);
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
  });

  it('a read that started before a withdrawal cannot bring the consent back (round 1 #6)', async () => {
    const read = deferred<{ data: unknown }>();
    mockGet.mockReturnValue(read.promise);
    await render(tree());
    let check!: Promise<boolean>;
    await act(async () => {
      check = consent.checkConsent();
    });
    await act(async () => {
      await consent.withdraw();
    });
    expect(consent.status).toBe('none');
    // The older read answers with the state from before the withdrawal.
    await act(async () => {
      read.resolve({ data: GIVEN });
    });
    await expect(check).resolves.toBe(false);
    expect(consent.status).toBe('none');
  });

  it('the prompt is accessible: a header, a policy link, labelled buttons', async () => {
    await render(tree());
    await act(async () => {
      void consent.ensureConsent();
    });
    expect(screen.getByRole('header', { name: 'Scanning uses AI' })).toBeTruthy();
    const link = screen.getByTestId('ai-consent-privacy');
    expect(link.props.accessibilityRole).toBe('link');
    expect(screen.getByRole('button', { name: 'Continue' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Not now' })).toBeTruthy();
  });

  it('the privacy policy link opens the hosted policy', async () => {
    const open = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined);
    await render(tree());
    await act(async () => {
      void consent.ensureConsent();
    });
    await fireEvent.press(screen.getByTestId('ai-consent-privacy'));
    expect(open).toHaveBeenCalledWith(PRIVACY_POLICY_URL);
    open.mockRejectedValueOnce(new Error('no browser'));
    await fireEvent.press(screen.getByTestId('ai-consent-privacy'));
    await waitFor(() => expect(open).toHaveBeenCalledTimes(2));
  });
});
