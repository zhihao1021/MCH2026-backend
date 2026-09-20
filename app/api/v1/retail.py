"""消費者回報的超市零售價。

消費者把在通路看到的標價拍下來回報，平台聚合成「這個作物在這一區
大概賣多少」，並與官方批發行情對照算出**產銷價差**。

跟第 9 節的意向價格是兩回事：意向是「我願意出多少」（意願），
這裡是「我看到店裡標多少」（觀察）。也因此**單筆是公開的**——
比價正是這個功能的重點，藏起來就沒有意義了。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import ClientIp, CurrentUser, DbSession, Locale, OptionalUser, Paging
from app.core.pagination import Page
from app.models.enums import StoreType
from app.schemas.catalog import ProductOut
from app.schemas.retail import (
    RetailReportCreate,
    RetailReportOut,
    RetailSpreadOut,
    RetailSummaryOut,
)
from app.services import catalog as catalog_service
from app.services import retail as retail_service

# 掛在 /products 底下：零售價是「某個作物的」，不是獨立資源
product_router = APIRouter(prefix="/products", tags=["retail"])
me_router = APIRouter(prefix="/me", tags=["retail"])


# ---------------------------------------------------------------------------
# 看板（公開）
# ---------------------------------------------------------------------------


@product_router.get(
    "/{ref}/retail-prices/summary",
    response_model=RetailSummaryOut,
    summary="零售價看板",
)
async def retail_summary(
    ref: str,
    session: DbSession,
    locale: Locale,
    days: Annotated[int | None, Query(ge=1, le=365, description="統計近幾天，預設 14")] = None,
    region: Annotated[str | None, Query(description="地區顯示名")] = None,
    subdivision_code: Annotated[
        str | None, Query(description="ISO 3166-2。**精確篩選請用這個**")
    ] = None,
    country_code: Annotated[str | None, Query()] = None,
    include_promotions: Annotated[
        bool, Query(description="是否把特價一併計入。預設不計入")
    ] = False,
) -> RetailSummaryOut:
    """公開端點。`region` 省略則看全國。

    **特價預設不計入**——促銷是短暫的，混進去會讓「平常大概多少錢」失真。
    `by_store_type` 會分通路呈現；便利商店是量販店的兩倍很正常，
    把兩者平均掉的話哪一邊都不像。
    """
    product = await catalog_service.resolve_product(session, ref)
    summary = await retail_service.summarise(
        session,
        product,
        days=days,
        region=region,
        subdivision_code=subdivision_code,
        country_code=country_code,
        include_promotions=include_promotions,
    )
    return RetailSummaryOut.from_summary(summary, ProductOut.from_model(product, locale))


@product_router.get(
    "/{ref}/retail-prices/spread",
    response_model=RetailSpreadOut,
    summary="產銷價差",
)
async def retail_spread(
    ref: str,
    session: DbSession,
    locale: Locale,
    days: Annotated[int | None, Query(ge=1, le=365)] = None,
    region: Annotated[str | None, Query()] = None,
    subdivision_code: Annotated[str | None, Query()] = None,
    country_code: Annotated[str | None, Query()] = None,
) -> RetailSpreadOut:
    """零售價與同期官方批發價的差距。

    批發價的取樣範圍會逐步放寬：先找同區域的市場，沒有就退回全國，
    `wholesale_source` 會說明用了哪一層。會退回是因為零售回報的 `region`
    來自使用者檔案的 ISO 行政區，而市場的 `region` 是資料源自訂字串，
    兩個命名空間不保證一致。

    `wholesale_source` 為 `null` 代表查無官方行情，這時 `spread` 也會是
    `null`——**不要當成價差為零**。
    """
    product = await catalog_service.resolve_product(session, ref)
    result = await retail_service.spread(
        session,
        product,
        days=days,
        region=region,
        subdivision_code=subdivision_code,
        country_code=country_code,
    )
    return RetailSpreadOut.from_model(result, ProductOut.from_model(product, locale))


@product_router.get(
    "/{ref}/retail-prices",
    response_model=Page[RetailReportOut],
    summary="零售回報清單",
)
async def list_retail_reports(
    ref: str,
    session: DbSession,
    paging: Paging,
    viewer: OptionalUser,
    days: Annotated[int | None, Query(ge=1, le=365)] = None,
    region: Annotated[str | None, Query()] = None,
    subdivision_code: Annotated[str | None, Query()] = None,
    country_code: Annotated[str | None, Query()] = None,
    store_type: Annotated[StoreType | None, Query(description="只看某種通路")] = None,
    include_promotions: Annotated[bool, Query()] = True,
) -> Page[RetailReportOut]:
    """近期回報的單筆清單，供使用者比價。

    公開的只有店家與價格；回報者僅顯示暱稱，`excluded_reason` 只有
    本人看得到。
    """
    product = await catalog_service.resolve_product(session, ref)
    items, total = await retail_service.list_reports(
        session,
        product,
        days=days,
        region=region,
        subdivision_code=subdivision_code,
        country_code=country_code,
        store_type=store_type,
        include_promotions=include_promotions,
        limit=paging.limit,
        offset=paging.offset,
    )
    viewer_id = viewer.id if viewer else None
    return Page.build(
        [RetailReportOut.from_model(r, viewer_id=viewer_id) for r in items], total, paging
    )


# ---------------------------------------------------------------------------
# 回報
# ---------------------------------------------------------------------------


@product_router.post(
    "/{ref}/retail-prices",
    response_model=RetailReportOut,
    status_code=status.HTTP_201_CREATED,
    summary="回報我看到的零售價",
)
async def create_retail_report(
    ref: str,
    payload: RetailReportCreate,
    user: CurrentUser,
    session: DbSession,
    client_ip: ClientIp,
) -> RetailReportOut:
    """**任何登入者都能回報**，不需要小農／盤商身分。

    | HTTP | code | 說明 |
    | --- | --- | --- |
    | 400 | `retail_observation_future` | 看到價格的日期是未來 |
    | 400 | `retail_observation_too_old` | 超過可補登的天數，`details.max_age_days` |
    | 429 | `retail_store_cooldown` | 同一店家同一作物的冷卻期，`details.retry_after` 是秒數 |

    通過檢查不代表一定計入看板：來源是機房／VPN IP，或帳號已被影子封禁時
    **仍然回 201 且本人看得到自己的數字**，只是 `excluded_reason` 會標記、
    聚合時不算。
    """
    product = await catalog_service.resolve_product(session, ref)
    report = await retail_service.submit(
        session,
        user,
        product,
        observed_price=payload.observed_price,
        store_name=payload.store_name,
        store_type=payload.store_type,
        store_branch=payload.store_branch,
        pack_size=payload.pack_size,
        unit=payload.unit,
        currency=payload.currency,
        observed_on=payload.observed_on,
        is_promotion=payload.is_promotion,
        location_text=payload.location_text,
        photo_url=payload.photo_url,
        note=payload.note,
        client_ip=client_ip,
    )
    await session.commit()
    await session.refresh(report, ["user"])
    return RetailReportOut.from_model(report, viewer_id=user.id)


# ---------------------------------------------------------------------------
# 我的回報
# ---------------------------------------------------------------------------


@me_router.get(
    "/retail-prices", response_model=list[RetailReportOut], summary="我回報過的零售價"
)
async def list_my_retail_reports(
    user: CurrentUser,
    session: DbSession,
    include_withdrawn: Annotated[bool, Query()] = False,
) -> list[RetailReportOut]:
    reports = await retail_service.list_mine(
        session, user, include_withdrawn=include_withdrawn
    )
    for r in reports:
        r.user = user
    return [RetailReportOut.from_model(r, viewer_id=user.id) for r in reports]


@me_router.delete(
    "/retail-prices/{report_id}", response_model=RetailReportOut, summary="撤回回報"
)
async def withdraw_retail_report(
    report_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> RetailReportOut:
    report = await retail_service.withdraw(session, user, report_id)
    await session.commit()
    report.user = user
    return RetailReportOut.from_model(report, viewer_id=user.id)
