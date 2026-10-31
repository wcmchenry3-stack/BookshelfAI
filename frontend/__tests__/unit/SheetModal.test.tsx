import React from 'react';
import { Modal, Text } from 'react-native';
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

  it('pins sheet props and dismiss wiring on the Modal and title header', async () => {
    const onHidden = jest.fn();
    const { getByText, onDismiss } = await setup({ onHidden });

    // The test renderer only exposes host nodes, so walk up from the title to the
    // composite Modal fiber and read the props it was rendered with.
    let fiber = getByText('Sheet title').unstable_fiber;
    while (fiber && fiber.type !== Modal) fiber = fiber.return;
    expect(fiber).not.toBeNull();
    const modal = { props: fiber!.memoizedProps };
    expect(modal.props.presentationStyle).toBe('pageSheet');
    expect(modal.props.animationType).toBe('slide');
    expect(modal.props.accessibilityViewIsModal).toBe(true);

    modal.props.onRequestClose();
    expect(onDismiss).toHaveBeenCalledTimes(1);

    modal.props.onDismiss();
    expect(onHidden).toHaveBeenCalledTimes(1);

    expect(getByText('Sheet title').props.accessibilityRole).toBe('header');
  });

  it('renders nothing when not visible', async () => {
    const { queryByText } = await setup({ visible: false });
    expect(queryByText('body content')).toBeNull();
  });
});
