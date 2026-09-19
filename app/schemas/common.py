"""共用 schema 與回應形狀。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """所有非 2xx 回應的統一形狀。"""

    error: ErrorDetail


class Ack(BaseModel):
    ok: bool = True
    message: str | None = None


class HealthOut(BaseModel):
    status: str
    environment: str
    version: str
    database: str
    extensions_loaded: int
    extensions_failed: int
