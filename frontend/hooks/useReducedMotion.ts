import { useSyncExternalStore } from 'react';
import { AccessibilityInfo } from 'react-native';

/**
 * The OS "Reduce Motion" setting (#1063). One shared subscription serves every
 * caller (a library list can mount dozens of covers), so the hook adds no
 * per-row listener. It reads `false` until the first query answers.
 */
let reduced = false;
let started = false;
const listeners = new Set<() => void>();

function set(value: boolean) {
  if (value === reduced) return;
  reduced = value;
  listeners.forEach((l) => l());
}

function start() {
  if (started) return;
  started = true;
  let sawEvent = false;
  AccessibilityInfo.isReduceMotionEnabled()
    // A late query result must not overwrite a newer change event.
    .then((on) => !sawEvent && set(on))
    .catch(() => {});
  AccessibilityInfo.addEventListener('reduceMotionChanged', (on) => {
    sawEvent = true;
    set(on);
  });
}

function subscribe(listener: () => void) {
  start();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const getSnapshot = () => reduced;

export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
