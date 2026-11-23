/**
 * #901 installs expo-image, expo-haptics, react-native-reanimated (with
 * react-native-worklets) and react-native-gesture-handler for later cover,
 * haptic and motion work. These smoke tests check that expo-image and the
 * gesture root load and work under Jest with no hand-written mock.
 *
 * The expo-haptics case was removed in #935, whose swipe-to-remove is the first
 * app code to import expo-haptics. Reanimated's Jest setup (also #935) is in
 * jest.config.js and jest.setup.ts; SwipeToRemove.test.tsx exercises it. Its
 * Babel plugin is covered by BabelWorklets.test.ts.
 */
import React from 'react';
import { Text } from 'react-native';
import { render } from '@testing-library/react-native';
import { Image } from 'expo-image';
import { GestureHandlerRootView } from 'react-native-gesture-handler';

describe('native dependencies (#901)', () => {
  it('expo-image renders an image under Jest', async () => {
    const { getByTestId } = await render(
      <Image testID="cover" source={{ uri: 'https://example.com/cover.jpg' }} />
    );
    expect(getByTestId('cover')).toBeTruthy();
  });

  it('gesture-handler root view renders its children', async () => {
    const { getByText } = await render(
      <GestureHandlerRootView>
        <Text>inside</Text>
      </GestureHandlerRootView>
    );
    expect(getByText('inside')).toBeTruthy();
  });
});
