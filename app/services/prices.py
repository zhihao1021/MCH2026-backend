"""官方價格查詢：最新行情、歷史走勢與跨來源摘要。

功能機的螢幕只有 240×320，一次看不了幾行，所以這裡的回傳刻意做成
「已經算好、可以直接畫」的形狀，不讓前端再做聚合。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PageParams
from app.models.catalog import DataSource, Market
from app.models.price import OfficialPrice


@dataclass
class PricePoint:
    trade_date: date
    price_avg: Decimal | None
    price_high: Decimal | None
    price_low: Decimal | None
    volume: Decimal | None
    currency: str
    unit: str


@dataclass
class PriceSeries:
    product_id: uuid.UUID
    market_id: uuid.UUID | None
    currency: str
    unit: str
    points: list[PricePoint]

    @property
    def latest(self) -> PricePoint | None:
        return self.points[-1] if self.points else None

    def change_pct(self) -> float | None:
        """最新一點相對前一點的漲跌幅（%）。資料不足回 None。"""
        usable = [p for p in self.points if p.price_avg is not None]
        if len(usable) < 2:
            return None
        prev, last = usable[-2].price_avg, usable[-1].price_avg
        if not prev:
            return None
        return float((last - prev) / prev * 100)  # type: ignore[operator]


async def latest_official_prices(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    params: PageParams,
    market_id: uuid.UUID | None = None,
    country_code: str | None = None,
    on_date: date | None = None,
    max_age_days: int = 14,
) -> tuple[list[tuple[OfficialPrice, Market, DataSource]], int]:
    """取某品項各市場的最新一筆行情。

    沒指定日期時，取每個市場在 `max_age_days` 內的最後一個交易日，
    因為各市場休市日不同，硬指定「今天」多半會查不到東西。
    """
    cutoff = (on_date or datetime.now(UTC).date()) - timedelta(days=max_age_days)

    base = (
        select(
            OfficialPrice.market_id,
            func.max(OfficialPrice.trade_date).label("latest_date"),
        )
        .where(OfficialPrice.product_id == product_id)
        .group_by(OfficialPrice.market_id)
    )
    if on_date is not None:
        base = base.where(OfficialPrice.trade_date <= on_date)
    base = base.where(OfficialPrice.trade_date >= cutoff)
    if market_id is not None:
        base = base.where(OfficialPrice.market_id == market_id)
    latest = base.subquery()

    stmt: Select = (
        select(OfficialPrice, Market, DataSource)
        .join(
            latest,
            (OfficialPrice.market_id == latest.c.market_id)
            & (OfficialPrice.trade_date == latest.c.latest_date),
        )
        .join(Market, Market.id == OfficialPrice.market_id)
        .join(DataSource, DataSource.id == OfficialPrice.source_id)
        .where(OfficialPrice.product_id == product_id)
    )
    count_stmt = (
        select(func.count())
        .select_from(OfficialPrice)
        .join(
            latest,
            (OfficialPrice.market_id == latest.c.market_id)
            & (OfficialPrice.trade_date == latest.c.latest_date),
        )
        .join(Market, Market.id == OfficialPrice.market_id)
        .where(OfficialPrice.product_id == product_id)
    )

    if country_code:
        cond = Market.country_code == country_code.upper()
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    stmt = (
        stmt.order_by(OfficialPrice.trade_date.desc(), Market.name)
        .limit(params.limit)
        .offset(params.offset)
    )
    rows = [(p, m, s) for p, m, s in (await session.execute(stmt)).all()]
    total = (await session.scalar(count_stmt)) or 0
    return rows, total


async def price_series(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    market_id: uuid.UUID | None = None,
    country_code: str | None = None,
    days: int = 30,
    end: date | None = None,
) -> PriceSeries:
    """品項的每日走勢。

    未指定市場時跨市場以交易量加權平均；沒有交易量的紀錄退回算術平均，
    否則整段序列會被少數有量的市場帶偏。
    """
    end = end or datetime.now(UTC).date()
    start = end - timedelta(days=days - 1)

    weighted = func.sum(OfficialPrice.price_avg * func.coalesce(OfficialPrice.volume, 0))
    total_volume = func.sum(func.coalesce(OfficialPrice.volume, 0))

    stmt = (
        select(
            OfficialPrice.trade_date,
            case(
                (total_volume > 0, weighted / func.nullif(total_volume, 0)),
                else_=func.avg(OfficialPrice.price_avg),
            ).label("price_avg"),
            func.max(OfficialPrice.price_high).label("price_high"),
            func.min(OfficialPrice.price_low).label("price_low"),
            func.sum(OfficialPrice.volume).label("volume"),
            func.min(OfficialPrice.currency).label("currency"),
            func.min(OfficialPrice.unit).label("unit"),
        )
        .where(
            OfficialPrice.product_id == product_id,
            OfficialPrice.trade_date >= start,
            OfficialPrice.trade_date <= end,
        )
        .group_by(OfficialPrice.trade_date)
        .order_by(OfficialPrice.trade_date)
    )

    if market_id is not None:
        stmt = stmt.where(OfficialPrice.market_id == market_id)
    if country_code:
        stmt = stmt.join(Market, Market.id == OfficialPrice.market_id).where(
            Market.country_code == country_code.upper()
        )

    rows = (await session.execute(stmt)).all()
    points = [
        PricePoint(
            trade_date=r.trade_date,
            price_avg=_q(r.price_avg),
            price_high=_q(r.price_high),
            price_low=_q(r.price_low),
            volume=r.volume,
            currency=r.currency or "",
            unit=r.unit or "",
        )
        for r in rows
    ]
    return PriceSeries(
        product_id=product_id,
        market_id=market_id,
        currency=points[-1].currency if points else "",
        unit=points[-1].unit if points else "",
        points=points,
    )


async def market_coverage(session: AsyncSession, product_id: uuid.UUID) -> list[Market]:
    """這個品項目前有哪些市場有資料。給前端做市場下拉選單。"""
    stmt = (
        select(Market)
        .join(OfficialPrice, OfficialPrice.market_id == Market.id)
        .where(OfficialPrice.product_id == product_id)
        .group_by(Market.id)
        .order_by(func.max(OfficialPrice.trade_date).desc())
    )
    return list((await session.execute(stmt)).scalars())


def _q(value: Decimal | float | None) -> Decimal | None:
    """把聚合結果收斂到兩位小數，避免浮點尾數噴到前端。"""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))
