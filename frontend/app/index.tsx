import { ActivityIndicator, View } from 'react-native';
import { Redirect } from 'expo-router';

import { useAuth } from '../hooks/useAuth';
import { DEFAULT_TAB_ROUTE } from '../lib/defaultTab';

export default function Index() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center' }}>
        <ActivityIndicator />
      </View>
    );
  }

  if (isAuthenticated) {
    return <Redirect href={DEFAULT_TAB_ROUTE} />;
  }

  return <Redirect href="/(auth)/login" />;
}
