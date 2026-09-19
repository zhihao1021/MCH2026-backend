"""demo_mock 的實作。刻意寫得很短，好讓它能當作範本閱讀。"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from decimal import Decimal

from pydantic import Field

from app.extensions.base import (
    ExtensionConfig,
    FetchWindow,
    PriceSource,
    RawMarket,
    RawPrice,
    SourceManifest,
)

# (代碼, 名稱, 基準價)
_PRODUCTS: list[tuple[str, str, int]] = [
    ("MK-CAB", "高麗菜", 25),
    ("MK-TOM", "牛番茄", 48),
    ("MK-BAN", "香蕉", 32),
    ("MK-RAD", "白蘿蔔", 18),
    ("MK-SPI", "菠菜", 60),
]

_MARKETS: list[tuple[str, str, str]] = [
    ("DM01", "示範第一市場", "台北市"),
    ("DM02", "示範第二市場", "台中市"),
]


class DemoMockConfig(ExtensionConfig):
    """`EXTENSIONS_CONFIG` 裡 demo_mock 這個 key 底下可以放的欄位。"""

    # 價格波動幅度（±%），調大可以測試前端的漲跌顯示
    volatility_pct: int = Field(default=20, ge=0, le=90)
    # 只產生這些市場代碼；留空代表全部
    markets: list[str] = Field(default_factory=list)


class DemoMockSource(PriceSource):
    manifest = SourceManifest(
        key="demo_mock",
        name="Demo Mock Market",
        country_code="TW",
        timezone="Asia/Taipei",
        currency="TWD",
        default_unit="kg",
        version="1.0.0",
        description="離線開發與測試用的假資料來源",
        license="MIT",
        schedule="*/30 * * * *",
        lookback_days=3,
        max_window_days=90,
    )
    config_model = DemoMockConfig

    async def fetch_markets(self) -> Sequence[RawMarket]:
        wanted = set(self.config.markets)
        return [
            RawMarket(
                external_id=code,
                name=name,
                region=region,
                timezone=self.manifest.timezone,
                raw={"demo": True},
            )
            for code, name, region in _MARKETS
            if not wanted or code in wanted
        ]

    async def fetch_prices(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        markets = await self.fetch_markets()
        day = window.start
        while day <= window.end:
            # 假日休市，讓資料看起來像真的
            if day.weekday() < 6:
                for market in markets:
                    for code, name, base in _PRODUCTS:
                        yield self._make_price(market, code, name, base, day)
            day += timedelta(days=1)

    def _make_price(
        self, market: RawMarket, code: str, name: str, base: int, day: date
    ) -> RawPrice:
        # 用雜湊當亂數種子：同樣的輸入永遠得到同樣的價格，測試才好斷言
        seed = hashlib.sha256(f"{market.external_id}{code}{day}".encode()).digest()
        swing = (seed[0] / 255 * 2 - 1) * self.config.volatility_pct / 100
        avg = Decimal(base) * (Decimal(1) + Decimal(str(round(swing, 4))))
        avg = avg.quantize(Decimal("0.1"))
        return RawPrice(
            market_external_id=market.external_id,
            market_name=market.name,
            product_code=code,
            product_name=name,
            trade_date=day,
            currency=self.manifest.currency,
            unit=self.manifest.default_unit,
            price_avg=avg,
            price_high=(avg * Decimal("1.35")).quantize(Decimal("0.1")),
            price_low=(avg * Decimal("0.7")).quantize(Decimal("0.1")),
            volume=Decimal(seed[1] * 10),
            volume_unit="kg",
            raw={"seed": seed[:4].hex(), "base_price": base},
        )

    async def healthcheck(self) -> bool:
        return True
