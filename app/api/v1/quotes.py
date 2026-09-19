"""小農 / 盤商報價端點。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, DbSession, Locale, OptionalUser, Paging, QuoterUser
from app.core.pagination import Page
from app.models.enums import QuoteSide, QuoteStatus, UserRole
from app.schemas.quote import QuoteCreate, QuoteOut, QuoteRegionOut, QuoteUpdate
from app.services import quotes as quote_service

router = APIRouter(prefix="/quotes", tags=["quotes"])


@router.get("", response_model=Page[QuoteOut], summary="瀏覽報價")
async def list_quotes(
    session: DbSession,
    paging: Paging,
    locale: Locale,
    viewer: OptionalUser,
    product_id: Annotated[uuid.UUID | None, Query()] = None,
    side: Annotated[QuoteSide | None, Query(description="sell=我要賣, buy=我要收")] = None,
    role: Annotated[UserRole | None, Query(description="只看小農或只看盤商")] = None,
    country_code: Annotated[str | None, Query()] = None,
    subdivision_code: Annotated[
        str | None, Query(description="ISO 3166-2，例如 TW-TPE。**精確篩選請用這個**")
    ] = None,
    region: Annotated[
        str | None,
        Query(description="地區顯示名。寬鬆比對（吸收臺／台），精確請改用 subdivision_code"),
    ] = None,
    market_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[QuoteOut]:
    items, total = await quote_service.list_quotes(
        session,
        params=paging,
        product_id=product_id,
        side=side,
        role=role,
        country_code=country_code,
        subdivision_code=subdivision_code,
        region=region,
        market_id=market_id,
        status=QuoteStatus.ACTIVE,
    )
    viewer_id = viewer.id if viewer else None
    return Page.build(
        [QuoteOut.from_model(q, locale, viewer_id=viewer_id) for q in items], total, paging
    )


@router.get("/regions", response_model=list[QuoteRegionOut], summary="有報價的地區")
async def list_quote_regions(
    session: DbSession,
    country_code: Annotated[str | None, Query()] = None,
) -> list[QuoteRegionOut]:
    """地區選單請用這支。

    **不要用 `/markets/regions`**：那是市場的地區，與報價的地區來自不同來源，
    字面不一定相同（`台北市` vs `臺北市`），拿去篩報價會查不到東西。
    這裡回的每個值都保證至少有一筆有效報價。
    """
    rows = await quote_service.list_quote_regions(session, country_code=country_code)
    return [
        QuoteRegionOut(region=r, subdivision_code=sub, country_code=cc, quote_count=n)
        for r, sub, cc, n in rows
    ]


# 這條要放在 /{quote_id} 之前，否則 "regions" 會先被當成 UUID 解析
@router.post("", response_model=QuoteOut, status_code=status.HTTP_201_CREATED, summary="新增報價")
async def create_quote(
    payload: QuoteCreate,
    user: QuoterUser,
    session: DbSession,
    locale: Locale,
) -> QuoteOut:
    quote = await quote_service.create_quote(
        session,
        user,
        product_id=payload.product_id,
        price=payload.price,
        side=payload.side,
        unit=payload.unit,
        currency=payload.currency,
        grade=payload.grade,
        quantity=payload.quantity,
        min_order=payload.min_order,
        market_id=payload.market_id,
        region=payload.region,
        location_text=payload.location_text,
        latitude=payload.latitude,
        longitude=payload.longitude,
        note=payload.note,
        contact_phone_public=payload.contact_phone_public,
        valid_hours=payload.valid_hours,
    )
    # 重新載入關聯（user / product）好組出完整回應
    quote = await quote_service.get_quote(session, quote.id)
    return QuoteOut.from_model(quote, locale, viewer_id=user.id)


@router.get("/{quote_id}", response_model=QuoteOut, summary="單筆報價")
async def get_quote(
    quote_id: uuid.UUID, session: DbSession, locale: Locale, viewer: OptionalUser
) -> QuoteOut:
    quote = await quote_service.get_quote(session, quote_id)
    return QuoteOut.from_model(quote, locale, viewer_id=viewer.id if viewer else None)


@router.patch("/{quote_id}", response_model=QuoteOut, summary="修改報價")
async def update_quote(
    quote_id: uuid.UUID,
    payload: QuoteUpdate,
    user: CurrentUser,
    session: DbSession,
    locale: Locale,
) -> QuoteOut:
    quote = await quote_service.get_quote(session, quote_id)
    await quote_service.update_quote(
        session, quote, user, **payload.model_dump(exclude_unset=True)
    )
    return QuoteOut.from_model(quote, locale, viewer_id=user.id)


@router.delete("/{quote_id}", response_model=QuoteOut, summary="下架報價")
async def withdraw_quote(
    quote_id: uuid.UUID, user: CurrentUser, session: DbSession, locale: Locale
) -> QuoteOut:
    quote = await quote_service.get_quote(session, quote_id)
    await quote_service.withdraw_quote(session, quote, user)
    return QuoteOut.from_model(quote, locale, viewer_id=user.id)
