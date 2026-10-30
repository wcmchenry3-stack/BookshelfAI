import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { FeedbackWidget } from './FeedbackWidget';
import { FeedbackThanksBanner } from './FeedbackThanksBanner';

interface FeedbackContextValue {
  openFeedback: () => void;
}

const FeedbackContext = createContext<FeedbackContextValue>({ openFeedback: () => {} });

/**
 * Owns the single feedback sheet and thanks banner so every entry point (the
 * header "?" button, the Settings row) opens the same widget and the thank-you
 * shows on whichever screen is visible.
 */
export function FeedbackProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  // Mount the sheet on first use only; it then stays mounted so it can animate out.
  const [mounted, setMounted] = useState(false);
  const [thanksVisible, setThanksVisible] = useState(false);
  const openFeedback = useCallback(() => {
    setMounted(true);
    setOpen(true);
  }, []);
  const closeFeedback = useCallback(() => setOpen(false), []);
  const showThanks = useCallback(() => setThanksVisible(true), []);
  const dismissThanks = useCallback(() => setThanksVisible(false), []);
  const value = useMemo(() => ({ openFeedback }), [openFeedback]);

  return (
    <FeedbackContext.Provider value={value}>
      {children}
      {mounted ? (
        <FeedbackWidget visible={open} onClose={closeFeedback} onSubmitted={showThanks} />
      ) : null}
      <FeedbackThanksBanner visible={thanksVisible} onDismiss={dismissThanks} />
    </FeedbackContext.Provider>
  );
}

export function useFeedback(): FeedbackContextValue {
  return useContext(FeedbackContext);
}
