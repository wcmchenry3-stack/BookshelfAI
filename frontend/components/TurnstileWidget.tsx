import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import { StyleSheet, View } from 'react-native';
import { WebView, type WebViewMessageEvent } from 'react-native-webview';
import type { ShouldStartLoadRequest } from 'react-native-webview/lib/WebViewTypes';

import { originOf } from '../lib/turnstile/config';
import {
  isAllowedTurnstileNavigation,
  turnstileOriginWhitelist,
} from '../lib/turnstile/navigationGuard';
import type { TurnstileTokenBroker } from '../lib/turnstile/tokenBroker';
import {
  buildTurnstileHtml,
  parseTurnstileMessage,
  resetChallengeScript,
  runChallengeScript,
} from '../lib/turnstile/widgetHtml';

export interface TurnstileWidgetProps {
  broker: TurnstileTokenBroker;
  siteKey: string;
  /** Native only: the https origin the inline page is loaded under. */
  baseUrl: string | null;
}

/**
 * iOS / Android (#740): one hidden WebView holding an invisible Turnstile
 * widget. Mounted once (TurnstileProvider); the broker asks it for a fresh
 * challenge per /scan attempt and it posts the token back. Locked down: only
 * its inline page, Turnstile's origin and blank frames load; no file access,
 * no new windows, nothing focusable or visible.
 */
export function TurnstileWidget({ broker, siteKey, baseUrl }: TurnstileWidgetProps) {
  const ref = useRef<WebView>(null);
  const html = useMemo(() => buildTurnstileHtml(siteKey), [siteKey]);

  useEffect(() => {
    const detach = broker.attach({
      start: (id) => ref.current?.injectJavaScript(runChallengeScript(id)),
      reset: () => ref.current?.injectJavaScript(resetChallengeScript()),
    });
    return detach;
  }, [broker]);

  const onMessage = useCallback(
    (event: WebViewMessageEvent) => {
      // Only the top-level inline page talks to us (iOS reports its URL).
      const from = event.nativeEvent.url;
      if (baseUrl && from && originOf(from) !== baseUrl) return;
      const msg = parseTurnstileMessage(event.nativeEvent.data);
      if (!msg) return;
      if (msg.type === 'ready') broker.setReady(true);
      else if (msg.type === 'token') broker.deliver(msg.id, msg.token);
      else if (msg.id === null) broker.failCurrent();
      else broker.fail(msg.id);
    },
    [broker, baseUrl]
  );

  const onShouldStartLoadWithRequest = useCallback(
    (request: ShouldStartLoadRequest) =>
      !!baseUrl && isAllowedTurnstileNavigation(request.url, baseUrl),
    [baseUrl]
  );

  // The web content process died (memory pressure): the page reloads and
  // posts `ready` again; a challenge in flight is re-run then.
  const onProcessGone = useCallback(() => {
    broker.setReady(false);
    ref.current?.reload();
  }, [broker]);

  if (!baseUrl) return null;

  return (
    <View
      style={styles.hidden}
      pointerEvents="none"
      accessible={false}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
    >
      <WebView
        ref={ref}
        testID="turnstile-webview"
        source={{ html, baseUrl }}
        originWhitelist={turnstileOriginWhitelist(baseUrl)}
        onShouldStartLoadWithRequest={onShouldStartLoadWithRequest}
        onMessage={onMessage}
        onLoadStart={() => broker.setReady(false)}
        onContentProcessDidTerminate={onProcessGone}
        onRenderProcessGone={onProcessGone}
        javaScriptEnabled
        javaScriptCanOpenWindowsAutomatically={false}
        setSupportMultipleWindows={false}
        allowFileAccess={false}
        allowFileAccessFromFileURLs={false}
        allowUniversalAccessFromFileURLs={false}
        allowsLinkPreview={false}
        allowsBackForwardNavigationGestures={false}
        mixedContentMode="never"
        geolocationEnabled={false}
        mediaPlaybackRequiresUserAction
        allowsInlineMediaPlayback={false}
        webviewDebuggingEnabled={false}
        scrollEnabled={false}
        focusable={false}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  // Tiny and transparent rather than unmounted or display:none, so the page
  // keeps running; off to the side and ignored by touch and screen readers.
  hidden: {
    position: 'absolute',
    width: 1,
    height: 1,
    left: -10,
    top: -10,
    opacity: 0,
    overflow: 'hidden',
  },
});
