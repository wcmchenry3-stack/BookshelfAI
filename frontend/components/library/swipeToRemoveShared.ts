import { useMemo, useRef, type ReactNode } from 'react';

/** What a list needs to close a swiped-open row. */
export interface SwipeRowHandle {
  close: () => void;
}

/**
 * Keeps at most one row of a list open (#935). Rows report when they open and
 * close; opening a row closes the one that was open, and the list calls
 * `closeOpen` when it starts scrolling.
 */
export interface SwipeRowGroup {
  onOpen: (row: SwipeRowHandle) => void;
  onClose: (row: SwipeRowHandle) => void;
  closeOpen: () => void;
}

export interface SwipeToRemoveProps {
  /** The row. */
  children: ReactNode;
  /**
   * Removes the item. It owns its own error alert; once it settles the row
   * closes, so a failed removal springs back (a removed row unmounts).
   */
  onRemove: () => unknown;
  /** Spoken label of the revealed Remove button, e.g. "Remove Dune from wishlist". */
  label: string;
  /** Visible text of the panel, e.g. "Remove". */
  actionText: string;
  /** Turns the gesture off (offline) and closes an open row. */
  disabled?: boolean;
  /** The list's one-open-row group (`useSingleOpenRow`). */
  group?: SwipeRowGroup;
}

/** A stable `SwipeRowGroup` for one list. */
export function useSingleOpenRow(): SwipeRowGroup {
  const openRef = useRef<SwipeRowHandle | null>(null);
  return useMemo(
    () => ({
      onOpen(row) {
        if (openRef.current && openRef.current !== row) openRef.current.close();
        openRef.current = row;
      },
      onClose(row) {
        if (openRef.current === row) openRef.current = null;
      },
      closeOpen() {
        openRef.current?.close();
        openRef.current = null;
      },
    }),
    []
  );
}
