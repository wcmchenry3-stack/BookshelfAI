from datetime import date

from pydantic import BaseModel


class ScanUsageResponse(BaseModel):
    """Returned by GET /scan/usage: the current user's scan usage. Standard
    and enhanced scans share one allowance, so both are counted.

    Counts are scans reserved (provider calls, refunds excluded) in the
    current UTC day and UTC calendar month; ``period_end`` is the date the
    monthly count resets (the 1st of the next UTC month).
    """

    used_today: int
    daily_limit: int
    used_month: int
    monthly_limit: int
    period_end: date
    # Enhanced scans reserved this UTC month and the Premium enhanced monthly
    # allowance (#533); the limit is 0 for every other tier.
    enhanced_used_month: int = 0
    enhanced_monthly_limit: int = 0
