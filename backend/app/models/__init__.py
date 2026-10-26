# Import all models here so Alembic's env.py sees them via Base.metadata
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import AIInflight, AIQuota
from app.models.ai_settings import AISetting
from app.models.ai_spend import AISpendDaily
from app.models.ai_usage import AIUsage, AIUsageDaily
from app.models.book import Book
from app.models.edition import Edition
from app.models.refresh_token import RefreshToken
from app.models.scan_correction import ScanCorrection, ScanCorrectionDaily
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook

__all__ = [
    "AIEnhancedZeroResult",
    "AIInflight",
    "AIQuota",
    "AISetting",
    "AISpendDaily",
    "AIUsage",
    "AIUsageDaily",
    "Book",
    "Edition",
    "RefreshToken",
    "ScanCorrection",
    "ScanCorrectionDaily",
    "ScanRecord",
    "User",
    "UserBook",
    "UserCustomBook",
]
