import React from 'react';
import { ActivityIndicator } from 'react-native';
import TestRenderer, { act } from 'react-test-renderer';

import { ScanProgressPanel } from '../../components/scan/ScanProgressPanel';

jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
jest.mock('expo-router', () => ({
  ...require('../helpers/routerMock').routerModule(),
  router: { back: jest.fn(), replace: jest.fn(), canGoBack: () => true },
}));

// #797: react-native-web renders the ActivityIndicator as a nameless
// role=progressbar (axe aria-progressbar-name). The labelled row is the
// progressbar, so the spinner inside it must be hidden from assistive tech.
describe('ScanProgressPanel accessibility', () => {
  it('names the progress row and hides its spinner', async () => {
    let renderer!: TestRenderer.ReactTestRenderer;
    await act(async () => {
      renderer = TestRenderer.create(
        <ScanProgressPanel
          waitingCount={0}
          failedCount={0}
          inProgress
          progressText="Reading your books…"
          isConnected
          activeColor="#000"
          onSendPending={jest.fn()}
          usage={null}
          notice={null}
          onDismissNotice={jest.fn()}
          onRetryNotice={jest.fn()}
        />
      );
    });
    const row = renderer.root.findByProps({ testID: 'scan-progress' });
    expect(row.props.accessibilityRole).toBe('progressbar');
    expect(row.props.accessibilityLabel).toBe('Reading your books…');
    expect(renderer.root.findByType(ActivityIndicator).props['aria-hidden']).toBe(true);
  });
});
