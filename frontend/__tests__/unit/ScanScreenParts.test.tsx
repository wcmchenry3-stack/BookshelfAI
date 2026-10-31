// Render tests for the components split out of the Scan screen (#811).
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react-native';

import { ScanModeToggle } from '../../components/scan/ScanModeToggle';
import { ScanProgressPanel } from '../../components/scan/ScanProgressPanel';

const mockOpenScanQueue = jest.fn();
jest.mock('../../lib/scanQueueNav', () => ({ openScanQueue: () => mockOpenScanQueue() }));
jest.mock('../../components/ScanStatusPanel', () => ({
  ScanStatusPanel: () => null,
}));

describe('ScanModeToggle', () => {
  it('marks the active tab selected and reports taps', async () => {
    const onChange = jest.fn();
    await render(
      <ScanModeToggle
        inputMode="camera"
        onChange={onChange}
        activeColor="#111"
        onActiveColor="#eee"
      />
    );
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(2);
    expect(buttons[0].props.accessibilityState).toEqual({ selected: true });
    expect(buttons[1].props.accessibilityState).toEqual({ selected: false });
    await fireEvent.press(buttons[1]);
    expect(onChange).toHaveBeenCalledWith('search');
    await fireEvent.press(buttons[0]);
    expect(onChange).toHaveBeenCalledWith('camera');
  });
});

describe('ScanProgressPanel', () => {
  const base = {
    waitingCount: 0,
    failedCount: 0,
    inProgress: false,
    progressText: 'Scanning',
    isConnected: true,
    activeColor: '#111',
    onSendPending: jest.fn(),
    usage: null,
    notice: null,
    onDismissNotice: jest.fn(),
  };

  beforeEach(() => jest.clearAllMocks());

  it('keeps the live region mounted and hides the progress row when idle', async () => {
    await render(<ScanProgressPanel {...base} />);
    expect(screen.getByTestId('scan-progress-region')).toBeTruthy();
    expect(screen.queryByTestId('scan-progress')).toBeNull();
  });

  it('shows the progress row with its label while a scan is in progress', async () => {
    await render(<ScanProgressPanel {...base} inProgress />);
    expect(screen.getByTestId('scan-progress').props.accessibilityLabel).toBe('Scanning');
  });

  it('shows the pending badge with queue and send-now actions when connected', async () => {
    const onSendPending = jest.fn();
    await render(<ScanProgressPanel {...base} waitingCount={2} onSendPending={onSendPending} />);
    expect(screen.getAllByRole('button')).toHaveLength(2);
    await fireEvent.press(screen.getAllByRole('button')[0]);
    expect(mockOpenScanQueue).toHaveBeenCalledTimes(1);
    await fireEvent.press(screen.getAllByRole('button')[1]);
    expect(onSendPending).toHaveBeenCalledTimes(1);
  });

  it('hides send-now while offline', async () => {
    await render(<ScanProgressPanel {...base} waitingCount={2} isConnected={false} />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('shows only the failed-queue link when nothing is waiting', async () => {
    await render(<ScanProgressPanel {...base} failedCount={3} />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
    await fireEvent.press(screen.getByRole('button'));
    expect(mockOpenScanQueue).toHaveBeenCalledTimes(1);
  });

  it('renders no badge when nothing is waiting or failed', async () => {
    await render(<ScanProgressPanel {...base} />);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
