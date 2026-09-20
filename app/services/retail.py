"""消費者回報的零售價：寫入、聚合、與批發價的產銷價差。

防濫用的組成跟意向價格（`app/services/intents.py`）刻意共用——信譽權重、
倍率護欄、機房 IP 偵測都是同一套，因為判斷的是**同一個人**可不可信。
一個在意向價格灌水的帳號，回報零售價時同樣不該被採信。

但有兩處刻意不同：

1. **沒有成本底線檢查。** 意向價低於產地成本代表惡意壓價；零售價低
   只是看到特價，那是真實資訊，擋掉會讓看板失真。
2. **不做「一人一筆」。** 意向是「我願意出多少」，同一作物只能有一個；
   零售回報是觀察，同一個人本來就可能在三家店看到三個價格。改成
   **同一人 × 同一品項 × 同一店家**在冷卻期內只收一筆。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import country_scope
from app.core.config import settings
from app.core.errors import AppError, ForbiddenError, NotFoundError
from app.models.catalog import Market, Product
from app.models.enums import RetailExclusion, RetailReportStatus, StoreType
from app.models.price import OfficialPrice
from app.models.retail import RetailPriceReport
from app.models.user import User
from app.services import statistics as st
from app.services.intents import (
    _default_currency,
    _ip_reputation,
    _user_region,
    get_or_create_reputation,
)

__all__ = [
    "RetailSpread",
    "RetailSummary",
    "StaleObservationError",
    "StoreCooldownError",
    "list_mine",
    "list_reports",
    "spread",
    "store_key_of",
    "submit",
    "unit_price_of",
    "summarise",
    "withdraw",
]

_CENT = Decimal("0.01")
_WS = re.compile(r"\s+")


class StoreCooldownError(AppError):
    status_code = 429
    code = "retail_store_cooldown"
    message = "這家店的這個作物你最近才回報過"


class StaleObservationError(AppError):
    status_code = 400
    code = "retail_observation_too_old"
    message = "看到價格的日期太久以前，無法回報"


def unit_price_of(observed_price: Decimal, pack_size: Decimal | None) -> Decimal:
    """把標籤上的價格換算成單位價。

    超市標的是「500g／₹40」，要使用者自己心算成每公斤多少一定會錯，
    所以讓他照標籤填，換算放在這裡。沒填包裝規格時標價本身就是單位價。
    """
    if pack_size is None or pack_size <= 0:
        return observed_price
    return (observed_price / pack_size).quantize(_CENT, rounding=ROUND_HALF_UP)


def store_key_of(store_name: str, branch: str | None = None) -> str:
    """把店名正規化成識別鍵。

    「Big Bazaar」「big  bazaar 」要算同一家，否則冷卻期形同虛設——
    改個大小寫就能重複灌同一家店的價格。
    """
    parts = [store_name]
    if branch:
        parts.append(branch)
    joined = " ".join(p for p in parts if p)
    return _WS.sub(" ", joined).strip().lower()[:160]


# ---------------------------------------------------------------------------
# 寫入
# ---------------------------------------------------------------------------


async def recent_report(
    session: AsyncSession, user_id: uuid.UUID, product_id: uuid.UUID, store_key: str
) -> RetailPriceReport | None:
    stmt = (
        select(RetailPriceReport)
        .where(
            RetailPriceReport.user_id == user_id,
            RetailPriceReport.product_id == product_id,
            RetailPriceReport.store_key == store_key,
            RetailPriceReport.status == RetailReportStatus.ACTIVE,
        )
        .order_by(RetailPriceReport.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def cooldown_remaining(
    session: AsyncSession, user_id: uuid.UUID, product_id: uuid.UUID, store_key: str
) -> int:
    """同店同品項的冷卻剩餘秒數。0 代表可以回報。"""
    last = await recent_report(session, user_id, product_id, store_key)
    if last is None:
        return 0
    elapsed = (datetime.now(UTC) - last.created_at).total_seconds()
    window = settings.retail_cooldown_hours * 3600
    return max(0, int(window - elapsed))


async def submit(
    session: AsyncSession,
    user: User,
    product: Product,
    *,
    observed_price: Decimal,
    store_name: str,
    store_type: StoreType = StoreType.SUPERMARKET,
    store_branch: str | None = None,
    pack_size: Decimal | None = None,
    unit: str | None = None,
    currency: str | None = None,
    observed_on: date | None = None,
    is_promotion: bool = False,
    location_text: str | None = None,
    photo_url: str | None = None,
    note: str | None = None,
    client_ip: str | None = None,
) -> RetailPriceReport:
    """寫入一筆零售價回報。

    跟意向價格一樣，被判定為不可信的**仍然寫入且回 201**，只是標記
    `excluded_reason` 不計入聚合。讓對方以為成功了，才不會立刻換帳號重來。
    """
    today = datetime.now(UTC).date()
    observed_on = observed_on or today
    if observed_on > today:
        raise StaleObservationError("看到價格的日期不能是未來", code="retail_observation_future")
    if (today - observed_on).days > settings.retail_max_observation_age_days:
        raise StaleObservationError(
            details={
                "max_age_days": settings.retail_max_observation_age_days,
                "observed_on": observed_on.isoformat(),
            }
        )

    key = store_key_of(store_name, store_branch)
    remaining = await cooldown_remaining(session, user.id, product.id, key)
    if remaining > 0:
        raise StoreCooldownError(
            details={
                "retry_after": remaining,
                "cooldown_hours": settings.retail_cooldown_hours,
                "store": store_name,
            }
        )

    rep = await get_or_create_reputation(session, user.id)
    hosting, proxy = await _ip_reputation(client_ip)

    excluded: RetailExclusion | None = None
    if rep.is_shadow_banned:
        excluded = RetailExclusion.SHADOWED
    elif hosting or proxy:
        excluded = RetailExclusion.UNTRUSTED_IP
    elif rep.weight <= 0:
        excluded = RetailExclusion.ZERO_WEIGHT

    unit_price = unit_price_of(observed_price, pack_size)

    report = RetailPriceReport(
        user_id=user.id,
        product_id=product.id,
        observed_price=observed_price,
        pack_size=pack_size,
        unit_price=unit_price,
        currency=(currency or _default_currency(user)).upper(),
        unit=unit or product.default_unit,
        is_promotion=is_promotion,
        store_type=store_type,
        store_name=store_name.strip(),
        store_branch=(store_branch or "").strip() or None,
        store_key=key,
        country_code=user.country_code,
        subdivision_code=user.subdivision_code,
        region=_user_region(user),
        location_text=location_text,
        latitude=user.latitude,
        longitude=user.longitude,
        observed_on=observed_on,
        photo_url=photo_url,
        note=note,
        status=RetailReportStatus.ACTIVE,
        excluded_reason=excluded,
        weight_snapshot=rep.weight,
        ip_hosting=hosting,
        ip_proxy=proxy,
    )
    session.add(report)
    await session.flush()
    return report


async def withdraw(
    session: AsyncSession, user: User, report_id: uuid.UUID
) -> RetailPriceReport:
    report = await session.get(RetailPriceReport, report_id)
    if report is None:
        raise NotFoundError("Retail report not found", code="retail_report_not_found")
    if report.user_id != user.id:
        raise ForbiddenError("只能撤回自己的回報", code="not_report_owner")
    report.status = RetailReportStatus.WITHDRAWN
    await session.flush()
    return report


# ---------------------------------------------------------------------------
# 查詢
# ---------------------------------------------------------------------------


def _base_query(
    product_id: uuid.UUID,
    *,
    since: date,
    region: str | None,
    subdivision_code: str | None,
    country_code: str | None,
    store_type: StoreType | None,
    include_promotions: bool,
) -> Select:
    stmt = select(RetailPriceReport).where(
        RetailPriceReport.product_id == product_id,
        RetailPriceReport.status == RetailReportStatus.ACTIVE,
        RetailPriceReport.observed_on >= since,
    )
    if subdivision_code:
        stmt = stmt.where(RetailPriceReport.subdivision_code == subdivision_code.upper())
    if region:
        stmt = stmt.where(RetailPriceReport.region == region)
    if country_code:
        stmt = stmt.where(RetailPriceReport.country_code == country_code.upper())
    if store_type is not None:
        stmt = stmt.where(RetailPriceReport.store_type == store_type)
    if not include_promotions:
        stmt = stmt.where(RetailPriceReport.is_promotion.is_(False))
    return country_scope.apply(stmt, RetailPriceReport.country_code)


async def list_reports(
    session: AsyncSession,
    product: Product,
    *,
    days: int | None = None,
    region: str | None = None,
    subdivision_code: str | None = None,
    country_code: str | None = None,
    store_type: StoreType | None = None,
    include_promotions: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RetailPriceReport], int]:
    """近期回報的清單。**單筆公開**——比價本來就是這個功能的重點。"""
    days = days or settings.retail_lookback_days
    since = datetime.now(UTC).date() - timedelta(days=days)
    stmt = _base_query(
        product.id,
        since=since,
        region=region,
        subdivision_code=subdivision_code,
        country_code=country_code,
        store_type=store_type,
        include_promotions=include_promotions,
    )
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.scalar(count_stmt)) or 0
    stmt = (
        stmt.options(selectinload(RetailPriceReport.user))
        .order_by(RetailPriceReport.observed_on.desc(), RetailPriceReport.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars()), total


async def list_mine(
    session: AsyncSession, user: User, *, include_withdrawn: bool = False, limit: int = 50
) -> list[RetailPriceReport]:
    stmt = select(RetailPriceReport).where(RetailPriceReport.user_id == user.id)
    if not include_withdrawn:
        stmt = stmt.where(RetailPriceReport.status == RetailReportStatus.ACTIVE)
    stmt = stmt.order_by(RetailPriceReport.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


@dataclass
class StoreBreakdown:
    store_type: StoreType
    median: Decimal | None
    sample_count: int


@dataclass
class RetailSummary:
    product_id: uuid.UUID
    region: str | None
    country_code: str | None
    currency: str | None
    unit: str | None
    days: int

    typical_price: Decimal | None      # 信譽加權中位數，要顯示的就是這個
    median: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None
    q1: Decimal | None
    q3: Decimal | None

    sample_count: int
    submitted_count: int
    excluded_count: int
    store_count: int
    outlier_filter_active: bool
    min_samples_for_outlier_filter: int
    exclusions: dict[str, int] = field(default_factory=dict)
    by_store_type: list[StoreBreakdown] = field(default_factory=list)


async def summarise(
    session: AsyncSession,
    product: Product,
    *,
    days: int | None = None,
    region: str | None = None,
    subdivision_code: str | None = None,
    country_code: str | None = None,
    include_promotions: bool = False,
) -> RetailSummary:
    """算出零售價看板。

    離群排除沿用意向價格那套**倍率護欄**（偏離中位數 5 倍以上排除）。
    這裡不做 IQR：零售價本來就會因通路而差距很大（便利商店是量販店的
    兩倍很正常），IQR 會把整個通路的樣本當成離群值砍掉。跨通路的差異
    改用 `by_store_type` 分開呈現，而不是抹平。

    促銷價預設**不計入**主要統計——特價是短暫的，混進去會讓「平常大概
    多少錢」失真。要看促銷請傳 `include_promotions=True`。
    """
    from app.services.intents import ratio_guard_bounds

    days = days or settings.retail_lookback_days
    since = datetime.now(UTC).date() - timedelta(days=days)
    stmt = _base_query(
        product.id,
        since=since,
        region=region,
        subdivision_code=subdivision_code,
        country_code=country_code,
        store_type=None,
        include_promotions=include_promotions,
    )
    reports = list((await session.execute(stmt)).scalars())
    submitted = len(reports)

    exclusions: dict[str, int] = {}
    candidates: list[RetailPriceReport] = []
    for r in reports:
        if r.excluded_reason is not None and r.excluded_reason is not RetailExclusion.OUTLIER:
            exclusions[r.excluded_reason.value] = exclusions.get(r.excluded_reason.value, 0) + 1
            continue
        candidates.append(r)

    if not candidates:
        return RetailSummary(
            product_id=product.id, region=region, country_code=country_code,
            currency=None, unit=None, days=days,
            typical_price=None, median=None, min_price=None, max_price=None,
            q1=None, q3=None,
            sample_count=0, submitted_count=submitted, excluded_count=submitted,
            store_count=0, outlier_filter_active=False,
            min_samples_for_outlier_filter=settings.intent_min_samples_for_ratio_guard,
            exclusions=exclusions,
        )

    guard = ratio_guard_bounds([r.unit_price for r in candidates])
    kept: list[RetailPriceReport] = []
    outlier_ids: list[uuid.UUID] = []
    for r in candidates:
        if guard is not None and not (guard[0] <= r.unit_price <= guard[1]):
            outlier_ids.append(r.id)
            exclusions[RetailExclusion.OUTLIER.value] = (
                exclusions.get(RetailExclusion.OUTLIER.value, 0) + 1
            )
            continue
        kept.append(r)

    await _mark_outliers(session, outlier_ids, kept_ids=[r.id for r in kept])

    prices = [r.unit_price for r in kept]
    quart = st.quartiles(prices)
    weighted = [(r.unit_price, max(0.0, r.weight_snapshot)) for r in kept]

    by_type: dict[StoreType, list[Decimal]] = {}
    for r in kept:
        by_type.setdefault(r.store_type, []).append(r.unit_price)

    return RetailSummary(
        product_id=product.id,
        region=region,
        country_code=country_code,
        currency=kept[0].currency,
        unit=kept[0].unit,
        days=days,
        typical_price=_q(st.weighted_median(weighted)),
        median=_q(st.median(prices)),
        min_price=_q(min(prices)),
        max_price=_q(max(prices)),
        q1=_q(quart.q1) if quart else None,
        q3=_q(quart.q3) if quart else None,
        sample_count=len(kept),
        submitted_count=submitted,
        excluded_count=submitted - len(kept),
        store_count=len({r.store_key for r in kept}),
        outlier_filter_active=guard is not None,
        min_samples_for_outlier_filter=settings.intent_min_samples_for_ratio_guard,
        exclusions=exclusions,
        by_store_type=[
            StoreBreakdown(store_type=t, median=_q(st.median(v)), sample_count=len(v))
            for t, v in sorted(by_type.items(), key=lambda kv: kv[0].value)
        ],
    )


async def _mark_outliers(
    session: AsyncSession, outlier_ids: list[uuid.UUID], *, kept_ids: list[uuid.UUID]
) -> None:
    """把離群判定寫回資料庫，使用者才查得到自己的回報為什麼沒被計入。"""
    if outlier_ids:
        await session.execute(
            update(RetailPriceReport)
            .where(RetailPriceReport.id.in_(outlier_ids))
            .values(excluded_reason=RetailExclusion.OUTLIER)
        )
    if kept_ids:
        # 樣本變動後可能不再是離群值，要能翻案
        await session.execute(
            update(RetailPriceReport)
            .where(
                RetailPriceReport.id.in_(kept_ids),
                RetailPriceReport.excluded_reason == RetailExclusion.OUTLIER,
            )
            .values(excluded_reason=None)
        )


# ---------------------------------------------------------------------------
# 產銷價差
# ---------------------------------------------------------------------------


@dataclass
class RetailSpread:
    """從批發到零售中間差了多少。

    這是整個零售回報功能存在的理由：批發價是公開資料，零售價過去只有
    走一趟超市才知道，中間的價差也就無從討論。兩邊湊齊才看得出來
    「產地跌了三成，架上為什麼沒動」。
    """

    product_id: uuid.UUID
    region: str | None
    currency: str | None
    unit: str | None
    retail_price: Decimal | None
    wholesale_price: Decimal | None
    spread: Decimal | None            # 零售 - 批發
    spread_pct: Decimal | None        # 相對批發價的百分比
    retail_samples: int
    wholesale_days: int
    wholesale_source: str | None      # region / country / null


async def spread(
    session: AsyncSession,
    product: Product,
    *,
    days: int | None = None,
    region: str | None = None,
    subdivision_code: str | None = None,
    country_code: str | None = None,
) -> RetailSpread:
    """零售價與同期官方批發價的差距。

    批發價的取樣範圍會**逐步放寬**：先找同區域的市場，沒有就退回全國。
    這個退回是必要的——零售回報的 `region` 來自使用者檔案的 ISO 行政區，
    而市場的 `region` 是資料源自訂的字串，兩個命名空間不保證一致
    （`臺北市` vs `台北市`）。不退回的話大多數品項都算不出價差。
    """
    days = days or settings.retail_lookback_days
    summary = await summarise(
        session,
        product,
        days=days,
        region=region,
        subdivision_code=subdivision_code,
        country_code=country_code,
    )
    retail = summary.typical_price

    since = datetime.now(UTC).date() - timedelta(days=days)

    async def _wholesale(with_region: str | None) -> Decimal | None:
        stmt = (
            select(OfficialPrice.price_avg)
            .join(Market, Market.id == OfficialPrice.market_id)
            .where(
                OfficialPrice.product_id == product.id,
                OfficialPrice.trade_date >= since,
                OfficialPrice.price_avg.is_not(None),
            )
        )
        if with_region:
            stmt = stmt.where(Market.region == with_region)
        if country_code:
            stmt = stmt.where(Market.country_code == country_code.upper())
        stmt = country_scope.apply(stmt, Market.country_code)
        values = [v for v in (await session.execute(stmt)).scalars() if v is not None]
        return st.median(values)

    source: str | None = None
    wholesale = None
    if region:
        wholesale = await _wholesale(region)
        if wholesale is not None:
            source = "region"
    if wholesale is None:
        wholesale = await _wholesale(None)
        if wholesale is not None:
            source = "country"

    diff = pct = None
    if retail is not None and wholesale is not None and wholesale > 0:
        diff = _q(retail - wholesale)
        pct = _q((retail - wholesale) / wholesale * 100)

    return RetailSpread(
        product_id=product.id,
        region=region,
        currency=summary.currency,
        unit=summary.unit,
        retail_price=retail,
        wholesale_price=_q(wholesale),
        spread=diff,
        spread_pct=pct,
        retail_samples=summary.sample_count,
        wholesale_days=days,
        wholesale_source=source,
    )


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP) if value is not None else None
