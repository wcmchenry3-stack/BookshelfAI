/**
 * The scan queue with the real AI consent provider (#1023): photos saved
 * before consent wait (no prompt at launch or on reconnect), go once consent
 * is given, a user's own scan asks first, text searches never wait, and a
 * server `ai_consent_required` asks again and then sends the photo.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';

import { AiConsentProvider, type AiConsentContextValue } from '../../contexts/AiConsentContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { ScanJobProvider, type ScanJobContextValue } from '../../contexts/ScanJobContext';
import { useAiConsent } from '../../hooks/useAiConsent';
import { useScanJobs } from '../../hooks/useScanJobs';
import { AI_CONSENT_ERROR } from '../../lib/scanErrors';
import { queuedJob, refusal, resetScanMocks, scanMocks, scanOk } from '../helpers/scanJobMocks';

jest.unmock('../../hooks/useAiConsent');
jest.mock('../../hooks/useAuth', () => require('../helpers/scanJobMocks').authedModule());
const mockDelete = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    post: (...a: unknown[]) => require('../helpers/scanJobMocks').scanMocks.post(...a),
    get: (...a: unknown[]) => require('../helpers/scanJobMocks').scanMocks.get(...a),
    delete: (...a: unknown[]) => mockDelete(...a),
  },
}));
jest.mock('../../lib/scanJobStorage', () => require('../helpers/scanJobMocks').scanStorageModule());
jest.mock('@react-native-community/netinfo', () =>
  require('../helpers/scanJobMocks').netInfoModule()
);
jest.mock('expo-crypto', () => require('../helpers/scanJobMocks').cryptoModule());
jest.mock('../../lib/sentry', () => require('../helpers/scanJobMocks').sentryModule());

const NONE = { ai_consent: false, ai_consent_at: null };
const GIVEN = { ai_consent: true, ai_consent_at: '2026-11-22T10:00:00+00:00' };

let me: unknown = NONE;
const scanPosts = () => scanMocks.post.mock.calls.filter((c) => c[0] === '/scan');
const grantPosts = () => scanMocks.post.mock.calls.filter((c) => c[0] === '/users/me/ai-consent');

let scan: ScanJobContextValue;
let consent: AiConsentContextValue;
function Probe() {
  // eslint-disable-next-line react-hooks/globals -- test probe, as in AuthContext.test.tsx
  scan = useScanJobs();
  // eslint-disable-next-line react-hooks/globals -- test probe
  consent = useAiConsent();
  return null;
}

async function setup() {
  await render(
    <BannerProvider>
      <AiConsentProvider>
        <ScanJobProvider>
          <Probe />
        </ScanJobProvider>
      </AiConsentProvider>
    </BannerProvider>
  );
  await waitFor(() => expect(scanMocks.saveJobs).toHaveBeenCalled());
}

const job = (id: string) => scan.jobs.find((j) => j.id === id);

beforeEach(() => {
  jest.resetAllMocks();
  resetScanMocks();
  me = NONE;
  scanMocks.get.mockImplementation(async (url: string) =>
    url === '/auth/me' ? { data: me } : { data: [{ title: 'Dune', author: 'Herbert' }] }
  );
  scanMocks.post.mockImplementation(async (url: string) =>
    url === '/users/me/ai-consent' ? { data: GIVEN } : scanOk()
  );
});

afterEach(() => {
  jest.useRealTimers();
});

describe('photos saved before consent', () => {
  it('wait on reconnect without a prompt, then go once consent is given', async () => {
    jest.useFakeTimers();
    scanMocks.loadJobs.mockResolvedValue([queuedJob(1), queuedJob(2)]);
    await setup();

    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(scanPosts()).toHaveLength(0);
    expect(job('q-1')).toMatchObject({ status: 'queued', error: AI_CONSENT_ERROR });
    // The drain stopped at the first photo: the second is still as it was.
    expect(job('q-2')?.status).toBe('queued');
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();

    // The next scan asks; Continue records consent and the saved photos go.
    await act(async () => {
      void consent.ensureConsent();
    });
    await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(grantPosts()).toHaveLength(1);
    expect(scanPosts()).toHaveLength(2);
    // Consent was recorded before the first photo was sent.
    const order = scanMocks.post.mock.calls.map((c) => c[0]);
    expect(order[0]).toBe('/users/me/ai-consent');
  });

  it('Not now keeps them waiting, and nothing is sent', async () => {
    jest.useFakeTimers();
    scanMocks.loadJobs.mockResolvedValue([queuedJob(1)]);
    await setup();
    await act(async () => {
      void consent.ensureConsent();
    });
    await fireEvent.press(screen.getByTestId('ai-consent-not-now'));
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(scanPosts()).toHaveLength(0);
    expect(job('q-1')?.status).toBe('queued');
  });
});

describe('a new scan', () => {
  it('asks first; Not now sends no photo, but a text search still goes', async () => {
    await setup();
    await act(async () => {
      scan.startScan('image', 'file:///new.jpg');
    });
    expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
    await fireEvent.press(screen.getByTestId('ai-consent-not-now'));
    await waitFor(() =>
      expect(scan.jobs[0]).toMatchObject({ status: 'queued', error: AI_CONSENT_ERROR })
    );
    expect(scanPosts()).toHaveLength(0);

    await act(async () => {
      scan.startScan('text', undefined, 'dune');
    });
    await waitFor(() =>
      expect(scanMocks.get).toHaveBeenCalledWith('/books/search', expect.anything())
    );
    expect(scanPosts()).toHaveLength(0);
  });

  it('Continue records consent, then the photo is sent', async () => {
    await setup();
    await act(async () => {
      scan.startScan('image', 'file:///new.jpg');
    });
    await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    await waitFor(() => expect(scanPosts()).toHaveLength(1));
    expect(scanMocks.post.mock.calls[0][0]).toBe('/users/me/ai-consent');
  });

  it('a user who consented on another device (or before a reinstall) is not asked', async () => {
    me = GIVEN;
    await setup();
    await act(async () => {
      scan.startScan('image', 'file:///new.jpg');
    });
    await waitFor(() => expect(scanPosts()).toHaveLength(1));
    expect(screen.queryByTestId('ai-consent-prompt')).toBeNull();
    expect(grantPosts()).toHaveLength(0);
  });
});

describe('the server says ai_consent_required (withdrawn elsewhere)', () => {
  it('asks again instead of an error; Continue sends the photo', async () => {
    me = GIVEN;
    let refused = false;
    scanMocks.post.mockImplementation(async (url: string) => {
      if (url === '/users/me/ai-consent') return { data: GIVEN };
      if (!refused) {
        refused = true;
        throw refusal(403, 'ai_consent_required');
      }
      return scanOk();
    });
    await setup();
    await act(async () => {
      scan.startScan('image', 'file:///new.jpg');
    });
    await waitFor(() => expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy());
    expect(scan.jobs[0]).toMatchObject({ status: 'queued', error: AI_CONSENT_ERROR });
    expect(screen.queryByText('Scan failed')).toBeNull();

    await fireEvent.press(screen.getByTestId('ai-consent-continue'));
    await waitFor(() => expect(scanPosts()).toHaveLength(2));
    await waitFor(() => expect(scan.jobs[0]?.status).toBe('complete'));
  });
});

describe('withdrawal', () => {
  it('takes effect at once: the next scan asks again', async () => {
    me = GIVEN;
    mockDelete.mockResolvedValue({
      data: { ai_consent: false, ai_consent_at: GIVEN.ai_consent_at },
    });
    await setup();
    await act(async () => {
      await consent.checkConsent();
    });
    await act(async () => {
      await consent.withdraw();
    });
    await act(async () => {
      scan.startScan('image', 'file:///new.jpg');
    });
    expect(screen.getByTestId('ai-consent-prompt')).toBeTruthy();
    expect(scanPosts()).toHaveLength(0);
  });
});
