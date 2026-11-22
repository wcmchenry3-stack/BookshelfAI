// The real SDK is used here (no module mock): its default integrations are
// checked against our navigation-tracing filter (#715).
import * as Sentry from '@sentry/react-native';
import { getDefaultIntegrations } from '@sentry/react-native/dist/js/integrations/default';
import { sentryIntegrations } from '../../lib/sentry';

describe('real SDK default integrations (#715)', () => {
  it('the SDK adds the expo router integration, and our filter removes only it', () => {
    const name = Sentry.expoRouterIntegration().name;
    const defaults = getDefaultIntegrations({
      tracesSampleRate: 1,
      enableAutoPerformanceTracing: true,
      enableNative: false,
    } as never);
    expect(defaults.map((i) => i.name)).toContain(name);

    const kept = sentryIntegrations(defaults);
    expect(kept.map((i) => i.name)).not.toContain(name);
    expect(kept).toHaveLength(defaults.length - 1);
  });
});
