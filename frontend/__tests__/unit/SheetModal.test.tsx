import React from 'react';
import { Text } from 'react-native';
import { fireEvent, render } from '@testing-library/react-native';

import SheetModal from '../../components/SheetModal';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: { background: '#fff', border: '#ccc', text: '#000', primary: '#007AFF' },
      typography: { fontSizeBase: 16, fontSizeLG: 20 },
    },
  }),
}));

async function setup(overrides: Partial<React.ComponentProps<typeof SheetModal>> = {}) {
  const onDismiss = jest.fn();
  const utils = await render(
    <SheetModal
      visible
      onDismiss={onDismiss}
      title="Sheet title"
      closeLabel="Cancel"
      closeA11yLabel="Close sheet"
      footer={<Text>footer content</Text>}
      {...overrides}
    >
      <Text>body content</Text>
    </SheetModal>
  );
  return { onDismiss, ...utils };
}

describe('SheetModal', () => {
  it('renders title, body, close label and footer', async () => {
    const { getByText } = await setup();
    expect(getByText('Sheet title')).toBeTruthy();
    expect(getByText('Cancel')).toBeTruthy();
    expect(getByText('body content')).toBeTruthy();
    expect(getByText('footer content')).toBeTruthy();
  });

  it('calls onDismiss from the close button with its a11y label', async () => {
    const { getByLabelText, onDismiss } = await setup();
    fireEvent.press(getByLabelText('Close sheet'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('renders nothing when not visible', async () => {
    const { queryByText } = await setup({ visible: false });
    expect(queryByText('body content')).toBeNull();
  });
});
