"""ORM 基底：命名慣例、共用欄位型別與 mixin。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from enum import Enum
from sqlalchemy import DateTime, Enum as SAEnum, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 固定 constraint 命名，否則 Alembic autogenerate 在改 index / FK 時會產出無名物件
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
    }

    def __repr__(self) -> str:  # pragma: no cover - 除錯用
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def pg_enum(enum_cls: type[Enum], name: str, **kwargs: Any) -> SAEnum:
    """建立 PostgreSQL 原生 enum 型別。

    預設 SQLAlchemy 會用 Python 成員「名稱」當資料庫的值（CONSUMER），
    這裡改成用 `.value`（consumer），讓資料庫、JSON API 與日誌看到的字串一致，
    手寫 SQL 查詢時也不用記兩套寫法。
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
        **kwargs,
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
