"""Counter keys, flags, outcomes and tunables of the quota package.

Numbers a test may tune (timeouts, purge batches) are read at call time
through the package (``quota.CLEANUP_TIMEOUT_SECONDS``), so a patch on
``app.ai.quota`` reaches the code that runs."""

# The ai_quota counter key for every scan, standard or enhanced: one shared
# daily allowance per user (OD-1). Not a gateway feature name.
SCAN_QUOTA_FEATURE = "scan"
# The ai_quota counter key of no-match scans (#577): standard scans whose
# provider answered properly but found no book. The found counter is
# SCAN_QUOTA_FEATURE.
SCAN_NO_MATCH_QUOTA_FEATURE = "scan.no_match"
# The ai_quota counter key of /scan requests (#586): every authenticated
# /scan request past the kill switches, new photo or repeat, refused or not.
# A request count, not an AI call, so the service-wide count
# (global_units_today) leaves it out (NON_AI_QUOTA_FEATURES).
SCAN_REQUESTS_QUOTA_FEATURE = "scan.requests"
# ai_quota keys that count something other than AI calls: never summed into
# the global daily cap.
NON_AI_QUOTA_FEATURES = (SCAN_REQUESTS_QUOTA_FEATURE,)
# What /scan says about a no-match (ScanResponse.no_match, #577).
NO_MATCH_FREE = "free"
NO_MATCH_COUNTED = "counted"
# The Sentry tag for the zero-result warning (the gateway feature name).
ENHANCED_FEATURE_TAG = "scan.enhanced"

# The lease outlives the provider timeout by this much (AC: timeout_s + 30).
LEASE_GRACE_SECONDS = 30
# Seconds a client should wait before retrying while another scan is running.
IN_PROGRESS_RETRY_AFTER_SECONDS = 2
# Bound on each give-back (refund, lease release) so cleanup can't hang a
# request; the lease expiry is the backstop.
CLEANUP_TIMEOUT_SECONDS = 5.0

DAILY_RETENTION_DAYS = 90
MONTHLY_RETENTION_MONTHS = 13
PURGE_BATCH_SIZE = 5_000
# Upper bound on batches per window kind per cleanup run.
PURGE_MAX_BATCHES = 20
ZERO_RESULT_RETENTION_DAYS = 30

# The ai_inflight flag (#590): whether the lease holder took its daily unit.
FLAG_DAILY = "daily_taken"
UNIT_FLAGS = (FLAG_DAILY,)
# What give_back_unit reports.
UNIT_GIVEN_BACK = "given_back"
UNIT_NOT_TAKEN = "not_taken"
UNIT_LEASE_LOST = "lease_lost"

# What _give_back reports: the op succeeded, raised on every attempt, or was
# cut off by CLEANUP_TIMEOUT_SECONDS.
GIVE_BACK_DONE = "done"
GIVE_BACK_FAILED = "failed"
GIVE_BACK_TIMED_OUT = "timed_out"

# Why a unit the request attempted to take was left charged (the fingerprint's
# second element and the ``reason`` tag of the Sentry warning).
UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED = "lease_lost_unacknowledged"
UNIT_KEPT_FALLBACK_FAILED = "fallback_failed"
UNIT_KEPT_GIVE_BACK_FAILED = "give_back_failed"
