"""消費者意向價格的 API schema。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import IntentExclusion, IntentStatus
from app.models.intent import IntentNotification, PriceIntent
from app.schemas.catalog import ProductOut
from app.services.intents import IntentSummary, PriceFloor


class IntentCreate(BaseModel):
    """我願意用這個價格買。"""

    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(gt=0, le=Decimal("99999999"), description="期望價格")
    quantity: Decimal | None = Field(
        default=None, gt=0, description="願意購買的數量（以 unit 計）。選填"
    )
    unit: str | None = Field(default=None, max_length=16, description="省略則用品項標準單位")
    currency: str | None = Field(
        default=None, min_length=3, max_length=3, description="省略則用個人檔案的幣別"
    )
    note: str | None = Field(default=None, max_length=300)


class IntentOut(BaseModel):
    """自己的意向。**別人看不到單筆意向**，只看得到聚合後的看板。"""

    id: uuid.UUID
    product: ProductOut
    price: Decimal
    quantity: Decimal | None = None
    currency: str
    unit: str
    country_code: str
    region: str | None = None
    status: IntentStatus
    # 有值代表這筆沒被計入看板；null 代表有計入
    excluded_reason: IntentExclusion | None = None
    # 提交當下的信譽權重
    weight: float
    # 提交當下算出來的成本底線
    floor_price: Decimal | None = None
    note: str | None = None
    created_at: datetime

    @classmethod
    def from_model(cls, intent: PriceIntent, locale: str) -> "IntentOut":
        return cls(
            id=intent.id,
            product=ProductOut.from_model(intent.product, locale),
            price=intent.price,
            quantity=intent.quantity,
            currency=intent.currency,
            unit=intent.unit,
            country_code=intent.country_code,
            region=intent.region,
            status=intent.status,
            excluded_reason=intent.excluded_reason,
            weight=intent.weight_snapshot,
            floor_price=intent.floor_price,
            note=intent.note,
            created_at=intent.created_at,
        )


class PriceFloorOut(BaseModel):
    """輸入前先問底線，前端就能即時擋下過低的出價（PRD 4.3 的後端支援）。"""

    floor_price: Decimal | None = None
    reference_price: Decimal | None = Field(
        default=None, description="推算基準：近 N 日官方行情的中位數"
    )
    currency: str | None = None
    unit: str | None = None
    sample_days: int
    # official_price_proxy = 用官方批發價推算；no_official_data = 沒資料，不設限
    source: str
    hint: str | None = Field(
        default=None, description="低於底線時要顯示給使用者的提示語"
    )

    @classmethod
    def from_model(cls, floor: PriceFloor) -> "PriceFloorOut":
        return cls(
            floor_price=floor.floor,
            reference_price=floor.reference,
            currency=floor.currency,
            unit=floor.unit,
            sample_days=floor.sample_days,
            source=floor.source,
            hint=(
                "若出價過低脫離產地成本，小農將判定為無效需求而拒絕接單；"
                "合理報價才能最快促成產地直運。"
                if floor.is_known
                else None
            ),
        )


class IntentSummaryOut(BaseModel):
    """區域意向看板。公開，但只給聚合值，不洩漏任何個人的出價。"""

    product: ProductOut
    region: str | None = None
    country_code: str | None = None
    currency: str | None = None
    unit: str | None = None

    # 這才是對外公佈的錨點：信譽加權的中位數。
    # 刻意不提供算術平均——一筆惡意值就能拉垮
    anchor_price: Decimal | None = None
    median: Decimal | None = None
    trimmed_mean: Decimal | None = None

    q1: Decimal | None = None
    q3: Decimal | None = None
    lower_bound: Decimal | None = Field(
        default=None,
        description="IQR 容許區間下界。**只有 outlier_filter_active 為 true 時才實際生效**",
    )
    upper_bound: Decimal | None = Field(
        default=None,
        description="IQR 容許區間上界。**只有 outlier_filter_active 為 true 時才實際生效**",
    )
    outlier_filter_active: bool = Field(
        default=False,
        description=(
            "這次聚合有沒有真的執行 IQR 離群排除。false 代表樣本數未達門檻，"
            "區間外的值仍會被計入 sample_count / min_price / max_price"
        ),
    )
    min_samples_for_outlier_filter: int = Field(
        default=0, description="啟用離群排除所需的樣本數門檻"
    )
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    floor_price: Decimal | None = None

    demand_quantity: Decimal | None = Field(
        default=None, description="需求總量（只加總有填數量的意向）"
    )
    demand_respondents: int = Field(default=0, description="其中有填數量的人數")
    sample_count: int = Field(description="納入計算的筆數")
    submitted_count: int = Field(description="總提交筆數，含被排除的")
    excluded_count: int
    # 各排除原因的筆數，例如 {"outlier": 3, "shadowed": 1}
    exclusions: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_summary(
        cls, summary: IntentSummary, product_out: ProductOut
    ) -> "IntentSummaryOut":
        return cls(
            product=product_out,
            region=summary.region,
            country_code=summary.country_code,
            currency=summary.currency,
            unit=summary.unit,
            anchor_price=summary.anchor_price,
            median=summary.median,
            trimmed_mean=summary.trimmed_mean,
            q1=summary.q1,
            q3=summary.q3,
            lower_bound=summary.lower_bound,
            upper_bound=summary.upper_bound,
            outlier_filter_active=summary.outlier_filter_active,
            min_samples_for_outlier_filter=summary.min_samples_for_outlier_filter,
            min_price=summary.min_price,
            max_price=summary.max_price,
            floor_price=summary.floor_price,
            demand_quantity=summary.demand_quantity,
            demand_respondents=summary.demand_respondents,
            sample_count=summary.sample_count,
            submitted_count=summary.submitted_count,
            excluded_count=summary.excluded_count,
            exclusions=summary.exclusions,
        )


class ReputationOut(BaseModel):
    """自己的信譽狀態。

    **不揭露 `is_shadow_banned`** ——影子封禁的重點就是對方不知道，
    知道了就會換帳號重來。
    """

    weight: float = Field(description="0.0 ~ 2.0，新使用者從 1.0 開始")
    samples: int
    hits: int = Field(description="落在共識區間內的次數")
    misses: int = Field(description="明顯偏離常態的次數")
    has_verified_purchase: bool


class NotificationOut(BaseModel):
    """產地開團的優先通知。"""

    id: uuid.UUID
    product: ProductOut
    offer_price: Decimal
    currency: str
    unit: str
    intent_price: Decimal | None = None
    quote_id: uuid.UUID | None = None
    sent_at: datetime
    opened_at: datetime | None = None
    clicked_at: datetime | None = None

    @classmethod
    def from_model(cls, row: IntentNotification, locale: str) -> "NotificationOut":
        return cls(
            id=row.id,
            product=ProductOut.from_model(row.product, locale),
            offer_price=row.offer_price,
            currency=row.currency,
            unit=row.unit,
            intent_price=row.intent_price,
            quote_id=row.quote_id,
            sent_at=row.sent_at,
            opened_at=row.opened_at,
            clicked_at=row.clicked_at,
        )


class NotificationResponseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clicked: bool = Field(
        default=True, description="true = 點了「前往購買」；false = 只是看過"
    )


class OfferMatchOut(BaseModel):
    """某筆產地報價該通知誰。維運 / 小農端用。"""

    user_id: uuid.UUID
    display_name: str | None = None
    intent_price: Decimal


class OfferMatchListOut(BaseModel):
    quote_id: uuid.UUID
    offer_price: Decimal
    currency: str
    unit: str
    matched: int
    # 有沒有把這批寫進 intent_notifications
    recorded: bool
    items: list[OfferMatchOut]
