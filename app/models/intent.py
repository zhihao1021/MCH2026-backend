"""消費者意向價格與其防刷機制。

與 `quotes`（小農／盤商的供給側報價）不同，這裡是**需求側**：
消費者回報「我願意用多少錢買」，平台聚合成區域意向錨點。

平台不涉入金流，沒有保證金也沒有預付款，所以惡意填一個超低價的成本是零。
防護靠四層，每一層在這裡都有對應的欄位：

1. 統計過濾 —— `excluded_reason = outlier`（IQR 判定，聚合時寫入）
2. 行為摩擦 —— `created_at` 撐冷卻期、`latitude/longitude` 撐地理圍欄、
   `ip_hosting` 撐機房 IP 判定
3. 信譽權重 —— `weight_snapshot` 記下提交當下的權重（之後權重變動不追溯）
4. 博弈約束 —— `intent_notifications` 追蹤推播後的實際響應
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.catalog import Product
from app.models.enums import IntentExclusion, IntentStatus
from app.models.quote import Quote
from app.models.user import User


class PriceIntent(Base, TimestampMixin):
    """一筆消費者意向價格。

    採**追加寫入**而非就地更新：同一人同品項再提交時，舊的轉成
    `superseded` 而不是覆蓋。信譽分要看歷史行為，覆蓋掉就算不出來了。
    """

    __tablename__ = "price_intents"
    __table_args__ = (
        # 看板主查詢：某品項某地區目前生效的意向
        Index("ix_price_intents_product_id_region_status", "product_id", "region", "status"),
        # 冷卻期檢查：這個人這個品項最近一次是什麼時候
        Index("ix_price_intents_user_id_product_id_created_at", "user_id", "product_id", "created_at"),
        Index("ix_price_intents_country_code_status", "country_code", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )

    price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    # 願意買多少（以 unit 計）。聚合後就是看板上的「需求總量」——
    # 對產地來說「450 人、共 1200 箱」比單純的價格共識更有行動價值，
    # 因為那直接決定要不要開一團。選填，不填只算人數不算量。
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))

    # 區域看板的分群鍵。取自使用者個人檔案的行政區，不讓前端自由指定，
    # 否則「台南東區的意向」可以被外地人隨便灌
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    region: Mapped[str | None] = mapped_column(String(80), index=True)

    # 提交當下的位置與距離，供地理圍欄稽核
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)

    status: Mapped[IntentStatus] = mapped_column(
        pg_enum(IntentStatus, "intent_status"), nullable=False, default=IntentStatus.ACTIVE
    )
    # NULL = 有計入看板。有值代表被哪一層防護擋下
    excluded_reason: Mapped[IntentExclusion | None] = mapped_column(
        pg_enum(IntentExclusion, "intent_exclusion")
    )

    # 提交當下的信譽權重。之後權重升降不追溯既有意向，
    # 否則一個人的行為會回頭改寫歷史看板數字
    weight_snapshot: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # 提交當下計算出來的成本底線，供稽核「當時為什麼判定過低」
    floor_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))

    # 來源 IP 的性質。機房／Proxy 的意向權重直接歸零
    ip_hosting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ip_proxy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship()
    product: Mapped[Product] = relationship()

    @property
    def is_counted(self) -> bool:
        return self.status is IntentStatus.ACTIVE and self.excluded_reason is None


class UserReputation(Base):
    """使用者的信譽權重。

    獨立成一張表而不是塞進 users：users 已經二十幾個欄位，
    而且信譽的更新節奏（每次聚合後重算）與個人檔案完全不同。
    """

    __tablename__ = "user_reputation"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # 0.0 ~ 2.0，新使用者從 1.0 開始
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 落在共識區間內的次數
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 偏離 2 個標準差以上的次數
    misses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_misses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 影子封禁：使用者照樣送得出去、也看得到自己的數字，
    # 但聚合時完全忽略。不明說是為了不讓對方知道要換帳號
    is_shadow_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shadow_banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 曾上傳過真實消費憑證（PRD 3.3）。目前沒有上傳端點，保留欄位
    has_verified_purchase: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship()


class IntentNotification(Base):
    """產地開團時推播給哪些意向使用者，以及他們有沒有響應。

    PRD 目標四：填了價格卻從不響應的人，活躍信用要扣——
    這張表就是判斷依據。
    """

    __tablename__ = "intent_notifications"
    __table_args__ = (
        Index("ix_intent_notifications_user_id_sent_at", "user_id", "sent_at"),
        Index("ix_intent_notifications_quote_id", "quote_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_intents.id", ondelete="SET NULL")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # 觸發這次推播的產地報價
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("quotes.id", ondelete="SET NULL")
    )

    offer_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    # 推播當下這個人填的意向價，用來事後檢視「他說願意出這麼多，結果呢」
    intent_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))

    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()
    product: Mapped[Product] = relationship()
    quote: Mapped[Quote | None] = relationship()
