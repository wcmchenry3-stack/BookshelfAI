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

// Capture the props SheetModal passes to RN's Modal. The real jest mock still
// does the rendering (children only when visible), so behaviour is unchanged.
type ModalProps = {
  presentationStyle?: string;
  animationType?: string;
  accessibilityViewIsModal?: boolean;
  onRequestClose: () => void;
  onDismiss: () => void;
};
const mockModalProps: { current: ModalProps | null } = { current: null };
jest.mock('react-native/Libraries/Modal/Modal', () => {
  const React = jest.requireActual('react');
  const Actual = jest.requireActual('@react-native/jest-preset/jest/mocks/Modal').default;
  return {
    __esModule: true,
    default: function MockModal(props: ModalProps) {
      mockModalProps.current = props;
      return React.createElement(Actual, props);
    },
  };
});

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

    const modal = mockModalProps.current;
    expect(modal).not.toBeNull();
    expect(modal!.presentationStyle).toBe('pageSheet');
    expect(modal!.animationType).toBe('slide');
    expect(modal!.accessibilityViewIsModal).toBe(true);

    modal!.onRequestClose();
    expect(onDismiss).toHaveBeenCalledTimes(1);

    modal!.onDismiss();
    expect(onHidden).toHaveBeenCalledTimes(1);

    expect(getByText('Sheet title').props.accessibilityRole).toBe('header');
  });

  it('renders nothing when not visible', async () => {
    const { queryByText } = await setup({ visible: false });
    expect(queryByText('body content')).toBeNull();
  });
});
