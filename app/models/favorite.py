"""使用者收藏的作物。

功能機上要逐層點進品項看價格很累，所以讓使用者把常看的幾樣釘起來，
首頁一次列出「我關心的作物 + 最新價 + 漲跌」。

刻意做成獨立的關聯表而不是 users 上的陣列欄位：
收藏會隨品項刪除而自動清掉（FK CASCADE），也才查得動
「這個品項被幾個人收藏」這種反向問題。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, uuid_pk
from app.models.catalog import Product
from app.models.user import User


class ProductFavorite(Base):
    __tablename__ = "product_favorites"
    __table_args__ = (
        # 同一個人不能重複收藏同一個品項；加入 API 因此可以做成冪等的 PUT
        UniqueConstraint("user_id", "product_id", name="uq_product_favorites_user_id_product_id"),
        # 主查詢：某人的收藏，新加的排前面
        Index("ix_product_favorites_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship()
    product: Mapped[Product] = relationship()
