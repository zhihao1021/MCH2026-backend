"""共用分頁工具。功能機頻寬有限，預設頁數刻意壓小。"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


class PageParams(BaseModel):
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="每頁筆數")] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, description="略過筆數")] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool

    @classmethod
    def build(cls, items: list[T], total: int, params: PageParams) -> "Page[T]":
        return cls(
            items=items,
            total=total,
            limit=params.limit,
            offset=params.offset,
            has_more=params.offset + len(items) < total,
        )
