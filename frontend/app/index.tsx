import { ActivityIndicator, View } from 'react-native';
import { Redirect } from 'expo-router';

import { useAuth } from '../hooks/useAuth';
import { defaultTabRoute } from '../lib/defaultTab';

export default function Index() {
  const { isAuthenticated, isLoading, defaultTab } = useAuth();

  if (isLoading) {
    return (
      <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center' }}>
        <ActivityIndicator />
      </View>
    );
  }

  if (isAuthenticated) {
    // The user's "Open on" tab (#913), read by the launch session check;
    // Scan when unset, unknown or not answered.
    return <Redirect href={defaultTabRoute(defaultTab)} />;
  }

  return <Redirect href="/(auth)/login" />;
}
