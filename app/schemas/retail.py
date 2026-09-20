"""消費者回報超市零售價的 API schema。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RetailExclusion, RetailReportStatus, StoreType
from app.models.retail import RetailPriceReport
from app.schemas.catalog import ProductOut
from app.services.retail import RetailSpread, RetailSummary, StoreBreakdown


class RetailReportCreate(BaseModel):
    """我在某家店看到這個價格。"""

    model_config = ConfigDict(extra="forbid")

    observed_price: Decimal = Field(
        gt=0, le=Decimal("99999999"), description="標籤上看到的價格（整包的價格）"
    )
    store_name: str = Field(min_length=1, max_length=120, description="店名，例如 Big Bazaar")
    store_type: StoreType = Field(default=StoreType.SUPERMARKET, description="通路別")
    store_branch: str | None = Field(default=None, max_length=120, description="分店")
    pack_size: Decimal | None = Field(
        default=None,
        gt=0,
        description="包裝規格（以 unit 計）。標「500g / ₹40」就填 0.5，後端換算成單位價",
    )
    unit: str | None = Field(default=None, max_length=16, description="省略則用品項標準單位")
    currency: str | None = Field(
        default=None, min_length=3, max_length=3, description="省略則用個人檔案的幣別"
    )
    observed_on: date | None = Field(
        default=None, description="**看到價格的日期**，不是送出日期。省略則為今天"
    )
    is_promotion: bool = Field(default=False, description="是否為特價／促銷價")
    location_text: str | None = Field(default=None, max_length=200)
    photo_url: str | None = Field(
        default=None, max_length=2000, description="價格標籤或收據照片的網址。上傳管道尚未提供"
    )
    note: str | None = Field(default=None, max_length=300)


class RetailReporterOut(BaseModel):
    """回報者。**只給暱稱**，不揭露電話或精確位置。"""

    display_name: str | None = None
    is_me: bool = False


class RetailReportOut(BaseModel):
    """一筆零售回報。**單筆是公開的**——比價本來就是這個功能的重點。"""

    id: uuid.UUID
    product_id: uuid.UUID
    observed_price: Decimal
    pack_size: Decimal | None = None
    unit_price: Decimal = Field(description="換算後的單位價，聚合一律用這個")
    currency: str
    unit: str
    is_promotion: bool

    store_type: StoreType
    store_name: str
    store_branch: str | None = None

    country_code: str
    subdivision_code: str | None = None
    region: str | None = None
    location_text: str | None = None

    observed_on: date
    photo_url: str | None = None
    note: str | None = None

    status: RetailReportStatus
    # **只有自己看得到。** 讓別人知道誰被排除等於告訴刷票者哪招失效了
    excluded_reason: RetailExclusion | None = None
    reporter: RetailReporterOut
    created_at: datetime

    @classmethod
    def from_model(
        cls, report: RetailPriceReport, *, viewer_id: uuid.UUID | None = None
    ) -> "RetailReportOut":
        mine = viewer_id is not None and report.user_id == viewer_id
        return cls(
            id=report.id,
            product_id=report.product_id,
            observed_price=report.observed_price,
            pack_size=report.pack_size,
            unit_price=report.unit_price,
            currency=report.currency,
            unit=report.unit,
            is_promotion=report.is_promotion,
            store_type=report.store_type,
            store_name=report.store_name,
            store_branch=report.store_branch,
            country_code=report.country_code,
            subdivision_code=report.subdivision_code,
            region=report.region,
            location_text=report.location_text,
            observed_on=report.observed_on,
            photo_url=report.photo_url,
            note=report.note,
            status=report.status,
            excluded_reason=report.excluded_reason if mine else None,
            reporter=RetailReporterOut(
                display_name=getattr(report.user, "display_name", None),
                is_me=mine,
            ),
            created_at=report.created_at,
        )


class StoreTypeBreakdownOut(BaseModel):
    store_type: StoreType
    median: Decimal | None = None
    sample_count: int

    @classmethod
    def from_model(cls, b: StoreBreakdown) -> "StoreTypeBreakdownOut":
        return cls(store_type=b.store_type, median=b.median, sample_count=b.sample_count)


class RetailSummaryOut(BaseModel):
    """零售價看板。"""

    product: ProductOut
    region: str | None = None
    country_code: str | None = None
    currency: str | None = None
    unit: str | None = None
    days: int = Field(description="統計了近幾天的回報")

    typical_price: Decimal | None = Field(
        default=None, description="**要顯示的就是這個**：信譽加權的中位數"
    )
    median: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    q1: Decimal | None = None
    q3: Decimal | None = None

    sample_count: int = Field(description="納入計算的筆數")
    submitted_count: int = Field(description="期間內的總回報數，含被排除的")
    excluded_count: int
    store_count: int = Field(description="涵蓋幾家不同的店")
    outlier_filter_active: bool = Field(
        description="這次有沒有執行離群排除。false 代表樣本未達門檻"
    )
    min_samples_for_outlier_filter: int
    exclusions: dict[str, int] = Field(default_factory=dict)
    by_store_type: list[StoreTypeBreakdownOut] = Field(
        default_factory=list,
        description="**分通路呈現而不是抹平**。便利商店是量販店的兩倍很正常",
    )

    @classmethod
    def from_summary(
        cls, summary: RetailSummary, product_out: ProductOut
    ) -> "RetailSummaryOut":
        return cls(
            product=product_out,
            region=summary.region,
            country_code=summary.country_code,
            currency=summary.currency,
            unit=summary.unit,
            days=summary.days,
            typical_price=summary.typical_price,
            median=summary.median,
            min_price=summary.min_price,
            max_price=summary.max_price,
            q1=summary.q1,
            q3=summary.q3,
            sample_count=summary.sample_count,
            submitted_count=summary.submitted_count,
            excluded_count=summary.excluded_count,
            store_count=summary.store_count,
            outlier_filter_active=summary.outlier_filter_active,
            min_samples_for_outlier_filter=summary.min_samples_for_outlier_filter,
            exclusions=summary.exclusions,
            by_store_type=[StoreTypeBreakdownOut.from_model(b) for b in summary.by_store_type],
        )


class RetailSpreadOut(BaseModel):
    """產銷價差：從批發到零售中間差了多少。"""

    product: ProductOut
    region: str | None = None
    currency: str | None = None
    unit: str | None = None
    retail_price: Decimal | None = None
    wholesale_price: Decimal | None = None
    spread: Decimal | None = Field(default=None, description="零售 − 批發")
    spread_pct: Decimal | None = Field(default=None, description="相對批發價的百分比")
    retail_samples: int
    wholesale_days: int
    wholesale_source: str | None = Field(
        default=None,
        description="批發價的取樣範圍：region / country。null 代表查無官方行情",
    )

    @classmethod
    def from_model(cls, s: RetailSpread, product_out: ProductOut) -> "RetailSpreadOut":
        return cls(
            product=product_out,
            region=s.region,
            currency=s.currency,
            unit=s.unit,
            retail_price=s.retail_price,
            wholesale_price=s.wholesale_price,
            spread=s.spread,
            spread_pct=s.spread_pct,
            retail_samples=s.retail_samples,
            wholesale_days=s.wholesale_days,
            wholesale_source=s.wholesale_source,
        )
