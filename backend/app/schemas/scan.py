from datetime import date

from pydantic import BaseModel


class ScanUsageResponse(BaseModel):
    """Returned by GET /scan/usage: the current user's scan usage. Standard
    and enhanced scans share one daily allowance, so both are counted.

    ``used_today`` is scans reserved (provider calls, refunds excluded) in the
    current UTC day; ``daily_limit`` is the tier's daily allowance.

    There is no monthly limit (#582). ``used_month``, ``monthly_limit``,
    ``period_end``, ``enhanced_used_month`` and ``enhanced_monthly_limit``
    are always ``null``: kept in the body for one release so an older app
    build that still reads them gets a value it can parse, then removed.
    """

    used_today: int
    daily_limit: int
    # Deprecated (#582): always null. Remove after one release.
    used_month: int | None = None
    monthly_limit: int | None = None
    period_end: date | None = None
    enhanced_used_month: int | None = None
    enhanced_monthly_limit: int | None = None
