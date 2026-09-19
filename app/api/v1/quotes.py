"""小農 / 盤商報價端點。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, DbSession, Locale, OptionalUser, Paging, QuoterUser
from app.core.pagination import Page
from app.models.enums import QuoteSide, QuoteStatus, UserRole
from app.schemas.quote import QuoteCreate, QuoteOut, QuoteUpdate
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
    region: Annotated[str | None, Query()] = None,
    market_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[QuoteOut]:
    items, total = await quote_service.list_quotes(
        session,
        params=paging,
        product_id=product_id,
        side=side,
        role=role,
        country_code=country_code,
        region=region,
        market_id=market_id,
        status=QuoteStatus.ACTIVE,
    )
    viewer_id = viewer.id if viewer else None
    return Page.build(
        [QuoteOut.from_model(q, locale, viewer_id=viewer_id) for q in items], total, paging
    )


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
