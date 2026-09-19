"""收藏作物的 API schema。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.catalog import ProductOut
from app.services.favorites import FavoriteItem


class FavoritePriceOut(BaseModel):
    """收藏清單上那一行價格。已經算好，前端直接顯示。"""

    trade_date: date
    price_avg: Decimal | None = None
    currency: str
    unit: str
    # 取這個價格的市場。跨多個市場時是其中一個的名字，搭配 market_count 看
    market_name: str
    market_count: int = Field(description="這個價格聚合了幾個市場")
    # 相對前一個有資料的交易日的漲跌幅（%）。只有一天資料時是 null
    change_pct: float | None = None


class FavoriteOut(BaseModel):
    product: ProductOut
    favorited_at: datetime
    # 近 14 天內沒有官方行情時是 null——例如非產季，或該國還沒接資料源
    latest: FavoritePriceOut | None = None

    @classmethod
    def from_item(cls, item: FavoriteItem, locale: str) -> "FavoriteOut":
        latest = None
        if item.latest is not None:
            latest = FavoritePriceOut(
                trade_date=item.latest.trade_date,
                price_avg=item.latest.price_avg,
                currency=item.latest.currency,
                unit=item.latest.unit,
                market_name=item.latest.market_name,
                market_count=item.latest.market_count,
                change_pct=item.latest.change_pct,
            )
        return cls(
            product=ProductOut.from_model(item.product, locale),
            favorited_at=item.favorited_at,
            latest=latest,
        )


class FavoriteListOut(BaseModel):
    """收藏清單。不分頁——有上限，一次全給比較省往返。"""

    items: list[FavoriteOut]
    total: int
    limit: int = Field(description="收藏數量上限")
    # 價格是以哪個國家的市場計算的
    country_code: str | None = None
