/**
 * jest.setup.ts makes gesture-handler's root-view context default to "inside a
 * root" so screen tests render without the app root (#935). This file puts the
 * real context back to prove the override hides nothing: a swipeable row still
 * needs a `GestureHandlerRootView` above it, which `GestureRoot` provides in
 * the app.
 */
import React from 'react';
import { Text } from 'react-native';
import { render } from '@testing-library/react-native';
import { GestureHandlerRootView } from 'react-native-gesture-handler';

import { SwipeToRemove } from '../../components/library/SwipeToRemove';

jest.unmock('react-native-gesture-handler/src/GestureHandlerRootViewContext');
jest.unmock('react-native-gesture-handler/lib/module/GestureHandlerRootViewContext');
jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

function Row() {
  return (
    <SwipeToRemove onRemove={jest.fn()} label="Remove Dune from wishlist" actionText="Remove">
      <Text>Dune</Text>
    </SwipeToRemove>
  );
}

describe('SwipeToRemove and the gesture-handler root (#935)', () => {
  it('throws outside a GestureHandlerRootView', async () => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    try {
      await expect(render(<Row />)).rejects.toThrow(
        'GestureDetector must be used as a descendant of GestureHandlerRootView'
      );
    } finally {
      consoleError.mockRestore();
    }
  });

  it('renders inside one', async () => {
    const screen = await render(
      <GestureHandlerRootView>
        <Row />
      </GestureHandlerRootView>
    );
    expect(screen.getByText('Dune')).toBeTruthy();
    expect(screen.getByTestId('swipe-to-remove')).toBeTruthy();
  });
});
