"""跨模組共用的列舉。值一律用小寫字串，直接當 PostgreSQL enum 型別存。"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    CONSUMER = "consumer"   # 一般消費者，只讀
    FARMER = "farmer"       # 小農，可報價
    TRADER = "trader"       # 盤商，可報價


class ProductCategory(StrEnum):
    VEGETABLE = "vegetable"
    FRUIT = "fruit"
    FLOWER = "flower"
    GRAIN = "grain"
    LIVESTOCK = "livestock"
    FISHERY = "fishery"
    OTHER = "other"


class QuoteSide(StrEnum):
    SELL = "sell"   # 我要賣（小農常用）
    BUY = "buy"     # 我要收（盤商常用）


class QuoteStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    HIDDEN = "hidden"       # 遭檢舉或違規下架


class OtpPurpose(StrEnum):
    LOGIN = "login"
    PHONE_CHANGE = "phone_change"


class IngestStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
