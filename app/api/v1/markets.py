"""市場清單端點。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.core.deps import DbSession, Paging
from app.core.pagination import Page
from app.models.catalog import DataSource
from app.schemas.catalog import MarketOut
from app.services import catalog as catalog_service

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("", response_model=Page[MarketOut], summary="市場清單")
async def list_markets(
    session: DbSession,
    paging: Paging,
    country_code: Annotated[str | None, Query(description="ISO 3166-1 alpha-2")] = None,
    source_key: Annotated[str | None, Query(description="只看某個資料來源")] = None,
    q: Annotated[str | None, Query(description="名稱關鍵字")] = None,
) -> Page[MarketOut]:
    items, total = await catalog_service.list_markets(
        session, params=paging, country_code=country_code, source_key=source_key, q=q
    )
    keys = await _source_keys(session, items)
    return Page.build(
        [MarketOut.from_model(m, keys.get(m.source_id)) for m in items], total, paging
    )


@router.get("/{market_id}", response_model=MarketOut, summary="單一市場")
async def get_market(market_id: uuid.UUID, session: DbSession) -> MarketOut:
    market = await catalog_service.get_market(session, market_id)
    keys = await _source_keys(session, [market])
    return MarketOut.from_model(market, keys.get(market.source_id))


async def _source_keys(session: DbSession, markets) -> dict[uuid.UUID, str]:
    """一次把用到的 source key 撈齊，避免每個市場各查一次。"""
    ids = {m.source_id for m in markets if m.source_id}
    if not ids:
        return {}
    rows = await session.execute(
        select(DataSource.id, DataSource.key).where(DataSource.id.in_(ids))
    )
    return {row.id: row.key for row in rows}
