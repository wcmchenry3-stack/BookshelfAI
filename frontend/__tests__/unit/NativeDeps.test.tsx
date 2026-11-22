/**
 * #901 installs expo-image, expo-haptics, react-native-reanimated (with
 * react-native-worklets) and react-native-gesture-handler for later cover,
 * haptic and motion work. These smoke tests check that expo-haptics, expo-image
 * and the gesture root load and work under Jest with no hand-written mock.
 *
 * The expo-haptics case also exists so knip sees expo-haptics as used: nothing
 * in the app imports it yet. Delete that case once the first real
 * `import ... from 'expo-haptics'` lands in app code.
 *
 * Reanimated is not imported here: importing it under Jest needs its official
 * Jest setup (see docs/claude/testing.md), which the first issue that animates
 * something adds. Its Babel plugin is covered by BabelWorklets.test.ts.
 */
import React from 'react';
import { Text } from 'react-native';
import { render } from '@testing-library/react-native';
import * as Haptics from 'expo-haptics';
import { Image } from 'expo-image';
import { GestureHandlerRootView } from 'react-native-gesture-handler';

describe('native dependencies (#901)', () => {
  it('expo-haptics calls resolve under Jest', async () => {
    await expect(Haptics.selectionAsync()).resolves.toBeUndefined();
    await expect(Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light)).resolves.toBeUndefined();
    await expect(
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success)
    ).resolves.toBeUndefined();
  });

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
