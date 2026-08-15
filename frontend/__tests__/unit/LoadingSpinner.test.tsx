import React from 'react';
import { render } from '@testing-library/react-native';

import { LoadingSpinner } from '../../components/LoadingSpinner';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: { primary: '#007AFF', textSecondary: '#888' },
      typography: { fontSizeBase: 16 },
    },
  }),
}));

describe('LoadingSpinner', () => {
  it('renders the activity indicator', async () => {
    const { getByTestId } = await render(<LoadingSpinner />);
    expect(getByTestId('loading-spinner')).toBeTruthy();
  });

  it('shows message text when provided', async () => {
    const { getByText } = await render(<LoadingSpinner message="Loading books…" />);
    expect(getByText('Loading books…')).toBeTruthy();
  });

  it('does not render message text when omitted', async () => {
    const { queryByText } = await render(<LoadingSpinner />);
    expect(queryByText(/./)).toBeNull();
  });
});
