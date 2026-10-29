import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { WebView, type WebViewMessageEvent } from 'react-native-webview';
import type { ShouldStartLoadRequest } from 'react-native-webview/lib/WebViewTypes';

import { Sentry } from '../lib/sentry';
import { originOf } from '../lib/turnstile/config';
import {
  isAllowedTurnstileNavigation,
  TURNSTILE_ORIGIN_WHITELIST,
} from '../lib/turnstile/navigationGuard';
import type { TurnstileTokenBroker } from '../lib/turnstile/tokenBroker';
import {
  buildTurnstileHtml,
  PAGE_FAILURE_CODES,
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
 * Waits before remounting a page that failed (Turnstile's script did not load,
 * the widget did not render, or requests time out with the page not ready),
 * by consecutive failure; the last step repeats. Reset once a page is ready.
 */
export const TURNSTILE_REMOUNT_DELAYS_MS: readonly number[] = [5_000, 30_000, 120_000];

/**
 * iOS / Android (#740): one hidden WebView holding an invisible Turnstile
 * widget. Mounted once (TurnstileProvider); the broker asks it for a fresh
 * challenge per /scan attempt and it posts the token back. Locked down: only
 * its inline page, Turnstile's origin and blank frames load; no file access,
 * no new windows, nothing focusable or visible.
 *
 * A broken page (offline launch, a failed script load, a dead web content
 * process) is replaced by remounting the WebView (a new `key`), with backoff,
 * never by `reload()`: after a render-process loss that instance is dead, and
 * on iOS `reload()` can load the base URL instead of the inline page.
 */
export function TurnstileWidget({ broker, siteKey, baseUrl }: TurnstileWidgetProps) {
  const ref = useRef<WebView>(null);
  const html = useMemo(() => buildTurnstileHtml(siteKey), [siteKey]);
  // Bumped to remount the WebView with a fresh page.
  const [instance, setInstance] = useState(0);
  const failuresRef = useRef(0);
  const remountTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const remountNow = useCallback(() => {
    if (remountTimerRef.current) clearTimeout(remountTimerRef.current);
    remountTimerRef.current = null;
    broker.setReady(false);
    setInstance((n) => n + 1);
  }, [broker]);

  // Remount after the backoff for this failure, unless one is already due.
  const scheduleRemount = useCallback(
    (reason: string) => {
      broker.setReady(false);
      if (remountTimerRef.current) return;
      const step = Math.min(failuresRef.current, TURNSTILE_REMOUNT_DELAYS_MS.length - 1);
      failuresRef.current += 1;
      Sentry.addBreadcrumb({
        category: 'turnstile',
        message: `Turnstile page failed (${reason}); remounting`,
        level: 'warning',
      });
      remountTimerRef.current = setTimeout(remountNow, TURNSTILE_REMOUNT_DELAYS_MS[step]);
    },
    [broker, remountNow]
  );

  useEffect(
    () => () => {
      if (remountTimerRef.current) clearTimeout(remountTimerRef.current);
    },
    []
  );

  useEffect(() => {
    const detach = broker.attach({
      start: (id) => ref.current?.injectJavaScript(runChallengeScript(id)),
      reset: () => ref.current?.injectJavaScript(resetChallengeScript()),
      stalled: () => scheduleRemount('stalled'),
    });
    return detach;
  }, [broker, scheduleRemount]);

  const onMessage = useCallback(
    (event: WebViewMessageEvent) => {
      // Only the top-level inline page talks to us; iOS and Android both
      // report the URL of the frame that posted. No URL, or another origin
      // (e.g. a Turnstile frame): ignored.
      const from = event.nativeEvent.url;
      if (!baseUrl || !from || originOf(from) !== baseUrl) return;
      const msg = parseTurnstileMessage(event.nativeEvent.data);
      if (!msg) return;
      if (msg.type === 'ready') {
        failuresRef.current = 0;
        broker.setReady(true);
      } else if (msg.type === 'token') {
        broker.deliver(msg.id, msg.token);
      } else if (PAGE_FAILURE_CODES.includes(msg.code)) {
        // The page is broken: answer every waiting request now (no token),
        // then replace the page.
        scheduleRemount(msg.code);
        broker.failAll();
      } else if (msg.id === null) {
        broker.failCurrent();
      } else {
        broker.fail(msg.id);
      }
    },
    [broker, baseUrl, scheduleRemount]
  );

  const onShouldStartLoadWithRequest = useCallback(
    (request: ShouldStartLoadRequest) =>
      !!baseUrl && isAllowedTurnstileNavigation(request.url, baseUrl),
    [baseUrl]
  );

  // The web content process died (memory pressure): this instance is gone,
  // so mount a new one now. A challenge in flight re-runs once it is ready.
  const onProcessGone = useCallback(() => remountNow(), [remountNow]);

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
        key={instance}
        ref={ref}
        testID="turnstile-webview"
        source={{ html, baseUrl }}
        originWhitelist={TURNSTILE_ORIGIN_WHITELIST as string[]}
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
