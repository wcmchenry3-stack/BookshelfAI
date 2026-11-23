import { useEffect, useState } from 'react';
import { AccessibilityInfo, Platform } from 'react-native';

/**
 * Whether a screen reader (VoiceOver, TalkBack) is running, kept up to date by
 * its change event. Always false on web: react-native-web reports a screen
 * reader as on everywhere, so web never queries or subscribes.
 */
export function useScreenReaderEnabled(): boolean {
  const [screenReaderOn, setScreenReaderOn] = useState(false);

  useEffect(() => {
    if (Platform.OS === 'web') return;
    let alive = true;
    let sawEvent = false;
    AccessibilityInfo.isScreenReaderEnabled()
      // A late query result must not overwrite a newer change event.
      .then((on) => alive && !sawEvent && setScreenReaderOn(on))
      .catch(() => {});
    const sub = AccessibilityInfo.addEventListener('screenReaderChanged', (on) => {
      sawEvent = true;
      setScreenReaderOn(on);
    });
    return () => {
      alive = false;
      sub.remove();
    };
  }, []);

  return screenReaderOn;
}
