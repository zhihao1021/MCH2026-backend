"""消費者回報的超市 / 零售通路價格。

這是平台上第四種價格，與前三種的性質都不同：

| 來源 | 性質 | 誰提供 |
| --- | --- | --- |
| `official_prices` | 批發成交行情 | 官方資料源 |
| `quotes` | 開價（要約） | 小農 / 盤商 |
| `price_intents` | 願付價格（意願） | 消費者 |
| `retail_price_reports` | **實際看到的售價（觀察）** | 消費者 |

差別很要緊。報價與意向是「我想要多少錢」，可以無成本地亂喊；零售回報
是「我在那家店看到標這個價」——是對外部世界的陳述，原則上可被查證。
所以它的防濫用重點不在「價格合不合理」，而在「這個人說的可不可信」。

**單筆是公開的。** 意向價格只給聚合值（會洩漏個人願付價格），零售回報
反過來——「某某量販店 ₹38/kg、兩天前」正是使用者要的比價資訊，藏起來
這個功能就沒有意義了。公開的只有店家與價格，回報者僅顯示暱稱。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.enums import RetailExclusion, RetailReportStatus, StoreType

if TYPE_CHECKING:
    from app.models.catalog import Product
    from app.models.user import User


class RetailPriceReport(Base, TimestampMixin):
    __tablename__ = "retail_price_reports"
    __table_args__ = (
        # 看板主查詢：某品項近期的有效回報
        Index(
            "ix_retail_price_reports_product_id_status_observed_on",
            "product_id",
            "status",
            "observed_on",
        ),
        Index("ix_retail_price_reports_user_id_created_at", "user_id", "created_at"),
        Index("ix_retail_price_reports_country_code_region", "country_code", "region"),
        # 冷卻期查詢：同一人同一品項同一店家最近回報了沒
        Index(
            "ix_retail_price_reports_user_id_product_id_store_key",
            "user_id",
            "product_id",
            "store_key",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )

    # -- 價格 -------------------------------------------------------------
    # 實際看到的標價。可能是整包的價格，不一定是單位價
    observed_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    # 包裝規格（以 unit 計）。超市多半標「500g / ₹40」而不是每公斤多少，
    # 硬要使用者自己換算會換錯，所以讓他照標籤填，由後端換算
    pack_size: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # observed_price / pack_size，聚合一律用這個。沒填 pack_size 時等於 observed_price
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    # 特價 / 促銷價。會拉低整體水準，聚合時可選擇排除
    is_promotion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- 店家 -------------------------------------------------------------
    store_type: Mapped[StoreType] = mapped_column(
        pg_enum(StoreType, "store_type"), nullable=False, default=StoreType.SUPERMARKET
    )
    store_name: Mapped[str] = mapped_column(String(120), nullable=False)
    store_branch: Mapped[str | None] = mapped_column(String(120))
    # 正規化後的店家識別（小寫、去空白），冷卻期與「同店歷史」都靠它。
    # 使用者打「Big Bazaar」與「big bazaar」要算同一家
    store_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)

    # -- 地點 -------------------------------------------------------------
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    # ISO 3166-2。精確篩選用這個，`region` 只是顯示字串
    subdivision_code: Mapped[str | None] = mapped_column(String(8), index=True)
    region: Mapped[str | None] = mapped_column(String(80))
    location_text: Mapped[str | None] = mapped_column(String(200))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    # -- 時間 -------------------------------------------------------------
    # **看到價格的日期**，不是送出的日期。允許事後補登，所以兩者會差開；
    # 聚合一律以這個為準，否則翻舊帳的回報會被當成今天的行情
    observed_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # -- 佐證 -------------------------------------------------------------
    # 價格標籤或收據的照片。**上傳管道還沒接**，目前只存外部網址，
    # 有照片的回報在信譽計算上給較高權重
    photo_url: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    # -- 狀態與防濫用 ------------------------------------------------------
    status: Mapped[RetailReportStatus] = mapped_column(
        pg_enum(RetailReportStatus, "retail_report_status"),
        nullable=False,
        default=RetailReportStatus.ACTIVE,
        index=True,
    )
    excluded_reason: Mapped[RetailExclusion | None] = mapped_column(
        pg_enum(RetailExclusion, "retail_exclusion")
    )
    # 寫入當下的信譽權重。之後信譽變動不該追溯改寫已算過的看板
    weight_snapshot: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    ip_hosting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ip_proxy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    product: Mapped["Product"] = relationship()
    user: Mapped["User"] = relationship()

    @property
    def is_counted(self) -> bool:
        return self.status is RetailReportStatus.ACTIVE and self.excluded_reason is None
