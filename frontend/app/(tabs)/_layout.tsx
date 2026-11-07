import { ColorValue, StyleSheet, View } from 'react-native';
import { Tabs } from 'expo-router';
import { MaterialIcons } from '@expo/vector-icons';
import { useTranslation } from 'react-i18next';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { AppText } from '../../components/ui/AppText';
import { useTheme } from '../../hooks/useTheme';
import { CHROME_MAX_FONT_SCALE, textVariants } from '../../theme/typography';

type MaterialIconName = React.ComponentProps<typeof MaterialIcons>['name'];

// Active indicator: a pill behind the icon only (#910).
const PILL_WIDTH = 56;
const PILL_HEIGHT = 30;
const ICON_SIZE = 24;
const BAR_PADDING_TOP = 8;
const ICON_LABEL_GAP = 4;
const LABEL_PADDING_BOTTOM = 6;
// The label's line height at the largest font scale it can reach, so it never clips.
const LABEL_MAX_LINE = Math.ceil(textVariants.tabLabel.lineHeight * CHROME_MAX_FONT_SCALE);
const BAR_CONTENT_HEIGHT =
  BAR_PADDING_TOP + PILL_HEIGHT + ICON_LABEL_GAP + LABEL_MAX_LINE + LABEL_PADDING_BOTTOM;

interface TabIconProps {
  name: MaterialIconName;
  focused: boolean;
  color: ColorValue;
}

function TabIcon({ name, focused, color }: TabIconProps) {
  const { theme } = useTheme();
  return (
    <View
      testID={focused ? `tab-pill-${name}` : undefined}
      style={[styles.pill, { backgroundColor: focused ? theme.colors.primarySoft : 'transparent' }]}
    >
      <MaterialIcons name={name} size={ICON_SIZE} color={color as string} />
    </View>
  );
}

interface TabLabelProps {
  focused: boolean;
  color: ColorValue;
  children: string;
}

function TabLabel({ focused, color, children }: TabLabelProps) {
  return (
    <AppText
      variant={focused ? 'tabLabelActive' : 'tabLabel'}
      numberOfLines={1}
      style={[styles.label, { color }]}
    >
      {children}
    </AppText>
  );
}

export default function TabLayout() {
  const { theme } = useTheme();
  const { t } = useTranslation('tabs');
  const insets = useSafeAreaInsets();

  // Height from the content plus the bottom safe-area inset, never a fixed number.
  const bottomInset = Math.max(insets.bottom, 0);
  const tabBarStyle = StyleSheet.flatten([
    styles.tabBar,
    {
      backgroundColor: theme.colors.surfaceBar,
      borderTopWidth: StyleSheet.hairlineWidth,
      borderTopColor: theme.colors.outline,
      height: BAR_CONTENT_HEIGHT + bottomInset,
      paddingBottom: LABEL_PADDING_BOTTOM + bottomInset,
    },
  ]);

  return (
    <Tabs
      screenOptions={() => ({
        headerShown: false,
        tabBarStyle,
        tabBarShowLabel: true,
        tabBarLabelPosition: 'below-icon',
        tabBarLabel: ({ focused, color, children }) => (
          <TabLabel focused={focused} color={color}>
            {children}
          </TabLabel>
        ),
        tabBarActiveTintColor: theme.colors.primary,
        tabBarInactiveTintColor: theme.colors.onSurfaceVariant,
        tabBarItemStyle: styles.tabItem,
        tabBarIconStyle: styles.iconSlot,
        tabBarActiveBackgroundColor: 'transparent',
        tabBarInactiveBackgroundColor: 'transparent',
      })}
    >
      <Tabs.Screen
        name="scan"
        options={{
          title: t('scan'),
          tabBarAccessibilityLabel: t('scanA11y'),
          tabBarIcon: ({ focused, color }) => (
            <TabIcon name="document-scanner" focused={focused} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="wishlist"
        options={{
          title: t('wishlist'),
          tabBarAccessibilityLabel: t('wishlistA11y'),
          tabBarIcon: ({ focused, color }) => (
            <TabIcon name="auto-stories" focused={focused} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="my-books"
        options={{
          title: t('myBooks'),
          tabBarAccessibilityLabel: t('myBooksA11y'),
          tabBarIcon: ({ focused, color }) => (
            <TabIcon name="library-books" focused={focused} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: t('settings'),
          tabBarAccessibilityLabel: t('settingsA11y'),
          tabBarIcon: ({ focused, color }) => (
            <TabIcon name="settings" focused={focused} color={color} />
          ),
        }}
      />
    </Tabs>
  );
}

const styles = StyleSheet.create({
  tabBar: {
    elevation: 0,
    paddingTop: BAR_PADDING_TOP,
    paddingHorizontal: 8,
  },
  tabItem: {
    minHeight: 44,
    paddingVertical: 0,
  },
  iconSlot: {
    width: PILL_WIDTH,
    height: PILL_HEIGHT,
  },
  pill: {
    width: PILL_WIDTH,
    height: PILL_HEIGHT,
    borderRadius: PILL_HEIGHT / 2,
    alignItems: 'center',
    justifyContent: 'center',
  },
  label: {
    marginTop: ICON_LABEL_GAP,
  },
});
