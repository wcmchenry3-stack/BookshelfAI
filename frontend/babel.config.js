module.exports = function (api) {
  api.cache(true);
  return {
    // babel-preset-expo (SDK 58) adds `react-native-worklets/plugin`, the Babel
    // plugin Reanimated 4 needs, by itself whenever react-native-worklets is
    // installed (#901). Do not list it in `plugins` too: it would run twice.
    // __tests__/unit/BabelWorklets.test.ts checks that worklets are transformed.
    presets: ['babel-preset-expo'],
  };
};
