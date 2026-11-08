import { Platform, RefreshControl, View } from 'react-native';

import { LibraryRowSeparator } from './LibraryRow';
import { GRID_ROW_GAP } from './libraryShared';

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

/** The 20px gap between two rows of the cover grid (#934); no hairline in the grid. */
function GridRowSeparator() {
  return <View style={{ height: GRID_ROW_GAP }} />;
}

/**
 * FlatList props of the cover grid (#934). Items are rows of cells, so 6 rows is
 * 18+ books at phone width: more than the first screen. Each later batch adds 6 rows.
 */
export const LIBRARY_GRID_PROPS = {
  keyExtractor: LIBRARY_LIST_PROPS.keyExtractor,
  ItemSeparatorComponent: GridRowSeparator,
  initialNumToRender: 6,
  maxToRenderPerBatch: 6,
  removeClippedSubviews: LIBRARY_LIST_PROPS.removeClippedSubviews,
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
