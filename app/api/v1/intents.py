"""消費者意向價格端點。

消費者回報「我願意用多少錢買」，平台聚合成區域錨點。
因為平台不涉入金流、亂填的成本是零，所有寫入都要過四層防護
（見 `app/services/intents.py` 與 `../code_artifact.md`）。

單筆意向**只有本人看得到**；對外一律只給聚合後的看板。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.deps import AdminGuard, ClientIp, CurrentUser, DbSession, Locale, Paging
from app.core.errors import NotFoundError
from app.core.pagination import Page
from app.models.catalog import Product
from app.models.intent import IntentNotification, PriceIntent
from app.models.quote import Quote
from app.schemas.catalog import ProductOut
from app.schemas.intent import (
    IntentCreate,
    IntentOut,
    IntentSummaryOut,
    NotificationOut,
    NotificationResponseIn,
    OfferMatchListOut,
    OfferMatchOut,
    PriceFloorOut,
    ReputationOut,
)
from app.services import catalog as catalog_service
from app.services import intents as intent_service

# 掛在 /products 底下：意向是「某個作物的」，不是獨立資源
product_router = APIRouter(prefix="/products", tags=["intents"])
# 本人的意向與信譽
me_router = APIRouter(prefix="/me", tags=["intents"])
# 產地開團的比對，需要 admin token（正式環境應改成小農本人可呼叫）
admin_router = APIRouter(prefix="/admin", tags=["intents"], dependencies=[AdminGuard])


# ---------------------------------------------------------------------------
# 看板與底線（公開）
# ---------------------------------------------------------------------------


@product_router.get(
    "/{ref}/intents/summary",
    response_model=IntentSummaryOut,
    summary="區域意向看板",
)
async def intent_summary(
    ref: str,
    session: DbSession,
    locale: Locale,
    region: Annotated[str | None, Query(description="區域，省略則看全國")] = None,
    country_code: Annotated[str | None, Query()] = None,
) -> IntentSummaryOut:
    """消費者意向價格的聚合結果。

    **錨點是信譽加權的中位數**，不是算術平均——平均值一筆惡意的 1 元就能拉垮。
    離群值（IQR 容許區間外）、影子封禁與機房 IP 的提交都已排除，
    `exclusions` 會說明各排除了幾筆。
    """
    product = await catalog_service.resolve_product(session, ref)
    summary = await intent_service.summarise(
        session, product, region=region, country_code=country_code
    )
    return IntentSummaryOut.from_summary(summary, ProductOut.from_model(product, locale))


@product_router.get(
    "/{ref}/intents/floor", response_model=PriceFloorOut, summary="意向價格的成本底線"
)
async def intent_floor(
    ref: str,
    session: DbSession,
    region: Annotated[str | None, Query()] = None,
    country_code: Annotated[str | None, Query()] = None,
) -> PriceFloorOut:
    """輸入框旁邊先問這支，就能在送出前擋下過低的出價。

    底線目前是由**近 7 日官方批發行情**推算的替代值
    （`source = official_price_proxy`）——真正的「公定生產成本」資料源
    還沒有。沒有官方行情時 `floor_price` 為 null，代表不設限。
    """
    product = await catalog_service.resolve_product(session, ref)
    floor = await intent_service.price_floor(
        session, product, country_code=country_code, region=region
    )
    return PriceFloorOut.from_model(floor)


# ---------------------------------------------------------------------------
# 提交（需登入）
# ---------------------------------------------------------------------------


@product_router.post(
    "/{ref}/intents",
    response_model=IntentOut,
    status_code=status.HTTP_201_CREATED,
    summary="提出我的期望價格",
)
async def submit_intent(
    ref: str,
    payload: IntentCreate,
    user: CurrentUser,
    session: DbSession,
    locale: Locale,
    ip: ClientIp,
) -> IntentOut:
    """消費者回報願意出的價格。**任何登入者都能提**，不需要小農／盤商身分。

    會依序檢查：

    1. 個人檔案要有所在地區（否則歸不到任何看板）→ 400 `intent_region_required`
    2. 同一作物的冷卻期（預設 7 天）→ 429 `intent_cooldown`
    3. 不得低於成本底線 → 400 `intent_below_floor`，`details` 帶底線價

    通過後才寫入。若來源是機房 / Proxy IP，或帳號已被影子封禁，
    **仍然寫入且回 201**，但 `excluded_reason` 會標記，聚合時不計入。
    這是刻意的：讓對方以為成功了，才不會立刻換帳號重來。

    同一作物再次提交會取代舊的（舊的轉 `superseded`），不是累加。
    """
    product = await catalog_service.resolve_product(session, ref)
    intent = await intent_service.submit(
        session,
        user,
        product,
        price=payload.price,
        quantity=payload.quantity,
        unit=payload.unit,
        currency=payload.currency,
        note=payload.note,
        client_ip=ip,
    )
    intent.product = product
    return IntentOut.from_model(intent, locale)


@me_router.get("/intents", response_model=Page[IntentOut], summary="我的意向價格")
async def my_intents(
    user: CurrentUser,
    session: DbSession,
    paging: Paging,
    locale: Locale,
    include_history: Annotated[
        bool, Query(description="含已被取代 / 已撤回的")
    ] = False,
) -> Page[IntentOut]:
    """自己填過的期望價格。`excluded_reason` 會說明為什麼沒被計入看板。"""
    from sqlalchemy import func

    from app.models.enums import IntentStatus

    stmt = (
        select(PriceIntent)
        .options(selectinload(PriceIntent.product).selectinload(Product.names))
        .where(PriceIntent.user_id == user.id)
        .order_by(PriceIntent.created_at.desc())
    )
    count_stmt = select(func.count()).select_from(PriceIntent).where(
        PriceIntent.user_id == user.id
    )
    if not include_history:
        stmt = stmt.where(PriceIntent.status == IntentStatus.ACTIVE)
        count_stmt = count_stmt.where(PriceIntent.status == IntentStatus.ACTIVE)

    rows = list(
        (await session.execute(stmt.limit(paging.limit).offset(paging.offset))).scalars()
    )
    total = (await session.scalar(count_stmt)) or 0
    return Page.build([IntentOut.from_model(i, locale) for i in rows], total, paging)


@me_router.delete(
    "/intents/{intent_id}", response_model=IntentOut, summary="撤回意向價格"
)
async def withdraw_intent(
    intent_id: uuid.UUID, user: CurrentUser, session: DbSession, locale: Locale
) -> IntentOut:
    intent = await intent_service.withdraw(session, user, intent_id)
    product = await catalog_service.get_product(session, intent.product_id)
    intent.product = product
    return IntentOut.from_model(intent, locale)


@me_router.get("/reputation", response_model=ReputationOut, summary="我的信譽狀態")
async def my_reputation(user: CurrentUser, session: DbSession) -> ReputationOut:
    """權重越高，你的意向對看板的影響力越大。

    回應**不包含**影子封禁狀態——那是刻意的，見 `app/models/intent.py`。
    """
    rep = await intent_service.get_or_create_reputation(session, user.id)
    return ReputationOut(
        weight=rep.weight,
        samples=rep.samples,
        hits=rep.hits,
        misses=rep.misses,
        has_verified_purchase=rep.has_verified_purchase,
    )


# ---------------------------------------------------------------------------
# 優先通知與響應（PRD 4.1 / 4.2）
# ---------------------------------------------------------------------------


@me_router.get(
    "/notifications", response_model=Page[NotificationOut], summary="產地開團通知"
)
async def my_notifications(
    user: CurrentUser, session: DbSession, paging: Paging, locale: Locale
) -> Page[NotificationOut]:
    from sqlalchemy import func

    stmt = (
        select(IntentNotification)
        .options(selectinload(IntentNotification.product).selectinload(Product.names))
        .where(IntentNotification.user_id == user.id)
        .order_by(IntentNotification.sent_at.desc())
    )
    rows = list(
        (await session.execute(stmt.limit(paging.limit).offset(paging.offset))).scalars()
    )
    total = (
        await session.scalar(
            select(func.count())
            .select_from(IntentNotification)
            .where(IntentNotification.user_id == user.id)
        )
    ) or 0
    return Page.build([NotificationOut.from_model(r, locale) for r in rows], total, paging)


@me_router.post(
    "/notifications/{notification_id}/respond",
    response_model=NotificationOut,
    summary="回報已讀 / 已點擊",
)
async def respond_notification(
    notification_id: uuid.UUID,
    payload: NotificationResponseIn,
    user: CurrentUser,
    session: DbSession,
    locale: Locale,
) -> NotificationOut:
    """前端在使用者開啟或點擊通知時打這支。

    多次收到推播卻完全零響應的帳號，排程會判定為「虛假幽靈需求」並扣信譽
    （PRD 4.2）。所以這支不只是統計，會實際影響權重。
    """
    row = await intent_service.record_response(
        session, user, notification_id, clicked=payload.clicked
    )
    product = await catalog_service.get_product(session, row.product_id)
    row.product = product
    return NotificationOut.from_model(row, locale)


@admin_router.post(
    "/quotes/{quote_id}/match-intents",
    response_model=OfferMatchListOut,
    summary="產地開團：找出該優先通知誰",
)
async def match_intents(
    quote_id: uuid.UUID,
    session: DbSession,
    record: Annotated[
        bool, Query(description="true = 同時寫進通知紀錄，供之後追蹤響應率")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> OfferMatchListOut:
    """小農依看板開出價格後，找出意向價 >= 開價的使用者。

    **依意向價由低到高排序**：填的價格越貼近產地實際開價的人越先拿到配額。
    這是 PRD 要的博弈方向——虛報低價進不了名單，虛報高價也搶不到優先權。

    這裡只做比對與紀錄，**不負責實際發送**（推播管道還沒接）。
    """
    quote = await session.get(Quote, quote_id)
    if quote is None:
        raise NotFoundError("Quote not found", code="quote_not_found")

    matches = await intent_service.match_offer(session, quote, limit=limit)
    if record and matches:
        await intent_service.record_notifications(session, quote, matches)

    return OfferMatchListOut(
        quote_id=quote.id,
        offer_price=quote.price,
        currency=quote.currency,
        unit=quote.unit,
        matched=len(matches),
        recorded=record and bool(matches),
        items=[
            OfferMatchOut(
                user_id=m.user_id, display_name=m.display_name, intent_price=m.intent_price
            )
            for m in matches
        ],
    )


@admin_router.post(
    "/products/{ref}/intents/recompute-reputation", summary="重算信譽權重"
)
async def recompute_reputation(
    ref: str,
    session: DbSession,
    region: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """依最新共識重算這個作物的參與者信譽。正式環境應由排程定期跑。"""
    product = await catalog_service.resolve_product(session, ref)
    changes = await intent_service.update_reputation(session, product, region=region)
    return {
        "product": product.slug,
        "region": region,
        "updated": len(changes),
        "changes": [
            {
                "user_id": str(c.user_id),
                "before": round(c.before, 3),
                "after": round(c.after, 3),
                "verdict": c.verdict,
                "shadow_banned": c.shadow_banned,
            }
            for c in changes
        ],
    }
