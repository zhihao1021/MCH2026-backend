"""官方價格資料源 Extension 的公開契約。

一個 extension 只做三件事：宣告自己是誰（manifest）、回報有哪些市場
（fetch_markets）、以及吐出價格紀錄（fetch_prices）。它不碰資料庫、
不認識 ORM、也不需要知道平台的品項是怎麼分類的——正規化與寫入一律
由 `app.services.ingest` 負責。

實作細節與範例請見專案根目錄的 EXTENSIONS.md。
"""

from __future__ import annotations

import abc
import logging
import re
from collections.abc import AsyncIterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

__all__ = [
    "ExtensionConfig",
    "FetchWindow",
    "PriceSource",
    "RawMarket",
    "RawPrice",
    "SourceContext",
    "SourceManifest",
]


class ExtensionConfig(BaseModel):
    """Extension 自訂組態的基底。

    子類別宣告的欄位會從 `.env` 的 `EXTENSIONS_CONFIG` JSON 中
    對應 key 的物件填入，例如：

        EXTENSIONS_CONFIG={"tw_moa": {"api_key": "abc", "markets": ["104"]}}
    """

    model_config = ConfigDict(extra="forbid")


class SourceManifest(BaseModel):
    """Extension 的靜態宣告。registry 會據此在 DB 建立 / 更新 data_sources。"""

    model_config = ConfigDict(frozen=True)

    # 全域唯一識別碼，慣例是 <國碼小寫>_<機構>，例如 tw_moa、jp_tokyo
    key: str
    name: str
    country_code: str = Field(min_length=2, max_length=2)
    # IANA 時區，用來把來源的當地日期換算成正確的交易日
    timezone: str = "UTC"
    currency: str = Field(min_length=3, max_length=3)
    # 這個來源的價格預設單位；個別紀錄仍可自行覆寫
    default_unit: str = "kg"

    version: str = "0.1.0"
    description: str | None = None
    homepage_url: str | None = None
    license: str | None = None

    # 5 欄位 cron 字串（分 時 日 月 週），由 APScheduler 解讀
    schedule: str = "30 * * * *"
    # 每次排程執行時往回補幾天，用來吸收來源的延遲更新與訂正
    lookback_days: int = Field(default=3, ge=0, le=365)
    # 單次抓取允許的最長區間；超過時框架會自動切成多段
    max_window_days: int = Field(default=31, ge=1, le=3650)
    # 是否支援 fetch_markets()；不支援時市場會由價格紀錄隱式建立
    provides_markets: bool = True

    @field_validator("key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        if not KEY_PATTERN.match(v):
            raise ValueError(
                f"invalid manifest key {v!r}: 必須是小寫字母開頭的 snake_case，2-63 字元"
            )
        return v

    @field_validator("country_code")
    @classmethod
    def _upper_country(cls, v: str) -> str:
        return v.upper()

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()

    @field_validator("schedule")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        if len(v.split()) != 5:
            raise ValueError(f"schedule 必須是 5 欄位 cron 字串，收到 {v!r}")
        return v


class FetchWindow(BaseModel):
    """框架交給 extension 的抓取區間。"""

    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    # 上一次成功執行留下的游標，內容形狀完全由 extension 自己決定
    cursor: dict[str, Any] = Field(default_factory=dict)
    # True 代表這是手動觸發的歷史回補，extension 可據此放寬速率限制
    is_backfill: bool = False

    @model_validator(mode="after")
    def _check_order(self) -> "FetchWindow":
        if self.end < self.start:
            raise ValueError("FetchWindow.end 不能早於 start")
        return self

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


class RawMarket(BaseModel):
    """Extension 回報的市場。以 external_id 為主鍵對應到平台的 markets。"""

    external_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    name_en: str | None = Field(default=None, max_length=160)
    region: str | None = Field(default=None, max_length=80)
    timezone: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    is_active: bool = True
    raw: dict[str, Any] = Field(default_factory=dict)


class RawPrice(BaseModel):
    """一筆官方行情。這是 extension 與框架之間唯一的資料交換格式。"""

    # 對應 RawMarket.external_id；若來源沒有市場概念，用固定字串如 "national"
    market_external_id: str = Field(min_length=1, max_length=64)
    market_name: str | None = Field(default=None, max_length=160)

    # 來源自己的品項代碼。沒有代碼時用名稱當代碼，但要保證同一品項恆定
    product_code: str = Field(min_length=1, max_length=80)
    product_name: str | None = Field(default=None, max_length=200)

    trade_date: date
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    unit: str | None = Field(default=None, max_length=16)
    # 等級 / 規格。來源沒有分級時留空
    grade: str = Field(default="", max_length=40)

    price_avg: Decimal | None = None
    price_high: Decimal | None = None
    price_mid: Decimal | None = None
    price_low: Decimal | None = None
    volume: Decimal | None = None
    volume_unit: str | None = Field(default=None, max_length=16)

    # 原始欄位，整筆保留以便日後回溯或補算
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v

    @model_validator(mode="after")
    def _need_a_price(self) -> "RawPrice":
        if all(
            p is None for p in (self.price_avg, self.price_high, self.price_mid, self.price_low)
        ):
            raise ValueError("RawPrice 至少要有一個價格欄位")
        return self

    def effective_avg(self) -> Decimal | None:
        """沒有平均價時，用中價或高低價推估，讓前端永遠有數字可顯示。"""
        if self.price_avg is not None:
            return self.price_avg
        if self.price_mid is not None:
            return self.price_mid
        bounds = [p for p in (self.price_high, self.price_low) if p is not None]
        if bounds:
            return sum(bounds) / Decimal(len(bounds))
        return None


class SourceContext:
    """Extension 執行期可以用的東西。由框架建立並注入。"""

    def __init__(
        self,
        *,
        key: str,
        config: ExtensionConfig,
        http: httpx.AsyncClient,
        logger: logging.Logger,
    ) -> None:
        self.key = key
        self.config = config
        self.http = http
        self.logger = logger


class PriceSource(abc.ABC):
    """所有官方價格 extension 的基底類別。

    子類別必須設定 `manifest`，並實作 `fetch_prices`。
    """

    manifest: ClassVar[SourceManifest]
    # 對應 EXTENSIONS_CONFIG 內該 key 的組態模型
    config_model: ClassVar[type[ExtensionConfig]] = ExtensionConfig

    def __init__(self, ctx: SourceContext) -> None:
        self.ctx = ctx

    # -- 便利存取 ---------------------------------------------------------
    @property
    def config(self) -> Any:
        return self.ctx.config

    @property
    def http(self) -> httpx.AsyncClient:
        return self.ctx.http

    @property
    def log(self) -> logging.Logger:
        return self.ctx.logger

    # -- 生命週期 ---------------------------------------------------------
    async def setup(self) -> None:
        """每次 ingest 開始前呼叫一次。可在此取 token、載入對照表。"""

    async def teardown(self) -> None:
        """ingest 結束（無論成功失敗）後呼叫。釋放自建資源用。"""

    # -- 資料 -------------------------------------------------------------
    async def fetch_markets(self) -> Sequence[RawMarket]:
        """回報這個來源的市場清單。

        預設回空清單：這時框架會依價格紀錄中的 market_external_id
        自動建立市場，名稱取自 RawPrice.market_name。
        """
        return []

    @abc.abstractmethod
    def fetch_prices(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        """產出區間內的價格紀錄。

        必須是 async generator（用 `async def` + `yield` 實作）。
        框架會邊收邊批次寫入，因此請勿在記憶體中累積整份資料。
        丟出例外會讓整次 ingest 標記為失敗並保留原游標。
        """
        raise NotImplementedError

    async def next_cursor(self, window: FetchWindow, fetched: int) -> dict[str, Any]:
        """本次成功後要存回 DB 的游標。預設沿用原游標。"""
        return dict(window.cursor)

    async def healthcheck(self) -> bool:
        """來源是否可連線。給 /v1/sources 與維運用，失敗回 False 而非丟例外。"""
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} key={self.manifest.key}>"
