"""匯總所有 ORM 模型，讓 Alembic 的 target_metadata 能一次看到全部。"""

from app.models.base import Base
from app.models.catalog import (
    DataSource,
    Market,
    Product,
    ProductName,
    ProductSourceMapping,
)
from app.models.enums import (
    IntentExclusion,
    IntentStatus,
    IngestStatus,
    OtpPurpose,
    ProductCategory,
    QuoteSide,
    QuoteStatus,
    RetailExclusion,
    RetailReportStatus,
    StoreType,
    UserRole,
)
from app.models.favorite import ProductFavorite
from app.models.intent import IntentNotification, PriceIntent, UserReputation
from app.models.price import IngestRun, OfficialPrice
from app.models.quote import Quote
from app.models.retail import RetailPriceReport
from app.models.user import OtpCode, RefreshToken, User

__all__ = [
    "Base",
    "DataSource",
    "IngestRun",
    "IngestStatus",
    "Market",
    "OfficialPrice",
    "OtpCode",
    "OtpPurpose",
    "IntentExclusion",
    "IntentNotification",
    "IntentStatus",
    "PriceIntent",
    "Product",
    "ProductFavorite",
    "ProductCategory",
    "ProductName",
    "ProductSourceMapping",
    "Quote",
    "QuoteSide",
    "QuoteStatus",
    "RefreshToken",
    "RetailExclusion",
    "RetailPriceReport",
    "RetailReportStatus",
    "StoreType",
    "User",
    "UserRole",
    "UserReputation",
]
