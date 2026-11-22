import { useContext } from 'react';
import { TurnstileContext } from '../contexts/TurnstileContext';

/** `getToken()` for POST /scan (#740); see TurnstileContext. */
export function useTurnstile() {
  return useContext(TurnstileContext);
}
