/**
 * The single place that defines where the app lands after sign-in and on
 * launch with a valid stored session. #913 will swap in the user's server-side
 * "Open on" preference here; until then Scan is the default for everyone.
 */
export const DEFAULT_TAB_ROUTE = '/(tabs)/scan' as const;
