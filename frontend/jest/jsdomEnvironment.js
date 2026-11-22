'use strict';

const { TestEnvironment: JsdomEnvironment } = require('jest-environment-jsdom');

/**
 * jsdom twin of jest-expo's `nativeEnvironment.js`. Since Jest 30.4,
 * `Runtime.resetModules()` runs `moduleMocker.clearMocksOnScope(global)` during
 * teardown, which reads every enumerable global. Expo installs lazy getters
 * (`TextEncoderStream`, `fetch`, ...) that `require()` on first access, and doing
 * that between tests throws. Skip accessor properties so they are never evaluated.
 */
function clearMocksOnScope(scope) {
  for (const key of Object.keys(scope)) {
    const descriptor = Object.getOwnPropertyDescriptor(scope, key);
    if (!descriptor || typeof descriptor.get === 'function') continue;
    const value = descriptor.value;
    if (
      value != null &&
      (typeof value === 'object' || typeof value === 'function') &&
      '_isMockFunction' in value &&
      this.isMockFunction(value) &&
      typeof value.mockClear === 'function'
    ) {
      value.mockClear();
    }
  }
}

class JsdomExpoEnvironment extends JsdomEnvironment {
  constructor(config, context) {
    super(config, context);
    this.moduleMocker.clearMocksOnScope = clearMocksOnScope;
  }
}

module.exports = JsdomExpoEnvironment;
module.exports.TestEnvironment = JsdomExpoEnvironment;
