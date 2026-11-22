import { TonalChip } from './TonalChip';

interface BoughtChipProps {
  label: string;
  accessibilityLabel: string;
  onPress: () => void;
  disabled?: boolean;
  testID?: string;
}

/** The tonal "Bought" chip on a Wishlist row: a `TonalChip` with a check icon. */
export function BoughtChip(props: BoughtChipProps) {
  return <TonalChip {...props} icon="check" />;
}
