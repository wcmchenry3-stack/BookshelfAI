from datetime import date

from pydantic import BaseModel


class ScanUsageResponse(BaseModel):
    """Returned by GET /scan/usage: the current user's scan usage (#577).

    ``found_used_today`` / ``found_daily_limit``: scans that used a found
    unit in the current UTC day (provider calls that found books, plus
    no-match scans counted because the no-match allowance was used up, plus
    calls kept charged after an error; refunds excluded) and the tier's found
    allowance. Standard and enhanced scans share it. A scan is refused (429
    ``daily_scan_limit``) when it is full.

    ``no_match_used_today`` / ``no_match_daily_limit``: standard scans where
    the model answered but found no book that did not use a found unit, and
    the tier's no-match allowance.

    ``used_today`` / ``daily_limit`` are the found counter again, kept for
    one release for older app builds, then removed.

    There is no monthly limit (#582). ``used_month``, ``monthly_limit``,
    ``period_end``, ``enhanced_used_month`` and ``enhanced_monthly_limit``
    are always ``null``: kept in the body for one release so an older app
    build that still reads them gets a value it can parse, then removed.
    """

    used_today: int
    daily_limit: int
    found_used_today: int = 0
    found_daily_limit: int = 0
    no_match_used_today: int = 0
    no_match_daily_limit: int = 0
    # Deprecated (#582): always null. Remove after one release.
    used_month: int | None = None
    monthly_limit: int | None = None
    period_end: date | None = None
    enhanced_used_month: int | None = None
    enhanced_monthly_limit: int | None = None
