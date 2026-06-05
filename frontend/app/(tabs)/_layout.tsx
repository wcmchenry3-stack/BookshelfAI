import { ColorValue, Platform, StyleSheet } from 'react-native';
import { Tabs } from 'expo-router';
import { MaterialIcons } from '@expo/vector-icons';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';

type MaterialIconName = React.ComponentProps<typeof MaterialIcons>['name'];

interface TabIconProps {
  name: MaterialIconName;
  focused: boolean;
  color: ColorValue;
  size: number;
}

function TabIcon({ name, focused, color, size }: TabIconProps) {
  return <MaterialIcons name={name} size={size} color={color as string} />;
}

export default function TabLayout() {
  const { theme } = useTheme();
  const { t } = useTranslation('tabs');

  // In dark mode use the gold tertiary accent for active tabs; primary in light mode.
  const activeTabBg = theme.isDark ? theme.colors.tertiary : theme.colors.primary;
  const activeTabTint = theme.isDark ? theme.colors.onTertiary : theme.colors.onPrimary;

  // Dark mode: frosted-glass bottom nav with rounded top and subtle border.
  const tabBarBg = theme.isDark ? 'rgba(17,19,23,0.85)' : theme.colors.surfaceContainerLow;
  const tabBarStyle = StyleSheet.flatten([
    styles.tabBar,
    {
      backgroundColor: tabBarBg,
      borderTopWidth: StyleSheet.hairlineWidth,
      borderTopColor: theme.isDark
        ? 'rgba(69,71,76,0.4)' // outlineVariant at ~40% opacity
        : 'transparent',
      ...(theme.isDark
        ? {
            shadowColor: '#000',
            shadowOffset: { width: 0, height: -4 },
            shadowOpacity: 0.3,
            shadowRadius: 12,
            elevation: 20,
          }
        : {}),
    },
  ]);

  return (
    <Tabs
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarStyle,
        tabBarShowLabel: true,
        tabBarLabelStyle: styles.label,
        tabBarActiveTintColor: activeTabTint,
        tabBarInactiveTintColor: theme.colors.secondary,
        tabBarItemStyle: styles.tabItem,
        tabBarButton: undefined,
        // Active tab gets a filled pill background; gold in dark mode, primary in light.
        tabBarActiveBackgroundColor: activeTabBg,
        tabBarInactiveBackgroundColor: 'transparent',
      })}
    >
      <Tabs.Screen
        name="scan"
        options={{
          title: t('scan'),
          tabBarAccessibilityLabel: t('scanA11y'),
          tabBarIcon: ({ focused, color, size }) => (
            <TabIcon name="document-scanner" focused={focused} color={color} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="wishlist"
        options={{
          title: t('wishlist'),
          tabBarAccessibilityLabel: t('wishlistA11y'),
          tabBarIcon: ({ focused, color, size }) => (
            <TabIcon name="auto-stories" focused={focused} color={color} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="my-books"
        options={{
          title: t('myBooks'),
          tabBarAccessibilityLabel: t('myBooksA11y'),
          tabBarIcon: ({ focused, color, size }) => (
            <TabIcon name="library-books" focused={focused} color={color} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: t('settings'),
          tabBarAccessibilityLabel: t('settingsA11y'),
          tabBarIcon: ({ focused, color, size }) => (
            <TabIcon name="settings" focused={focused} color={color} size={size} />
          ),
        }}
      />
    </Tabs>
  );
}

const styles = StyleSheet.create({
  tabBar: {
    borderTopWidth: 0,
    elevation: 0,
    paddingHorizontal: 8,
    paddingBottom: Platform.OS === 'ios' ? 16 : 8,
    paddingTop: 8,
    height: Platform.OS === 'ios' ? 80 : 64,
    borderTopLeftRadius: 16,
    borderTopRightRadius: 16,
  },
  tabItem: {
    borderRadius: 12,
    marginHorizontal: 4,
    paddingVertical: 4,
  },
  label: {
    fontSize: 10,
    fontWeight: '500',
    letterSpacing: 0.8,
    textTransform: 'uppercase',
    marginBottom: 2,
  },
});
