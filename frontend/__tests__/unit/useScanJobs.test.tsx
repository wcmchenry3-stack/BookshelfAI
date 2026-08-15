import React from 'react';
import { renderHook } from '@testing-library/react-native';

import { useScanJobs } from '../../hooks/useScanJobs';

const mockContextValue = {
  jobs: [],
  startScan: jest.fn(),
  retryScan: jest.fn(),
  cancelScan: jest.fn(),
  clearJobs: jest.fn(),
  reviewingJob: null,
  setReviewingJob: jest.fn(),
  handleSelectBook: jest.fn(),
};

jest.mock('../../contexts/ScanJobContext', () => ({
  ScanJobContext: require('react').createContext(null),
}));

describe('useScanJobs', () => {
  it('returns the ScanJobContext value when wrapped in provider', async () => {
    const { ScanJobContext } = require('../../contexts/ScanJobContext');
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ScanJobContext.Provider value={mockContextValue}>{children}</ScanJobContext.Provider>
    );
    const { result } = await renderHook(() => useScanJobs(), { wrapper });
    expect(result.current).toBe(mockContextValue);
  });

  it('returns null when no provider is present', async () => {
    const { result } = await renderHook(() => useScanJobs());
    expect(result.current).toBeNull();
  });
});
