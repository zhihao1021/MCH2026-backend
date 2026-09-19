"""消費者意向價格：提交、聚合與防刷。

對應 `../code_artifact.md` 的四大防護機制。平台不涉入金流，惡意填一個
超低價的成本是零，所以每一層都要擋一點：

| PRD | 這裡的實作 |
| --- | --- |
| 1.1 強健統計 | `summarise()` 用中位數／截尾均值，IQR 剔離群值 |
| 1.2 成本硬約束 | `floor_price()` 由近 7 日官方行情推算，低於底線降權隔離 |
| 2.1 單一門號 | 沿用既有的手機 OTP（users.phone 唯一） |
| 2.2 冷卻期 | `submit()` 檢查同人同品項的上次提交時間 |
| 2.2 地理圍欄 | `submit()` 比對使用者所在地與看板區域 |
| 2.2 機房 IP | `submit()` 查 geoip 的 hosting/proxy 旗標，命中即權重歸零 |
| 3.1 信譽權重 | `update_reputation()` 依共識吻合度升降 |
| 3.2 影子封禁 | 被封的照樣寫得進去，但 `summarise()` 完全忽略 |
| 4.1 優先通知 | `match_offer()` 找出該通知誰 |
| 4.2 響應追蹤 | `record_response()` 記錄點擊，無響應會扣信譽 |
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import country_scope
from app.core.config import settings
from app.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.models.catalog import Market, Product
from app.models.enums import IntentExclusion, IntentStatus, QuoteStatus
from app.models.intent import IntentNotification, PriceIntent, UserReputation
from app.models.price import OfficialPrice
from app.models.quote import Quote
from app.models.user import User
from app.services import statistics as st


class IntentCooldownError(AppError):
    """同一品項還在冷卻期內。"""

    status_code = 429
    code = "intent_cooldown"
    message = "這個作物的意向價格還在冷卻期，暫時不能修改"


class BelowFloorError(AppError):
    """低於產銷成本底線。

    PRD 要求「拒絕寫入或降權隔離」。這裡選**拒絕寫入**並回傳底線價，
    讓前端能直接顯示提示語——資料連進都沒進，比事後降權乾淨。
    """

    code = "intent_below_floor"
    message = (
        "該出價已低於產地基本生存與物流成本，將不計入區域有效採購意向"
    )


class NoRegionError(AppError):
    """使用者還沒設定所在地，無法歸入任何區域看板。"""

    code = "intent_region_required"
    message = "請先在個人設定填寫所在地區，才能提交意向價格"


# ---------------------------------------------------------------------------
# 成本底線（PRD 1.2）
# ---------------------------------------------------------------------------


@dataclass
class PriceFloor:
    """意向價格的下界。"""

    floor: Decimal | None
    # 推算基準：近 N 日官方行情的中位數
    reference: Decimal | None
    currency: str | None
    unit: str | None
    sample_days: int
    source: str

    @property
    def is_known(self) -> bool:
        return self.floor is not None


async def price_floor(
    session: AsyncSession,
    product: Product,
    *,
    country_code: str | None = None,
    region: str | None = None,
) -> PriceFloor:
    """算出 `P_min`。

    PRD 想要的是「農政單位公定生產成本 + 採收物流費」。**我們沒有那個資料源**，
    所以這裡用能拿到的最好的替代：近 `INTENT_FLOOR_LOOKBACK_DAYS` 日
    官方批發行情的中位數 × `INTENT_FLOOR_RATIO`，再加上固定物流費。

    批發價本來就高於產地成本，所以係數要小於 1（預設 0.7）。
    真的接上成本資料源時，換掉這個函式即可，其餘邏輯不用動。
    """
    lookback = settings.intent_floor_lookback_days
    since = datetime.now(UTC).date() - timedelta(days=lookback)

    async def _fetch(with_region: str | None):
        stmt = (
            select(OfficialPrice.price_avg, OfficialPrice.currency, OfficialPrice.unit)
            .join(Market, Market.id == OfficialPrice.market_id)
            .where(
                OfficialPrice.product_id == product.id,
                OfficialPrice.trade_date >= since,
                OfficialPrice.price_avg.is_not(None),
            )
        )
        if country_code:
            stmt = stmt.where(Market.country_code == country_code.upper())
        if with_region:
            stmt = stmt.where(Market.region == with_region)
        stmt = country_scope.apply(stmt, Market.country_code)
        return (await session.execute(stmt)).all()

    # 先試該區域，沒有就退回全國。
    #
    # 這個退回是必要的，不是保險：意向的 region 來自**使用者個人檔案的
    # ISO 3166-2 行政區顯示名**（臺北市），而 markets.region 是**資料源
    # 自己給的自由文字**（台北市）——兩個命名空間本來就不保證一致，
    # 任何國家都可能對不上。底線是成本防線，寧可用全國均價也不能因為
    # 字串不匹配就整個消失、讓超低價長驅直入。
    rows = await _fetch(region) if region else []
    scope_used = "region"
    if not rows:
        rows = await _fetch(None)
        scope_used = "country" if country_code else "global"

    if not rows:
        # 真的完全沒有官方行情就沒有底線可言。不擋——寧可放行也不要因為
        # 資料源還沒接上就讓使用者完全不能填
        return PriceFloor(None, None, None, None, lookback, "no_official_data")

    reference = st.median([r.price_avg for r in rows])
    assert reference is not None
    ratio = Decimal(str(settings.intent_floor_ratio))
    logistics = Decimal(str(settings.intent_floor_logistics))
    floor = (reference * ratio + logistics).quantize(Decimal("0.01"))

    return PriceFloor(
        floor=floor,
        reference=reference.quantize(Decimal("0.01")),
        currency=rows[0].currency,
        unit=rows[0].unit,
        sample_days=lookback,
        source=f"official_price_proxy:{scope_used}",
    )


# ---------------------------------------------------------------------------
# 提交（PRD 2.1 / 2.2 / 1.2）
# ---------------------------------------------------------------------------


async def get_or_create_reputation(session: AsyncSession, user_id: uuid.UUID) -> UserReputation:
    rep = await session.get(UserReputation, user_id)
    if rep is None:
        rep = UserReputation(user_id=user_id, weight=settings.intent_weight_initial)
        session.add(rep)
        await session.flush()
    return rep


async def last_intent(
    session: AsyncSession, user_id: uuid.UUID, product_id: uuid.UUID
) -> PriceIntent | None:
    return await session.scalar(
        select(PriceIntent)
        .where(PriceIntent.user_id == user_id, PriceIntent.product_id == product_id)
        .order_by(PriceIntent.created_at.desc())
        .limit(1)
    )


async def cooldown_remaining(
    session: AsyncSession, user_id: uuid.UUID, product_id: uuid.UUID
) -> int:
    """還要等幾秒才能再次提交。0 代表現在就可以。"""
    previous = await last_intent(session, user_id, product_id)
    if previous is None:
        return 0
    elapsed = (datetime.now(UTC) - previous.created_at).total_seconds()
    window = settings.intent_cooldown_days * 86400
    return max(0, int(window - elapsed))


async def submit(
    session: AsyncSession,
    user: User,
    product: Product,
    *,
    price: Decimal,
    quantity: Decimal | None = None,
    unit: str | None = None,
    currency: str | None = None,
    note: str | None = None,
    client_ip: str | None = None,
) -> PriceIntent:
    """提交意向價格，逐層過防護。

    順序是刻意的：先擋掉不該寫入的（沒地區、冷卻期、低於底線），
    再寫入並標記需要降權的（機房 IP、影子封禁、權重為 0）。
    降權的那些仍然寫進去——使用者看得到自己的數字（PRD 3.2 的影子封禁
    正是要這個效果），只是聚合時不算。
    """
    region = _user_region(user)
    if not region:
        raise NoRegionError()

    remaining = await cooldown_remaining(session, user.id, product.id)
    if remaining > 0:
        raise IntentCooldownError(
            f"這個作物還要 {remaining // 86400 + 1} 天才能再次調整意向價格",
            details={"retry_after": remaining, "cooldown_days": settings.intent_cooldown_days},
        )

    floor = await price_floor(
        session, product, country_code=user.country_code, region=region
    )
    if floor.is_known and price < floor.floor:
        raise BelowFloorError(
            details={
                "floor_price": str(floor.floor),
                "reference_price": str(floor.reference),
                "currency": floor.currency,
                "unit": floor.unit,
            }
        )

    rep = await get_or_create_reputation(session, user.id)
    hosting, proxy = await _ip_reputation(client_ip)

    excluded: IntentExclusion | None = None
    if rep.is_shadow_banned:
        excluded = IntentExclusion.SHADOWED
    elif hosting or proxy:
        excluded = IntentExclusion.UNTRUSTED_IP
    elif rep.weight <= 0:
        excluded = IntentExclusion.ZERO_WEIGHT

    # 舊的那筆退場，保留歷史供信譽計算
    await session.execute(
        update(PriceIntent)
        .where(
            PriceIntent.user_id == user.id,
            PriceIntent.product_id == product.id,
            PriceIntent.status == IntentStatus.ACTIVE,
        )
        .values(status=IntentStatus.SUPERSEDED)
    )

    intent = PriceIntent(
        user_id=user.id,
        product_id=product.id,
        price=price,
        quantity=quantity,
        currency=(currency or _default_currency(user)).upper(),
        unit=unit or product.default_unit,
        country_code=user.country_code,
        region=region,
        latitude=user.latitude,
        longitude=user.longitude,
        status=IntentStatus.ACTIVE,
        excluded_reason=excluded,
        weight_snapshot=rep.weight,
        floor_price=floor.floor,
        ip_hosting=hosting,
        ip_proxy=proxy,
        note=note,
    )
    session.add(intent)
    await session.flush()
    return intent


async def withdraw(session: AsyncSession, user: User, intent_id: uuid.UUID) -> PriceIntent:
    intent = await session.get(PriceIntent, intent_id)
    if intent is None:
        raise NotFoundError("Intent not found", code="intent_not_found")
    if intent.user_id != user.id:
        raise ForbiddenError("只能撤回自己的意向價格", code="not_intent_owner")
    intent.status = IntentStatus.WITHDRAWN
    await session.flush()
    return intent


# ---------------------------------------------------------------------------
# 聚合（PRD 1.1 / 3.2）
# ---------------------------------------------------------------------------


@dataclass
class IntentSummary:
    """區域意向看板。"""

    product_id: uuid.UUID
    region: str | None
    country_code: str | None
    currency: str | None
    unit: str | None

    # 納入計算的樣本數（已排除離群、影子封禁、機房 IP）
    sample_count: int
    # 總提交數，含被排除的
    submitted_count: int
    excluded_count: int

    # 公開的錨點價格。刻意不提供算術平均——一筆惡意值就能拉垮
    anchor_price: Decimal | None       # 加權中位數，權重來自信譽分
    median: Decimal | None
    trimmed_mean: Decimal | None

    q1: Decimal | None
    q3: Decimal | None
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None

    floor_price: Decimal | None

    # 這次有沒有真的執行離群排除（倍率護欄或 IQR 任一生效就算）。
    # 兩層都有樣本數門檻，全都未達時 q1/q3/lower_bound/upper_bound 仍會
    # 回報——那是樣本的描述統計。沒有這個旗標的話，呼叫端會看到一組
    # 界線卻不知道它沒有被執行。
    outlier_filter_active: bool = False
    # 要幾筆才會開始有防護（倍率護欄的門檻，比 IQR 低很多）
    min_samples_for_outlier_filter: int = 0

    # 需求總量（只加總有填數量的意向）與有填數量的人數。
    # 對產地來說「450 人、共 1200 箱」比價格共識更有行動價值，
    # 因為那直接決定要不要開一團
    demand_quantity: Decimal | None = None
    demand_respondents: int = 0
    exclusions: dict[str, int] = field(default_factory=dict)


def iqr_filter_enabled(sample_count: int, bounds: st.OutlierBounds | None) -> bool:
    """這批樣本要不要執行 IQR 離群排除。

    樣本太少時不做：三、五筆的四分位數不具意義，硬做會把正常的價差
    當成離群值砍掉。細緻的離群判斷需要足夠樣本才站得住腳。
    """
    return bounds is not None and sample_count >= settings.intent_min_samples_for_iqr


def ratio_guard_bounds(values: list[Decimal]) -> tuple[Decimal, Decimal] | None:
    """以中位數為基準的絕對倍率護欄，回 (下限, 上限)。

    IQR 有樣本數門檻，所以樣本不足時完全沒有防線——五筆意向裡塞一筆
    中位數 14 倍的價格，過去會原封不動計入 `sample_count` 與 `max_price`。
    這一層補的就是那個缺口：它只需要三筆樣本，因為基準是中位數而不是
    四分位距，一筆極端值拉不動它。

    倍率刻意放寬（預設 5 倍）。真實的品質價差、產地價差很少超過兩三倍，
    設在 5 倍幾乎不可能誤殺，但足以擋下量級層次的灌水。細緻的離群值
    仍然交給樣本夠多時的 IQR，兩層互補而不是取代。

    回 `None` 代表這批不適用（樣本不足，或中位數非正數算不出比例）。
    """
    if len(values) < settings.intent_min_samples_for_ratio_guard:
        return None
    med = st.median(values)
    if med is None or med <= 0:
        return None
    ratio = Decimal(str(settings.intent_max_median_ratio))
    return (med / ratio, med * ratio)


async def summarise(
    session: AsyncSession,
    product: Product,
    *,
    region: str | None = None,
    country_code: str | None = None,
) -> IntentSummary:
    """算出區域看板。

    流程：撈生效意向 → 排除影子封禁等 → IQR 找離群值並標記 →
    用剩下的算中位數／截尾均值／加權中位數。

    **離群值會被寫回資料庫**（`excluded_reason = outlier`），
    這樣使用者查自己的意向時看得到「為什麼沒被計入」，
    信譽計算也才有依據。

    離群排除分兩層，因為兩者擋的是不同東西：

    1. **倍率護欄**（`ratio_guard_bounds`，3 筆起）：偏離中位數 5 倍以上
       一律排除。擋的是量級層次的灌水，樣本再少也有效。
    2. **IQR**（`iqr_filter_enabled`，預設 8 筆起）：擋的是幅度較小、
       需要靠分布才判斷得出來的離群值。

    兩層都未達門檻時不會排除任何東西，但 `lower_bound` / `upper_bound`
    仍照常回報，所以 `outlier_filter_active` 會說明這次到底有沒有濾。
    """
    stmt = (
        select(PriceIntent)
        .options(selectinload(PriceIntent.user))
        .where(
            PriceIntent.product_id == product.id,
            PriceIntent.status == IntentStatus.ACTIVE,
        )
    )
    if region:
        stmt = stmt.where(PriceIntent.region == region)
    if country_code:
        stmt = stmt.where(PriceIntent.country_code == country_code.upper())
    stmt = country_scope.apply(stmt, PriceIntent.country_code)

    intents = list((await session.execute(stmt)).scalars())
    submitted = len(intents)

    floor = await price_floor(session, product, country_code=country_code, region=region)

    exclusions: dict[str, int] = {}
    # 先排除與統計無關的（影子封禁、機房 IP、零權重）——這些連算 IQR 都不該參與，
    # 否則刷票者仍然能靠數量把容許區間撐開
    candidates: list[PriceIntent] = []
    for intent in intents:
        if intent.excluded_reason is not None and intent.excluded_reason is not IntentExclusion.OUTLIER:
            exclusions[intent.excluded_reason.value] = (
                exclusions.get(intent.excluded_reason.value, 0) + 1
            )
            continue
        candidates.append(intent)

    if not candidates:
        return IntentSummary(
            product_id=product.id, region=region, country_code=country_code,
            currency=None, unit=None, sample_count=0, submitted_count=submitted,
            excluded_count=submitted, anchor_price=None, median=None, trimmed_mean=None,
            q1=None, q3=None, lower_bound=None, upper_bound=None,
            min_price=None, max_price=None, floor_price=floor.floor, exclusions=exclusions,
            outlier_filter_active=False,
            min_samples_for_outlier_filter=settings.intent_min_samples_for_ratio_guard,
        )

    outlier_ids: list[uuid.UUID] = []

    def _drop(intent: PriceIntent) -> None:
        outlier_ids.append(intent.id)
        exclusions[IntentExclusion.OUTLIER.value] = (
            exclusions.get(IntentExclusion.OUTLIER.value, 0) + 1
        )

    # 第一層：倍率護欄。順序不能對調——兩筆以上協同的極端值會把 Q3
    # 撐高、容許區間跟著放寬，幅度小一點的灌水值就能躲過 IQR。
    # 先砍掉量級層次的假值，IQR 才會在乾淨的分布上判斷
    guard = ratio_guard_bounds([i.price for i in candidates])
    survivors: list[PriceIntent] = []
    for intent in candidates:
        if guard is not None and not (guard[0] <= intent.price <= guard[1]):
            _drop(intent)
            continue
        survivors.append(intent)

    # 第二層：IQR。界線與四分位數都用護欄後的樣本算，回報的數字才與
    # 實際套用的一致
    prices = [i.price for i in survivors]
    bounds = st.iqr_bounds(prices, settings.intent_iqr_multiplier)
    quart = st.quartiles(prices)
    iqr_active = iqr_filter_enabled(len(survivors), bounds)
    filter_active = guard is not None or iqr_active

    kept: list[PriceIntent] = []
    for intent in survivors:
        if iqr_active:
            assert bounds is not None
            if not bounds.contains(intent.price):
                _drop(intent)
                continue
        kept.append(intent)

    await _mark_outliers(session, outlier_ids, kept_ids=[i.id for i in kept])

    kept_prices = [i.price for i in kept]
    weighted = [(i.price, max(0.0, i.weight_snapshot)) for i in kept]

    return IntentSummary(
        product_id=product.id,
        region=region,
        country_code=country_code,
        currency=kept[0].currency if kept else None,
        unit=kept[0].unit if kept else None,
        sample_count=len(kept),
        submitted_count=submitted,
        excluded_count=submitted - len(kept),
        anchor_price=_q(st.weighted_median(weighted)),
        median=_q(st.median(kept_prices)),
        trimmed_mean=_q(st.trimmed_mean(kept_prices, settings.intent_trim_fraction)),
        q1=_q(quart.q1) if quart else None,
        q3=_q(quart.q3) if quart else None,
        lower_bound=_q(bounds.lower) if bounds else None,
        upper_bound=_q(bounds.upper) if bounds else None,
        min_price=min(kept_prices) if kept_prices else None,
        max_price=max(kept_prices) if kept_prices else None,
        floor_price=floor.floor,
        exclusions=exclusions,
        outlier_filter_active=filter_active,
        min_samples_for_outlier_filter=settings.intent_min_samples_for_ratio_guard,
        demand_quantity=_sum_quantity(kept),
        demand_respondents=sum(1 for i in kept if i.quantity is not None),
    )


async def _mark_outliers(
    session: AsyncSession, outlier_ids: list[uuid.UUID], *, kept_ids: list[uuid.UUID]
) -> None:
    """把 IQR 的判定寫回資料庫，並把重新落回區間內的解除標記。"""
    if outlier_ids:
        await session.execute(
            update(PriceIntent)
            .where(PriceIntent.id.in_(outlier_ids))
            .values(excluded_reason=IntentExclusion.OUTLIER)
        )
    if kept_ids:
        await session.execute(
            update(PriceIntent)
            .where(
                PriceIntent.id.in_(kept_ids),
                PriceIntent.excluded_reason == IntentExclusion.OUTLIER,
            )
            .values(excluded_reason=None)
        )


# ---------------------------------------------------------------------------
# 信譽（PRD 3.1 / 3.2）
# ---------------------------------------------------------------------------


@dataclass
class ReputationChange:
    user_id: uuid.UUID
    before: float
    after: float
    verdict: str          # hit / miss / neutral
    shadow_banned: bool


async def update_reputation(
    session: AsyncSession, product: Product, *, region: str | None = None
) -> list[ReputationChange]:
    """依最新的共識重算這批使用者的信譽權重。

    - 落在共識區間（中位數 ±`INTENT_CONSENSUS_BAND_PCT`%）內 → 權重上調
    - 偏離 `INTENT_DEVIATION_SIGMA` 個標準差以上 → 權重下調
    - 連續偏離超過門檻 → 影子封禁

    設計上刻意讓**上調慢、下調快**：錯殺一個正常使用者的代價，
    遠低於讓刷票者維持高權重。
    """
    summary = await summarise(session, product, region=region)
    if summary.median is None or summary.sample_count < settings.intent_min_samples_for_reputation:
        return []

    stmt = select(PriceIntent).where(
        PriceIntent.product_id == product.id,
        PriceIntent.status == IntentStatus.ACTIVE,
    )
    if region:
        stmt = stmt.where(PriceIntent.region == region)
    intents = list((await session.execute(stmt)).scalars())
    counted = [i for i in intents if i.excluded_reason is None]
    if not counted:
        return []

    prices = [i.price for i in counted]
    avg = st.mean(prices)
    sd = st.stdev(prices)
    changes: list[ReputationChange] = []

    for intent in intents:
        rep = await get_or_create_reputation(session, intent.user_id)
        before = rep.weight
        verdict = "neutral"

        if intent.excluded_reason is IntentExclusion.OUTLIER:
            verdict = "miss"
        elif intent.excluded_reason is None and avg is not None:
            if st.within_band(intent.price, summary.median, settings.intent_consensus_band_pct):
                verdict = "hit"
            else:
                sigma = st.sigma_distance(intent.price, avg, sd)
                if sigma is not None and sigma >= settings.intent_deviation_sigma:
                    verdict = "miss"

        rep.samples += 1
        if verdict == "hit":
            rep.hits += 1
            rep.consecutive_misses = 0
            rep.weight = min(settings.intent_weight_max, rep.weight + settings.intent_weight_step_up)
        elif verdict == "miss":
            rep.misses += 1
            rep.consecutive_misses += 1
            rep.weight = max(
                settings.intent_weight_min, rep.weight - settings.intent_weight_step_down
            )
            if (
                rep.consecutive_misses >= settings.intent_shadow_ban_strikes
                or rep.weight <= settings.intent_shadow_ban_weight
            ) and not rep.is_shadow_banned:
                rep.is_shadow_banned = True
                rep.shadow_banned_at = datetime.now(UTC)

        changes.append(
            ReputationChange(
                user_id=intent.user_id,
                before=before,
                after=rep.weight,
                verdict=verdict,
                shadow_banned=rep.is_shadow_banned,
            )
        )

    await session.flush()
    return changes


# ---------------------------------------------------------------------------
# 優先通知與響應追蹤（PRD 4.1 / 4.2）
# ---------------------------------------------------------------------------


@dataclass
class OfferMatch:
    user_id: uuid.UUID
    intent_id: uuid.UUID
    intent_price: Decimal
    display_name: str | None


async def match_offer(
    session: AsyncSession, quote: Quote, *, limit: int = 200
) -> list[OfferMatch]:
    """產地開了一個價，找出該優先通知誰。

    條件：意向價 **>=** 開團價（願意出更多的當然買得起），
    同區域，且該筆意向有被計入看板（離群、影子封禁的不通知）。

    排序刻意用「意向價由低到高」：填的價格越貼近產地實際開價的人，
    越應該先拿到配額——這正是 PRD 要的博弈方向，虛報高價搶不到優勢，
    虛報低價則根本進不了名單。
    """
    stmt = (
        select(PriceIntent)
        .options(selectinload(PriceIntent.user))
        .where(
            PriceIntent.product_id == quote.product_id,
            PriceIntent.status == IntentStatus.ACTIVE,
            PriceIntent.excluded_reason.is_(None),
            PriceIntent.price >= quote.price,
            PriceIntent.currency == quote.currency,
        )
        .order_by(PriceIntent.price.asc())
        .limit(limit)
    )
    if quote.region:
        stmt = stmt.where(PriceIntent.region == quote.region)

    return [
        OfferMatch(
            user_id=i.user_id,
            intent_id=i.id,
            intent_price=i.price,
            display_name=i.user.display_name if i.user else None,
        )
        for i in (await session.execute(stmt)).scalars()
    ]


async def record_notifications(
    session: AsyncSession, quote: Quote, matches: list[OfferMatch]
) -> list[IntentNotification]:
    """把推播名單記下來，之後才追蹤得到響應率。

    這裡只寫紀錄，**不負責實際發送**——推播管道還沒接。
    """
    rows = [
        IntentNotification(
            user_id=m.user_id,
            intent_id=m.intent_id,
            product_id=quote.product_id,
            quote_id=quote.id,
            offer_price=quote.price,
            currency=quote.currency,
            unit=quote.unit,
            intent_price=m.intent_price,
        )
        for m in matches
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def record_response(
    session: AsyncSession, user: User, notification_id: uuid.UUID, *, clicked: bool
) -> IntentNotification:
    """記錄使用者看了／點了推播。"""
    row = await session.get(IntentNotification, notification_id)
    if row is None:
        raise NotFoundError("Notification not found", code="notification_not_found")
    if row.user_id != user.id:
        raise ForbiddenError("只能回報自己的通知", code="not_notification_owner")

    now = datetime.now(UTC)
    row.opened_at = row.opened_at or now
    if clicked:
        row.clicked_at = row.clicked_at or now
    await session.flush()
    return row


@dataclass
class ResponseStats:
    sent: int
    opened: int
    clicked: int

    @property
    def click_rate(self) -> float | None:
        return round(self.clicked / self.sent * 100, 2) if self.sent else None


async def response_stats(
    session: AsyncSession, user_id: uuid.UUID, *, days: int = 90
) -> ResponseStats:
    since = datetime.now(UTC) - timedelta(days=days)
    row = (
        await session.execute(
            select(
                func.count(),
                func.count(IntentNotification.opened_at),
                func.count(IntentNotification.clicked_at),
            ).where(
                IntentNotification.user_id == user_id,
                IntentNotification.sent_at >= since,
            )
        )
    ).one()
    return ResponseStats(sent=row[0] or 0, opened=row[1] or 0, clicked=row[2] or 0)


async def penalise_ghost_demand(session: AsyncSession, *, days: int = 90) -> int:
    """PRD 4.2：多次收到推播卻完全零響應的，扣信譽。

    由排程定期呼叫。門檻設得保守——通知可能根本沒送達，
    不能因為幾次沒點就當成假需求。
    """
    threshold = settings.intent_ghost_notification_threshold
    since = datetime.now(UTC) - timedelta(days=days)

    rows = (
        await session.execute(
            select(
                IntentNotification.user_id,
                func.count().label("sent"),
                func.count(IntentNotification.opened_at).label("opened"),
            )
            .where(IntentNotification.sent_at >= since)
            .group_by(IntentNotification.user_id)
            .having(func.count() >= threshold)
            .having(func.count(IntentNotification.opened_at) == 0)
        )
    ).all()

    for row in rows:
        rep = await get_or_create_reputation(session, row.user_id)
        rep.weight = max(
            settings.intent_weight_min, rep.weight - settings.intent_weight_step_down
        )
    await session.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


async def _ip_reputation(client_ip: str | None) -> tuple[bool, bool]:
    """查這個 IP 是不是機房或 Proxy。查不到就當成一般 IP 放行。"""
    if not settings.intent_block_hosting_ip or not client_ip:
        return False, False

    from app.services import geoip

    if not geoip.is_public_ip(client_ip):
        return False, False
    provider = geoip.get_geoip_provider()
    if not provider.enabled:
        return False, False
    flags = await geoip.ip_flags(client_ip)
    return flags


def _user_region(user: User) -> str | None:
    from app.data.countries import lookup_subdivision

    sub = lookup_subdivision(user.subdivision_code)
    if sub is not None:
        return sub.display_name(user.locale)
    return user.locality


def _default_currency(user: User) -> str:
    from app.data.countries import default_currency

    return user.preferred_currency or default_currency(user.country_code)


def _sum_quantity(intents: list[PriceIntent]) -> Decimal | None:
    """需求總量。沒有人填數量時回 None，而不是 0——那兩者意義不同。"""
    amounts = [i.quantity for i in intents if i.quantity is not None]
    return sum(amounts) if amounts else None


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.01")) if value is not None else None
