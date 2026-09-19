"""小農與盤商的自行報價。"""

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
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.catalog import Market, Product
from app.models.enums import QuoteSide, QuoteStatus, UserRole
from app.models.user import User


class Quote(Base, TimestampMixin):
    __tablename__ = "quotes"
    __table_args__ = (
        # 清單主查詢：某品項目前有效的報價，依時間排序
        Index("ix_quotes_product_id_status_created_at", "product_id", "status", "created_at"),
        Index("ix_quotes_user_id_created_at", "user_id", "created_at"),
        Index("ix_quotes_country_code_status", "country_code", "status"),
        Index("ix_quotes_status_valid_until", "status", "valid_until"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # 選填：若報價是針對某個批發市場
    market_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("markets.id", ondelete="SET NULL")
    )

    side: Mapped[QuoteSide] = mapped_column(
        pg_enum(QuoteSide, "quote_side"),
        nullable=False,
        default=QuoteSide.SELL,
    )
    # 快照當下的身分，之後使用者改角色也不影響歷史報價的呈現
    role_snapshot: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role", create_type=False), nullable=False
    )
    status: Mapped[QuoteStatus] = mapped_column(
        pg_enum(QuoteStatus, "quote_status"),
        nullable=False,
        default=QuoteStatus.ACTIVE,
    )

    price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="TWD")
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="kg")
    grade: Mapped[str | None] = mapped_column(String(40))

    quantity: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))
    min_order: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))

    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    # ISO 3166-2，例如 TW-TPE。**篩選請用這個欄位**——
    # region 是給人看的顯示字串，不同來源寫法不一致（臺北市 / 台北市），
    # 拿來比對會漏掉大半資料。
    subdivision_code: Mapped[str | None] = mapped_column(String(8), index=True)
    region: Mapped[str | None] = mapped_column(String(80), index=True)
    location_text: Mapped[str | None] = mapped_column(String(160))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    # 是否公開聯絡電話；不公開時 API 只回遮罩後的號碼
    contact_phone_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text)

    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="quotes")
    product: Mapped[Product] = relationship()
    market: Mapped[Market | None] = relationship()
