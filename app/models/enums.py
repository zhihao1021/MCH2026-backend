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


class UnitSystem(StrEnum):
    """使用者偏好的度量衡制。

    農產品在美國以磅計價、在華語圈以公斤或台斤計價，
    所以顯示單位是使用者偏好，不是全域常數。
    """

    METRIC = "metric"       # kg
    IMPERIAL = "imperial"   # lb


class LocationVisibility(StrEnum):
    """位置對其他使用者公開到什麼程度。

    小農報價時會連帶露出所在地，但住家地址與精確座標屬於個資，
    所以預設只公開到行政區層級，要更精確得由使用者自己選。
    """

    EXACT = "exact"                # 完整地址與座標
    APPROXIMATE = "approximate"    # 座標模糊化到約 1 公里，不給地址
    REGION = "region"              # 只到行政區 / 城鎮（預設）
    PRIVATE = "private"            # 只顯示國家


class OtpPurpose(StrEnum):
    LOGIN = "login"
    PHONE_CHANGE = "phone_change"


class IngestStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
