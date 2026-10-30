/** Request timeouts (#766). Kept apart from lib/api.ts so tests that mock the client keep the real values. */

/** Default request timeout. */
export const DEFAULT_TIMEOUT_MS = 15_000;
/** Per-call timeout for POST /scan (photo upload plus AI processing). */
export const SCAN_UPLOAD_TIMEOUT_MS = 90_000;
