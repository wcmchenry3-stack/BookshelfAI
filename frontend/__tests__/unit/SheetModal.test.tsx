import React from 'react';
import { ScrollView, Text } from 'react-native';
import { fireEvent, render, within } from '@testing-library/react-native';

import SheetModal from '../../components/SheetModal';
import { textVariants } from '../../theme/typography';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        border: '#ccc',
        text: '#000',
        primary: '#007AFF',
        outline: '#E4DFD4',
        surfaceBar: '#FBFAF7',
        scrim: '#0000008c',
      },
      radius: { button: 12 },
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
      {overrides.children ?? <Text>body content</Text>}
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

  it('renders the grabber and a subtitle when given', async () => {
    const { getByTestId, getByText } = await setup({ subtitle: 'Sheet subtitle' });
    expect(getByTestId('sheet-grabber', { includeHiddenElements: true })).toBeTruthy();
    expect(getByText('Sheet subtitle')).toBeTruthy();
  });

  it('omits the subtitle line when none is given', async () => {
    const { queryByText } = await setup();
    expect(queryByText('Sheet subtitle')).toBeNull();
  });

  it('sets the title in the serif sheetTitle font file, never fontWeight', async () => {
    const { getByText } = await setup();
    const flat = Object.assign({}, ...[getByText('Sheet title').props.style].flat(3));
    expect(flat.fontFamily).toBe(textVariants.sheetTitle.fontFamily);
    expect(flat.fontSize).toBe(24);
    expect(flat.fontWeight).toBeUndefined();
  });

  it('close action is at least 44pt and announces its a11y label', async () => {
    const { getByLabelText } = await setup();
    const close = getByLabelText('Close sheet');
    expect(close.props.accessibilityRole).toBe('button');
    const flat = Object.assign({}, ...[close.props.style].flat(3));
    expect(flat.minHeight).toBeGreaterThanOrEqual(44);
  });

  it('pins the footer outside the body so it stays visible while the body scrolls', async () => {
    const { getByTestId, getByText } = await setup({
      children: (
        <ScrollView testID="scroller">
          <Text>long body</Text>
        </ScrollView>
      ),
    });
    const body = getByTestId('sheet-body');
    const footer = getByTestId('sheet-footer');
    expect(within(footer).getByText('footer content')).toBeTruthy();
    expect(within(body).queryByText('footer content')).toBeNull();
    expect(within(body).getByTestId('scroller')).toBeTruthy();
    expect(getByText('long body')).toBeTruthy();
    const flat = (n: { props: { style: unknown } }) =>
      Object.assign({}, ...[n.props.style].flat(3).filter(Boolean));
    expect(flat(body).flexShrink).toBe(1);
    expect(flat(footer).borderTopWidth).toBeGreaterThan(0);
    expect(flat(footer).backgroundColor).toBe('#FBFAF7');
  });

  it('uses the scrim token behind the sheet', async () => {
    const { getByTestId } = await setup();
    const flat = Object.assign({}, ...[getByTestId('sheet-scrim').props.style].flat(3));
    expect(flat.backgroundColor).toBe('#0000008c');
  });

  it('renders nothing when not visible', async () => {
    const { queryByText } = await setup({ visible: false });
    expect(queryByText('body content')).toBeNull();
  });
});
