// Settings "AI photo scanning" row (#1023): shows the state and withdraws.
// useAiConsent is the shared Jest mock (helpers/aiConsentMock.ts).
import React from 'react';
import { act, fireEvent, render } from '@testing-library/react-native';

import { AiConsentSetting } from '../../components/AiConsentSetting';
import { BannerContext } from '../../contexts/BannerContext';
import { aiConsentMock, resetAiConsentMock } from '../helpers/aiConsentMock';

const mockShowBanner = jest.fn();

function renderRow() {
  return render(
    <BannerContext.Provider
      value={{ banner: null, showBanner: mockShowBanner, hideBanner: jest.fn() }}
    >
      <AiConsentSetting />
    </BannerContext.Provider>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  resetAiConsentMock();
});
afterAll(resetAiConsentMock);

describe('Settings AI photo scanning row (#1023)', () => {
  it('with consent: says Allowed and offers Withdraw', async () => {
    const utils = await renderRow();
    expect(utils.getByText('AI photo scanning')).toBeTruthy();
    expect(utils.getByTestId('settings-ai-consent-value').props.children).toBe('Allowed');
    const button = utils.getByTestId('settings-ai-consent-withdraw');
    expect(button.props.accessibilityRole).toBe('button');
    expect(button.props.accessibilityHint).toMatch(/Anthropic/);
    expect(utils.getByLabelText('AI photo scanning: Allowed')).toBeTruthy();
  });

  it('Withdraw withdraws on the server and confirms', async () => {
    const withdraw = jest.spyOn(aiConsentMock, 'withdraw');
    const utils = await renderRow();
    await fireEvent.press(utils.getByTestId('settings-ai-consent-withdraw'));
    expect(withdraw).toHaveBeenCalledTimes(1);
    expect(mockShowBanner).toHaveBeenCalledWith({
      message: "AI scanning is off. You'll be asked before your next scan.",
      type: 'info',
      duration: 5000,
    });
  });

  it('a failed withdrawal says so', async () => {
    jest.spyOn(aiConsentMock, 'withdraw').mockResolvedValue(false);
    const utils = await renderRow();
    await fireEvent.press(utils.getByTestId('settings-ai-consent-withdraw'));
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error', message: expect.stringMatching(/withdraw/) })
    );
  });

  it('ignores a second press while withdrawing', async () => {
    let finish!: (ok: boolean) => void;
    const withdraw = jest
      .spyOn(aiConsentMock, 'withdraw')
      .mockReturnValue(new Promise<boolean>((r) => (finish = r)));
    const utils = await renderRow();
    await fireEvent.press(utils.getByTestId('settings-ai-consent-withdraw'));
    await fireEvent.press(utils.getByTestId('settings-ai-consent-withdraw'));
    expect(withdraw).toHaveBeenCalledTimes(1);
    await act(async () => finish(true));
  });

  it('without consent: says Not allowed, explains the next scan asks, no button', async () => {
    aiConsentMock.status = 'none';
    const utils = await renderRow();
    expect(utils.getByTestId('settings-ai-consent-value').props.children).toBe('Not allowed');
    expect(utils.getByText("You'll be asked before your next scan.")).toBeTruthy();
    expect(utils.queryByTestId('settings-ai-consent-withdraw')).toBeNull();
  });

  it('unknown: reads the state once and shows nothing until it is known', async () => {
    aiConsentMock.status = 'unknown';
    const check = jest.spyOn(aiConsentMock, 'checkConsent');
    const utils = await renderRow();
    expect(check).toHaveBeenCalled();
    expect(utils.queryByTestId('settings-ai-consent')).toBeNull();
  });
});
