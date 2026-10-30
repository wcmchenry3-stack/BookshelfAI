import React from 'react';
import { render, fireEvent, act } from '@testing-library/react-native';
import { FeedbackWidget } from '../../components/FeedbackWidget/FeedbackWidget';

// A submit whose resolution the test controls, to model "user closes mid-submit".
let resolveSubmit: (ok: boolean) => void = () => {};
jest.mock('../../components/FeedbackWidget/useFeedbackSubmit', () => ({
  DESCRIPTION_MAX: 2000,
  useFeedbackSubmit: () => ({
    status: 'idle',
    error: null,
    submit: () => new Promise<boolean>((res) => (resolveSubmit = res)),
    reset: jest.fn(),
  }),
}));

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        surface: '#fff',
        onSurface: '#000',
        onSurfaceVariant: '#666',
        onPrimary: '#fff',
        primary: '#00f',
        outline: '#ccc',
        surfaceContainerHigh: '#eee',
        error: '#f00',
        scrim: '#0000008c',
      },
      typography: {
        fontFamilyBody: 'Inter_400Regular',
        fontSizeLG: 20,
        fontSizeBase: 16,
        fontSizeSM: 14,
        fontSizeXS: 12,
      },
    },
  }),
}));

describe('FeedbackWidget close-during-submit guard', () => {
  it('closes once (not twice) when dismissed before the submit resolves, then still notifies', async () => {
    const onClose = jest.fn();
    const onSubmitted = jest.fn();
    const r = await render(<FeedbackWidget visible onClose={onClose} onSubmitted={onSubmitted} />);
    await fireEvent.changeText(r.getByPlaceholderText(/Describe what happened/), 'Desc');
    // fireEvent awaits the handler, which is pending until we resolve it.
    const pressing = fireEvent.press(r.getByText('Submit'));

    await fireEvent.press(r.getByLabelText('Close'));
    expect(onClose).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveSubmit(true);
      await pressing;
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSubmitted).toHaveBeenCalledTimes(1);
  });
});
