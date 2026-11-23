/**
 * #901 installs expo-image, expo-haptics, react-native-reanimated (with
 * react-native-worklets) and react-native-gesture-handler for later cover,
 * haptic and motion work. This smoke test checks that the gesture root loads
 * and works under Jest with no hand-written mock.
 *
 * The expo-image case was removed in #1063 (BookCover draws covers with it) and
 * the expo-haptics case in #935, whose swipe-to-remove is the first app code to
 * import expo-haptics. Reanimated's Jest setup (also #935) is in jest.config.js
 * and jest.setup.ts; SwipeToRemove.test.tsx exercises it. Its Babel plugin is
 * covered by BabelWorklets.test.ts.
 */
import React from 'react';
import { Text } from 'react-native';
import { render } from '@testing-library/react-native';
import { GestureHandlerRootView } from 'react-native-gesture-handler';

describe('native dependencies (#901)', () => {
  it('gesture-handler root view renders its children', async () => {
    const { getByText } = await render(
      <GestureHandlerRootView>
        <Text>inside</Text>
      </GestureHandlerRootView>
    );
    expect(getByText('inside')).toBeTruthy();
  });
});
