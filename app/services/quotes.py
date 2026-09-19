"""小農 / 盤商自行報價的商業邏輯。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.data.countries import default_currency, lookup_subdivision
from app.core.pagination import PageParams
from app.models.catalog import Product
from app.models.enums import QuoteSide, QuoteStatus, UserRole
from app.models.quote import Quote
from app.models.user import User


@dataclass
class QuoteStats:
    """某品項目前所有有效報價的摘要，給清單頁一眼看懂行情。"""

    count: int
    price_min: Decimal | None
    price_max: Decimal | None
    price_avg: Decimal | None
    currency: str | None
    unit: str | None


async def create_quote(
    session: AsyncSession,
    user: User,
    *,
    product_id: uuid.UUID,
    price: Decimal,
    side: QuoteSide,
    unit: str | None = None,
    currency: str | None = None,
    grade: str | None = None,
    quantity: Decimal | None = None,
    min_order: Decimal | None = None,
    market_id: uuid.UUID | None = None,
    region: str | None = None,
    location_text: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    note: str | None = None,
    contact_phone_public: bool | None = None,
    valid_hours: int | None = None,
) -> Quote:
    if not user.can_quote:
        raise ForbiddenError(
            "只有小農或盤商身分可以報價，請先到個人設定切換身分",
            code="role_cannot_quote",
        )

    product = await session.get(Product, product_id)
    if product is None or not product.is_active:
        raise NotFoundError("Product not found", code="product_not_found")

    active = await session.scalar(
        select(func.count())
        .select_from(Quote)
        .where(Quote.user_id == user.id, Quote.status == QuoteStatus.ACTIVE)
    )
    if (active or 0) >= settings.quote_max_active_per_user:
        raise ConflictError(
            f"有效報價已達上限（{settings.quote_max_active_per_user} 筆），請先下架部分報價",
            code="quote_limit_reached",
        )

    now = datetime.now(UTC)
    ttl = valid_hours if valid_hours is not None else settings.quote_default_ttl_hours

    quote = Quote(
        user_id=user.id,
        product_id=product_id,
        market_id=market_id,
        side=side,
        role_snapshot=user.role,
        status=QuoteStatus.ACTIVE,
        price=price,
        currency=(currency or default_currency(user.country_code)).upper(),
        unit=unit or product.default_unit,
        grade=grade,
        quantity=quantity,
        min_order=min_order,
        country_code=user.country_code,
        # 沒指定就沿用個人檔案的所在地（行政區名優先，其次自由輸入的城鎮）
        region=region or _user_region(user),
        location_text=location_text,
        latitude=latitude,
        longitude=longitude,
        note=note,
        contact_phone_public=(
            user.contact_phone_public if contact_phone_public is None else contact_phone_public
        ),
        valid_from=now,
        valid_until=now + timedelta(hours=ttl) if ttl > 0 else None,
    )
    session.add(quote)
    await session.flush()
    return quote


async def list_quotes(
    session: AsyncSession,
    *,
    params: PageParams,
    product_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    side: QuoteSide | None = None,
    role: UserRole | None = None,
    country_code: str | None = None,
    region: str | None = None,
    market_id: uuid.UUID | None = None,
    status: QuoteStatus | None = QuoteStatus.ACTIVE,
    include_expired: bool = False,
) -> tuple[list[Quote], int]:
    stmt: Select = select(Quote).options(
        selectinload(Quote.user), selectinload(Quote.product).selectinload(Product.names)
    )
    count_stmt = select(func.count()).select_from(Quote)

    conditions = []
    if product_id is not None:
        conditions.append(Quote.product_id == product_id)
    if user_id is not None:
        conditions.append(Quote.user_id == user_id)
    if side is not None:
        conditions.append(Quote.side == side)
    if role is not None:
        conditions.append(Quote.role_snapshot == role)
    if country_code:
        conditions.append(Quote.country_code == country_code.upper())
    if region:
        conditions.append(Quote.region == region)
    if market_id is not None:
        conditions.append(Quote.market_id == market_id)
    if status is not None:
        conditions.append(Quote.status == status)
    if not include_expired:
        # 已過期但還沒被清理工作掃到的，查詢時就先濾掉
        conditions.append(
            or_(Quote.valid_until.is_(None), Quote.valid_until > datetime.now(UTC))
        )

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    stmt = stmt.order_by(Quote.created_at.desc()).limit(params.limit).offset(params.offset)
    items = list((await session.execute(stmt)).scalars().unique())
    total = (await session.scalar(count_stmt)) or 0
    return items, total


async def quote_stats(
    session: AsyncSession, product_id: uuid.UUID, *, side: QuoteSide | None = None
) -> QuoteStats:
    stmt = select(
        func.count(),
        func.min(Quote.price),
        func.max(Quote.price),
        func.avg(Quote.price),
        func.min(Quote.currency),
        func.min(Quote.unit),
    ).where(
        Quote.product_id == product_id,
        Quote.status == QuoteStatus.ACTIVE,
        or_(Quote.valid_until.is_(None), Quote.valid_until > datetime.now(UTC)),
    )
    if side is not None:
        stmt = stmt.where(Quote.side == side)

    count, lo, hi, avg, currency, unit = (await session.execute(stmt)).one()
    return QuoteStats(
        count=count or 0,
        price_min=lo,
        price_max=hi,
        price_avg=Decimal(str(avg)).quantize(Decimal("0.01")) if avg is not None else None,
        currency=currency,
        unit=unit,
    )


async def get_quote(session: AsyncSession, quote_id: uuid.UUID) -> Quote:
    quote = await session.scalar(
        select(Quote)
        .options(selectinload(Quote.user), selectinload(Quote.product).selectinload(Product.names))
        .where(Quote.id == quote_id)
    )
    if quote is None:
        raise NotFoundError("Quote not found", code="quote_not_found")
    return quote


async def update_quote(
    session: AsyncSession, quote: Quote, user: User, **changes: object
) -> Quote:
    if quote.user_id != user.id:
        raise ForbiddenError("只能修改自己的報價", code="not_quote_owner")
    if quote.status in (QuoteStatus.WITHDRAWN, QuoteStatus.HIDDEN):
        raise ConflictError("已下架的報價無法修改", code="quote_not_editable")

    editable = {
        "price",
        "quantity",
        "min_order",
        "grade",
        "unit",
        "note",
        "region",
        "location_text",
        "latitude",
        "longitude",
        "contact_phone_public",
        "market_id",
    }
    for field, value in changes.items():
        if value is not None and field in editable:
            setattr(quote, field, value)

    valid_hours = changes.get("valid_hours")
    if isinstance(valid_hours, int):
        quote.valid_until = (
            datetime.now(UTC) + timedelta(hours=valid_hours) if valid_hours > 0 else None
        )
        # 續期等於重新上架
        quote.status = QuoteStatus.ACTIVE

    await session.flush()
    return quote


async def withdraw_quote(session: AsyncSession, quote: Quote, user: User) -> Quote:
    if quote.user_id != user.id:
        raise ForbiddenError("只能下架自己的報價", code="not_quote_owner")
    quote.status = QuoteStatus.WITHDRAWN
    await session.flush()
    return quote


async def expire_stale_quotes(session: AsyncSession) -> int:
    """把過期的有效報價標成 expired。由排程定期呼叫。"""
    result = await session.execute(
        update(Quote)
        .where(
            Quote.status == QuoteStatus.ACTIVE,
            Quote.valid_until.is_not(None),
            Quote.valid_until <= datetime.now(UTC),
        )
        .values(status=QuoteStatus.EXPIRED)
    )
    return result.rowcount or 0


def _user_region(user: User) -> str | None:
    """個人檔案的所在地，用來當報價地區的預設值。

    有 ISO 3166-2 代碼就用該行政區在使用者語系下的名稱，
    沒有（該國尚未收錄清單）就退回自由輸入的 locality。
    """
    sub = lookup_subdivision(user.subdivision_code)
    if sub is not None:
        return sub.display_name(user.locale)
    return user.locality
