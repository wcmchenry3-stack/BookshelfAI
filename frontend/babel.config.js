module.exports = function (api) {
  api.cache(true);
  return {
    // babel-preset-expo (SDK 58) already adds `react-native-worklets/plugin`, the
    // Babel plugin Reanimated 4 needs, whenever react-native-worklets is installed
    // (#901), so listing it (or `react-native-reanimated/plugin`, which re-exports
    // it in 4.7.1) in `plugins` is redundant. __tests__/unit/BabelWorklets.test.ts
    // checks that the preset is where it comes from.
    presets: ['babel-preset-expo'],
  };
};
