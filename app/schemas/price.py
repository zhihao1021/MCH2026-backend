"""價格相關的 API schema。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.catalog import DataSource, Market
from app.models.price import OfficialPrice
from app.schemas.catalog import ImageCreditOut, ProductOut


class OfficialPriceOut(BaseModel):
    """單一市場的一筆官方行情。"""

    market_id: uuid.UUID
    market_name: str
    region: str | None = None
    country_code: str
    source_key: str
    trade_date: date
    currency: str
    unit: str
    grade: str | None = None
    price_avg: Decimal | None = None
    price_high: Decimal | None = None
    price_low: Decimal | None = None
    volume: Decimal | None = None
    volume_unit: str | None = None

    @classmethod
    def from_row(
        cls, price: OfficialPrice, market: Market, source: DataSource
    ) -> "OfficialPriceOut":
        return cls(
            market_id=market.id,
            market_name=market.name,
            region=market.region,
            country_code=market.country_code,
            source_key=source.key,
            trade_date=price.trade_date,
            currency=price.currency,
            unit=price.unit,
            grade=price.grade or None,
            price_avg=price.price_avg,
            price_high=price.price_high,
            price_low=price.price_low,
            volume=price.volume,
            volume_unit=price.volume_unit,
        )


class PricePointOut(BaseModel):
    """走勢圖的一個點。欄位名刻意縮短，功能機頻寬省一點是一點。"""

    d: date = Field(description="交易日")
    avg: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    vol: Decimal | None = None


class PriceSeriesOut(BaseModel):
    product_id: uuid.UUID
    market_id: uuid.UUID | None = None
    currency: str
    unit: str
    # 最新一點相對前一點的漲跌幅（%）
    change_pct: float | None = None
    points: list[PricePointOut]


class QuoteSummaryOut(BaseModel):
    """民間報價摘要，與官方價並排顯示。"""

    count: int
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    price_avg: Decimal | None = None
    currency: str | None = None
    unit: str | None = None


class ProductPriceOverview(BaseModel):
    """品項詳情頁一次要的全部東西，避免功能機連打三支 API。"""

    product: ProductOut
    # 圖片出處。詳情頁會大張顯示圖片，所以這裡必須帶完整授權資訊
    image: ImageCreditOut | None = None
    official: list[OfficialPriceOut]
    official_series: PriceSeriesOut | None = None
    quotes: QuoteSummaryOut
    updated_at: datetime
