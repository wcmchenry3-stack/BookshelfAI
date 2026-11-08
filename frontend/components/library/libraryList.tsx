import { Platform, RefreshControl } from 'react-native';

import { LibraryRowSeparator } from './LibraryRow';

/**
 * FlatList props shared by the library lists (My Books, Wishlist): stable keys,
 * the inset hairline between rows, and the render-window tuning from #929.
 */
export const LIBRARY_LIST_PROPS = {
  keyExtractor: (item: { id: string }) => item.id,
  ItemSeparatorComponent: LibraryRowSeparator,
  initialNumToRender: 12,
  removeClippedSubviews: Platform.OS === 'android',
} as const;

/** Pull-to-refresh control for a library list: marks refreshing, then reloads. */
export function libraryRefreshControl(
  refreshing: boolean,
  setRefreshing: (value: boolean) => void,
  reload: () => Promise<unknown>,
  tint: string
) {
  const onRefresh = () => {
    setRefreshing(true);
    void reload();
  };
  return <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={tint} />;
}
