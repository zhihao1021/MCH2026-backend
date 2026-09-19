"""官方批發價格與 ingest 執行紀錄。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, pg_enum
from app.models.catalog import DataSource, Market, Product, ProductSourceMapping
from app.models.enums import IngestStatus


class OfficialPrice(Base):
    """單一市場、單一品項、單一交易日的官方行情。

    以 BIGSERIAL 當 PK：這張表會是整個系統最大的，
    而且永遠用 (product, market, date) 查，不需要 UUID 的分散特性。
    """

    __tablename__ = "official_prices"
    __table_args__ = (
        # 同一來源同市場同品項同日同等級只留一筆，重跑時走 upsert
        UniqueConstraint(
            "source_id",
            "market_id",
            "mapping_id",
            "trade_date",
            "grade",
            name="uq_official_prices_source_market_mapping_date_grade",
        ),
        # 主要查詢：某品項近 N 天的走勢
        Index("ix_official_prices_product_id_trade_date", "product_id", "trade_date"),
        Index("ix_official_prices_market_id_trade_date", "market_id", "trade_date"),
        Index("ix_official_prices_trade_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    mapping_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_source_mappings.id", ondelete="CASCADE"), nullable=False
    )
    # 由 mapping 冗餘展開，讓「查某品項的所有國家價格」不必 join 兩層。
    # mapping 補上對應後，ingest 與回填工作會一併更新這個欄位。
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL")
    )

    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    # 等級 / 規格，來源沒有分級時填空字串（NULL 在 UNIQUE 中不相等，會造成重複）
    grade: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    price_avg: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    price_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    price_mid: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    price_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))
    volume_unit: Mapped[str | None] = mapped_column(String(16))

    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source: Mapped[DataSource] = relationship()
    market: Mapped[Market] = relationship(back_populates="prices")
    mapping: Mapped[ProductSourceMapping] = relationship()
    product: Mapped[Product | None] = relationship()


class IngestRun(Base):
    """每次 extension 抓取的執行紀錄，供監控與除錯。"""

    __tablename__ = "ingest_runs"
    __table_args__ = (Index("ix_ingest_runs_source_id_started_at", "source_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IngestStatus] = mapped_column(
        pg_enum(IngestStatus, "ingest_status"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(24), nullable=False, default="schedule")

    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)

    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    markets_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mappings_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    source: Mapped[DataSource] = relationship()
