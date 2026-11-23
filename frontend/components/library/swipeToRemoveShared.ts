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
   * Removes the item and resolves true once it is removed (the row then
   * unmounts). It owns its own error alert; on false the row springs back.
   */
  onRemove: () => Promise<boolean>;
  /** Spoken label of the revealed Remove button, e.g. "Remove Dune from wishlist". */
  label: string;
  /** Visible text of the panel, e.g. "Remove". */
  actionText: string;
  /**
   * Turns the gesture and the panel off (offline, or a change to the item in
   * flight) and closes an open row, unless it is this row's own removal.
   */
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
