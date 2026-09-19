"""品項、市場、資料來源與代碼對照表。

這裡是多國支援的核心：每個 extension 用自己的代碼系統送資料進來，
`ProductSourceMapping` 負責把「來源代碼」對應到平台的標準品項 `Product`，
因此不同國家的價格才能放在同一個時間序列上比較。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pg_enum, uuid_pk
from app.models.enums import ProductCategory

if TYPE_CHECKING:
    from app.models.price import OfficialPrice


class DataSource(Base, TimestampMixin):
    """一個已載入的官方價格 extension 在資料庫中的投影。

    由 registry 於啟動時 upsert，`key` 對應 extension 的 manifest key。
    """

    __tablename__ = "data_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(Text)
    license: Mapped[str | None] = mapped_column(String(160))
    version: Mapped[str | None] = mapped_column(String(32))

    # 這個來源目前是否可用（extension 未被載入時會設為 false）
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_installed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # 增量抓取用的游標，形狀由 extension 自行決定
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    markets: Mapped[list["Market"]] = relationship(back_populates="source")
    mappings: Mapped[list["ProductSourceMapping"]] = relationship(back_populates="source")


class Market(Base, TimestampMixin):
    """批發市場 / 交易地點。"""

    __tablename__ = "markets"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_markets_source_id_external_id"),
        Index("ix_markets_country_code_name", "country_code", "name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL"), index=True
    )
    # 來源系統中的市場代碼，例如台灣農業部的 "104"
    external_id: Mapped[str | None] = mapped_column(String(64))
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    # 行政區 / 都道府県，用來做區域篩選
    region: Mapped[str | None] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(160))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    source: Mapped[DataSource | None] = relationship(back_populates="markets")
    prices: Mapped[list["OfficialPrice"]] = relationship(back_populates="market")


class Product(Base, TimestampMixin):
    """平台標準品項。跨國比較的共同軸。"""

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    category: Mapped[ProductCategory] = mapped_column(
        pg_enum(ProductCategory, "product_category"),
        nullable=False,
        default=ProductCategory.OTHER,
        index=True,
    )
    # 顯示與換算的基準單位，例如 "kg"
    default_unit: Mapped[str] = mapped_column(String(16), nullable=False, default="kg")
    image_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 首頁熱門排序用，數字大的排前面
    popularity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    names: Mapped[list["ProductName"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )
    mappings: Mapped[list["ProductSourceMapping"]] = relationship(back_populates="product")

    def display_name(self, locale: str, fallback: str = "en") -> str:
        """取指定語系的主要名稱，找不到就退回 fallback，再退回任一個。"""
        for want in (locale, fallback):
            for n in self.names:
                if n.locale == want and n.is_primary:
                    return n.name
        return self.names[0].name if self.names else self.slug


class ProductName(Base):
    """品項的多語名稱與別名。別名同時作為搜尋詞。"""

    __tablename__ = "product_names"
    __table_args__ = (
        UniqueConstraint("product_id", "locale", "name", name="uq_product_names_product_id_locale_name"),
        Index("ix_product_names_locale_name", "locale", "name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # BCP 47，例如 zh-Hant / ja / en
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # 每個 (product, locale) 只有一筆 primary，其餘是別名
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    product: Mapped[Product] = relationship(back_populates="names")


class ProductSourceMapping(Base, TimestampMixin):
    """來源品項代碼 → 平台標準品項。

    Extension 只負責吐出自己的代碼；ingest 時若代碼未曾出現過會自動建一筆
    `product_id = NULL` 的待對應紀錄，價格照樣入庫，之後補對應即可回填。
    """

    __tablename__ = "product_source_mappings"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "external_code", name="uq_product_source_mappings_source_id_external_code"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_code: Mapped[str] = mapped_column(String(80), nullable=False)
    external_name: Mapped[str | None] = mapped_column(String(200))
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    # 來源單位（可能是「公斤」「kg」「箱」），換算成 product.default_unit 的倍率
    source_unit: Mapped[str | None] = mapped_column(String(32))
    unit_factor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # 人工確認過的對應不會被自動流程覆寫
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    source: Mapped[DataSource] = relationship(back_populates="mappings")
    product: Mapped[Product | None] = relationship(back_populates="mappings")
