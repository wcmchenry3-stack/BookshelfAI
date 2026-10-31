import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Sentry } from '../lib/sentry';
import { ThemeContext } from '../theme/ThemeContext';
import { lightTheme, type Theme } from '../theme/light';

/**
 * Theme access for a class component.
 *
 * A class cannot call useTheme(), so the fallback UI reads the theme through
 * ThemeContext.Consumer. In app/_layout.tsx the boundary is mounted ABOVE
 * ThemeProvider, so it normally receives ThemeContext's default value
 * (lightTheme). If the context value is missing or malformed (provider failed
 * or supplied null), we fall back to lightTheme directly so the error screen
 * can never itself crash. All error-surface colours are the fixed
 * `errorBoundary*` tokens, identical in light and dark.
 */
function resolveTheme(value: { theme?: Theme } | null | undefined): Theme {
  return value?.theme?.colors ? value.theme : lightTheme;
}

interface Props {
  children: React.ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[ErrorBoundary] Unhandled error:', error, info.componentStack);
    Sentry.captureException(error, { extra: { componentStack: info.componentStack } });
  }

  render() {
    if (this.state.hasError) {
      return (
        <ThemeContext.Consumer>
          {(value) => {
            const c = resolveTheme(value).colors;
            return (
              <View style={[styles.container, { backgroundColor: c.errorBoundarySurface }]}>
                <Text style={[styles.title, { color: c.errorBoundaryTitle }]}>
                  Something went wrong
                </Text>
                <Text style={[styles.body, { color: c.errorBoundaryBody }]}>
                  The app ran into an unexpected error.
                </Text>
                {__DEV__ && this.state.error && (
                  <Text
                    style={[
                      styles.errorDetail,
                      {
                        color: c.errorBoundaryDetail,
                        backgroundColor: c.errorBoundaryDetailSurface,
                      },
                    ]}
                    selectable
                  >
                    {this.state.error.name}: {this.state.error.message}
                    {'\n\n'}
                    {this.state.error.stack?.slice(0, 500)}
                  </Text>
                )}
                <Pressable
                  style={[styles.button, { backgroundColor: c.errorBoundaryAction }]}
                  onPress={() => this.setState({ hasError: false })}
                  accessibilityRole="button"
                  accessibilityLabel="Try again"
                >
                  <Text style={[styles.buttonText, { color: c.errorBoundaryOnAction }]}>
                    Try again
                  </Text>
                </Pressable>
              </View>
            );
          }}
        </ThemeContext.Consumer>
      );
    }

    return this.props.children;
  }
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 32,
  },
  title: {
    fontSize: 20,
    fontWeight: '600',
    marginBottom: 8,
    textAlign: 'center',
  },
  body: {
    fontSize: 15,
    textAlign: 'center',
    marginBottom: 24,
  },
  errorDetail: {
    fontSize: 12,
    fontFamily: 'Courier',
    textAlign: 'left',
    marginBottom: 16,
    padding: 12,
    borderRadius: 6,
    width: '100%',
  },
  button: {
    paddingHorizontal: 24,
    paddingVertical: 12,
    borderRadius: 8,
    minHeight: 44,
    justifyContent: 'center',
  },
  buttonText: {
    fontWeight: '600',
    fontSize: 16,
  },
});
