/**
 * React Native's Jest setup (`@react-native/jest-preset/jest/setup.js`) runs
 * `Object.defineProperties(global, { ..., window: { value: global } })`. Under
 * the Node-based native environment that is harmless, but in the jsdom
 * environment `window` is a non-configurable property of the jsdom global, so
 * the call throws `TypeError: Cannot redefine property: window` (Jest 30 /
 * RN 0.88). In jsdom `window === global` already, so the only thing to skip is
 * that one property; everything else in the RN setup is applied as usual.
 */
const originalDefineProperties = Object.defineProperties;

Object.defineProperties = function defineProperties(target, props) {
  if (target === global && props && 'window' in props && global.window === global) {
    const { window: _skipped, ...rest } = props;
    return originalDefineProperties.call(this, target, rest);
  }
  return originalDefineProperties.call(this, target, props);
};

try {
  require('@react-native/jest-preset/jest/setup.js');
} finally {
  Object.defineProperties = originalDefineProperties;
}
