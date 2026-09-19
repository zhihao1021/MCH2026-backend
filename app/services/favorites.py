"""收藏作物的商業邏輯。

重點在 `list_favorites`：它不只回品項，還把每個品項的最新官方價與漲跌
一起算好。功能機上「我關心的五樣作物今天多少錢」應該是一次請求就拿到，
不該讓前端對每個收藏各打一次 /overview。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import country_scope
from app.core.errors import ConflictError, NotFoundError
from app.models.catalog import Market, Product
from app.models.favorite import ProductFavorite
from app.models.price import OfficialPrice
from app.models.user import User

# 一個人最多收藏幾樣。功能機一頁也列不完，太多反而失去「快速查看」的意義
MAX_FAVORITES = 30

# 找最新價格時往回看幾天。各市場休市日不同，只看今天多半是空的
LOOKBACK_DAYS = 14


@dataclass
class FavoritePrice:
    """收藏清單上顯示的那一行價格。"""

    trade_date: date
    price_avg: Decimal | None
    currency: str
    unit: str
    market_name: str
    market_count: int
    # 相對前一個有資料的交易日的漲跌幅（%）。只有一天資料時是 None
    change_pct: float | None


@dataclass
class FavoriteItem:
    product: Product
    favorited_at: datetime
    latest: FavoritePrice | None


async def list_favorites(
    session: AsyncSession, user: User, *, country_code: str | None = None
) -> list[FavoriteItem]:
    """使用者的收藏，附上每個品項的最新官方價。

    價格預設只看使用者自己國家的市場——烏干達的使用者看到台幣報價
    沒有意義，而且不同幣別混在同一張清單上無法比較。
    可以用 `country_code` 覆寫。
    """
    scope_country = (country_code or user.country_code or "").upper() or None

    rows = await session.execute(
        select(ProductFavorite)
        .options(selectinload(ProductFavorite.product).selectinload(Product.names))
        .where(ProductFavorite.user_id == user.id)
        .order_by(ProductFavorite.created_at.desc())
    )
    favorites = list(rows.scalars())
    if not favorites:
        return []

    prices = await _latest_prices(
        session,
        product_ids=[f.product_id for f in favorites],
        country_code=scope_country,
    )
    return [
        FavoriteItem(
            product=f.product,
            favorited_at=f.created_at,
            latest=prices.get(f.product_id),
        )
        for f in favorites
    ]


async def _latest_prices(
    session: AsyncSession, *, product_ids: list[uuid.UUID], country_code: str | None
) -> dict[uuid.UUID, FavoritePrice]:
    """一次把所有收藏品項的最新價算出來，避免 N+1。

    做法：先找出每個品項最近兩個有資料的交易日，再各自跨市場聚合，
    兩者相減就是漲跌。跨市場用交易量加權，沒有量的退回算術平均——
    與 `prices.price_series` 同一套規則，兩邊數字才會一致。
    """
    if not product_ids:
        return {}

    cutoff = datetime.now(UTC).date() - timedelta(days=LOOKBACK_DAYS)

    base = (
        select(
            OfficialPrice.product_id,
            OfficialPrice.trade_date,
            case(
                (
                    func.sum(func.coalesce(OfficialPrice.volume, 0)) > 0,
                    func.sum(OfficialPrice.price_avg * func.coalesce(OfficialPrice.volume, 0))
                    / func.nullif(func.sum(func.coalesce(OfficialPrice.volume, 0)), 0),
                ),
                else_=func.avg(OfficialPrice.price_avg),
            ).label("price_avg"),
            func.min(OfficialPrice.currency).label("currency"),
            func.min(OfficialPrice.unit).label("unit"),
            func.count(func.distinct(OfficialPrice.market_id)).label("market_count"),
            func.min(Market.name).label("market_name"),
        )
        .join(Market, Market.id == OfficialPrice.market_id)
        .where(
            OfficialPrice.product_id.in_(product_ids),
            OfficialPrice.trade_date >= cutoff,
            OfficialPrice.price_avg.is_not(None),
        )
        .group_by(OfficialPrice.product_id, OfficialPrice.trade_date)
    )
    if country_code:
        base = base.where(Market.country_code == country_code)
    base = country_scope.apply(base, Market.country_code)

    daily = base.subquery()
    ranked = select(
        daily,
        func.row_number()
        .over(partition_by=daily.c.product_id, order_by=daily.c.trade_date.desc())
        .label("rn"),
    ).subquery()

    rows = (await session.execute(select(ranked).where(ranked.c.rn <= 2))).all()

    by_product: dict[uuid.UUID, list] = {}
    for row in rows:
        by_product.setdefault(row.product_id, []).append(row)

    result: dict[uuid.UUID, FavoritePrice] = {}
    for product_id, entries in by_product.items():
        entries.sort(key=lambda r: r.rn)
        newest = entries[0]
        previous = entries[1] if len(entries) > 1 else None

        change = None
        if previous is not None and previous.price_avg:
            change = float(
                (newest.price_avg - previous.price_avg) / previous.price_avg * 100
            )

        result[product_id] = FavoritePrice(
            trade_date=newest.trade_date,
            price_avg=_q(newest.price_avg),
            currency=newest.currency or "",
            unit=newest.unit or "",
            market_name=newest.market_name or "",
            market_count=newest.market_count or 0,
            change_pct=round(change, 2) if change is not None else None,
        )
    return result


async def add_favorite(session: AsyncSession, user: User, product: Product) -> ProductFavorite:
    """加入收藏。已經收藏過就回原本那筆，不報錯（PUT 語意）。"""
    existing = await session.scalar(
        select(ProductFavorite).where(
            ProductFavorite.user_id == user.id,
            ProductFavorite.product_id == product.id,
        )
    )
    if existing is not None:
        return existing

    count = await count_favorites(session, user.id)
    if count >= MAX_FAVORITES:
        raise ConflictError(
            f"收藏數已達上限（{MAX_FAVORITES} 個），請先移除一些",
            code="favorite_limit_reached",
            details={"limit": MAX_FAVORITES},
        )

    # 兩個請求同時進來時靠唯一鍵收斂，不要讓其中一個 500
    stmt = (
        pg_insert(ProductFavorite)
        .values(user_id=user.id, product_id=product.id)
        .on_conflict_do_nothing(constraint="uq_product_favorites_user_id_product_id")
        .returning(ProductFavorite)
    )
    created = (await session.execute(stmt)).scalar_one_or_none()
    if created is None:
        # 剛好被另一個請求搶先建立了，讀回來即可
        created = await session.scalar(
            select(ProductFavorite).where(
                ProductFavorite.user_id == user.id,
                ProductFavorite.product_id == product.id,
            )
        )
    await session.flush()
    assert created is not None
    return created


async def remove_favorite(session: AsyncSession, user: User, product: Product) -> None:
    favorite = await session.scalar(
        select(ProductFavorite).where(
            ProductFavorite.user_id == user.id,
            ProductFavorite.product_id == product.id,
        )
    )
    if favorite is None:
        raise NotFoundError("這個品項不在收藏中", code="favorite_not_found")
    await session.delete(favorite)
    await session.flush()


async def count_favorites(session: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ProductFavorite)
            .where(ProductFavorite.user_id == user_id)
        )
    ) or 0


async def favorited_product_ids(
    session: AsyncSession, user_id: uuid.UUID, product_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """這批品項裡哪些已經被收藏。給清單頁標示愛心用，一次查完不要 N+1。"""
    if not product_ids:
        return set()
    rows = await session.execute(
        select(ProductFavorite.product_id).where(
            ProductFavorite.user_id == user_id,
            ProductFavorite.product_id.in_(product_ids),
        )
    )
    return set(rows.scalars())


def _q(value: Decimal | float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))
