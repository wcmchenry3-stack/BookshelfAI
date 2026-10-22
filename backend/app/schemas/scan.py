from datetime import date

from pydantic import BaseModel


class ScanUsageResponse(BaseModel):
    """Returned by GET /scan/usage: the current user's standard-scan usage.

    Counts are scans reserved (provider calls, refunds excluded) in the
    current UTC day and UTC calendar month; ``period_end`` is the date the
    monthly count resets (the 1st of the next UTC month).
    """

    used_today: int
    daily_limit: int
    used_month: int
    monthly_limit: int
    period_end: date
