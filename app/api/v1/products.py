"""品項與價格查詢端點。App 的主要讀取面都在這裡。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import DbSession, Locale, Paging
from app.core.pagination import Page, PageParams
from app.models.enums import ProductCategory
from app.schemas.catalog import MarketOut, ProductDetailOut, ProductOut
from app.schemas.price import (
    OfficialPriceOut,
    PricePointOut,
    PriceSeriesOut,
    ProductPriceOverview,
    QuoteSummaryOut,
)
from app.services import catalog as catalog_service
from app.services import prices as price_service
from app.services import quotes as quote_service

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=Page[ProductOut], summary="搜尋品項")
async def search_products(
    session: DbSession,
    paging: Paging,
    locale: Locale,
    q: Annotated[str | None, Query(description="關鍵字，比對各語系名稱與別名")] = None,
    category: Annotated[ProductCategory | None, Query(description="分類")] = None,
) -> Page[ProductOut]:
    items, total = await catalog_service.search_products(
        session, params=paging, q=q, category=category, locale=locale
    )
    return Page.build([ProductOut.from_model(p, locale) for p in items], total, paging)


@router.get("/{ref}", response_model=ProductDetailOut, summary="品項詳情")
async def get_product(ref: str, session: DbSession, locale: Locale) -> ProductDetailOut:
    """`ref` 可以是 UUID 或 slug。"""
    product = await catalog_service.resolve_product(session, ref)
    return ProductDetailOut.from_model(product, locale)


@router.get(
    "/{ref}/overview",
    response_model=ProductPriceOverview,
    summary="品項總覽（官方價 + 走勢 + 報價摘要）",
)
async def product_overview(
    ref: str,
    session: DbSession,
    locale: Locale,
    days: Annotated[int, Query(ge=2, le=365, description="走勢天數")] = 14,
    country_code: Annotated[str | None, Query(description="只看某個國家")] = None,
    markets_limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ProductPriceOverview:
    """功能機一次拿齊詳情頁需要的資料，省下兩趟往返。"""
    product = await catalog_service.resolve_product(session, ref)

    rows, _ = await price_service.latest_official_prices(
        session,
        product_id=product.id,
        params=PageParams(limit=markets_limit, offset=0),
        country_code=country_code,
    )
    series = await price_service.price_series(
        session, product_id=product.id, days=days, country_code=country_code
    )
    stats = await quote_service.quote_stats(session, product.id)

    return ProductPriceOverview(
        product=ProductOut.from_model(product, locale),
        official=[OfficialPriceOut.from_row(p, m, s) for p, m, s in rows],
        official_series=_series_out(series),
        quotes=QuoteSummaryOut(
            count=stats.count,
            price_min=stats.price_min,
            price_max=stats.price_max,
            price_avg=stats.price_avg,
            currency=stats.currency,
            unit=stats.unit,
        ),
        updated_at=datetime.now(UTC),
    )


@router.get(
    "/{ref}/prices/official",
    response_model=Page[OfficialPriceOut],
    summary="各市場最新官方價",
)
async def official_prices(
    ref: str,
    session: DbSession,
    paging: Paging,
    country_code: Annotated[str | None, Query()] = None,
    market_id: Annotated[str | None, Query()] = None,
    max_age_days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> Page[OfficialPriceOut]:
    import uuid as _uuid

    product = await catalog_service.resolve_product(session, ref)
    rows, total = await price_service.latest_official_prices(
        session,
        product_id=product.id,
        params=paging,
        country_code=country_code,
        market_id=_uuid.UUID(market_id) if market_id else None,
        max_age_days=max_age_days,
    )
    return Page.build([OfficialPriceOut.from_row(p, m, s) for p, m, s in rows], total, paging)


@router.get("/{ref}/prices/series", response_model=PriceSeriesOut, summary="官方價走勢")
async def price_series(
    ref: str,
    session: DbSession,
    days: Annotated[int, Query(ge=2, le=365)] = 30,
    market_id: Annotated[str | None, Query()] = None,
    country_code: Annotated[str | None, Query()] = None,
) -> PriceSeriesOut:
    import uuid as _uuid

    product = await catalog_service.resolve_product(session, ref)
    series = await price_service.price_series(
        session,
        product_id=product.id,
        days=days,
        market_id=_uuid.UUID(market_id) if market_id else None,
        country_code=country_code,
    )
    return _series_out(series)


@router.get("/{ref}/markets", response_model=list[MarketOut], summary="有此品項資料的市場")
async def product_markets(ref: str, session: DbSession) -> list[MarketOut]:
    product = await catalog_service.resolve_product(session, ref)
    markets = await price_service.market_coverage(session, product.id)
    return [MarketOut.from_model(m) for m in markets]


def _series_out(series: price_service.PriceSeries) -> PriceSeriesOut:
    return PriceSeriesOut(
        product_id=series.product_id,
        market_id=series.market_id,
        currency=series.currency,
        unit=series.unit,
        change_pct=series.change_pct(),
        points=[
            PricePointOut(d=p.trade_date, avg=p.price_avg, high=p.price_high, low=p.price_low, vol=p.volume)
            for p in series.points
        ],
    )
