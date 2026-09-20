"""demo_mock 的實作：印度 APMC mandi 的模擬行情。

雖然是假資料，價格**不是純亂數**——亂數產生的曲線在前端看起來很假，
也沒辦法用來驗證漲跌顯示、產季標示這類功能。這裡疊了四層真實世界的效應：

1. **產季**：盛產期到貨量大、價格低；產季外若仍供應（有冷藏或分批產區）
   則明顯偏高；完全不在產季的作物**不會有報價**，而不是報一個假價格。
2. **季風**：不耐儲運的蔬果在雨季供應受阻、價格上揚。印度各地雨季不同，
   坦米爾那都靠的是 10–12 月的東北季風，所以用市場自己的 `monsoon_months`。
3. **休市**：mandi 週日休市。
4. **雜訊**：以雜湊當種子，同樣的輸入永遠得到同樣的價格，測試才好斷言。

作物與市場清單見 `catalogue.py`，那是與 seed 腳本共用的單一事實來源。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from pydantic import Field

from app.extensions.base import (
    ExtensionConfig,
    FetchWindow,
    PriceSource,
    RawMarket,
    RawPrice,
    SourceManifest,
)
from app.extensions.demo_mock.catalogue import MARKETS, PRODUCTS, DemoMarket, DemoProduct

# 價格相對於基準價的倍率
PEAK_FACTOR = Decimal("0.78")       # 盛產期：到貨量最大，價格最低
IN_SEASON_FACTOR = Decimal("1.00")  # 產季內但非盛產
OFF_SEASON_FACTOR = Decimal("1.32")  # 產季外仍供應（冷藏 / 其他產區調入）
MONSOON_FACTOR = Decimal("1.25")    # 雨季的不耐儲蔬果

# mandi 的週休。印度多數批發市場週日不交易
CLOSED_WEEKDAY = 6  # Monday=0 ... Sunday=6

_CENT = Decimal("0.01")


class DemoMockConfig(ExtensionConfig):
    """`EXTENSIONS_CONFIG` 裡 demo_mock 這個 key 底下可以放的欄位。"""

    # 價格波動幅度（±%），調大可以測試前端的漲跌顯示
    volatility_pct: int = Field(default=12, ge=0, le=90)
    # 只產生這些市場代碼；留空代表全部
    markets: list[str] = Field(default_factory=list)
    # 只產生這些作物代碼；留空代表全部
    products: list[str] = Field(default_factory=list)
    # 關掉之後產季外的作物也會有報價，方便一次灌滿所有品項
    respect_seasons: bool = True


class DemoMockSource(PriceSource):
    manifest = SourceManifest(
        key="demo_mock",
        name="India APMC Mandi (Demo)",
        country_code="IN",
        timezone="Asia/Kolkata",
        currency="INR",
        default_unit="kg",
        version="2.0.0",
        description="印度 APMC 批發市場的模擬行情，依產季與季風生成",
        license="MIT",
        schedule="*/30 * * * *",
        lookback_days=3,
        max_window_days=400,
    )
    config_model = DemoMockConfig

    # -- 市場 -------------------------------------------------------------
    def _markets(self) -> list[DemoMarket]:
        wanted = set(self.config.markets)
        return [m for m in MARKETS if not wanted or m.code in wanted]

    def _products(self) -> list[DemoProduct]:
        wanted = set(self.config.products)
        return [p for p in PRODUCTS if not wanted or p.code in wanted]

    async def fetch_markets(self) -> Sequence[RawMarket]:
        return [
            RawMarket(
                external_id=m.code,
                name=m.name,
                name_en=m.name,
                region=m.region,
                timezone=self.manifest.timezone,
                latitude=m.latitude,
                longitude=m.longitude,
                raw={"demo": True, "subdivision_code": m.subdivision},
            )
            for m in self._markets()
        ]

    # -- 價格 -------------------------------------------------------------
    async def fetch_prices(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        markets = {m.code: m for m in self._markets()}
        products = self._products()

        day = window.start
        while day <= window.end:
            if day.weekday() != CLOSED_WEEKDAY:
                for product in products:
                    factor = self._season_factor(product, day.month)
                    if factor is None:
                        continue  # 不在產季且沒有全年供應：當天就是沒有這個作物
                    for code in product.markets:
                        market = markets.get(code)
                        if market is not None:
                            yield self._make_price(market, product, factor, day)
            day += timedelta(days=1)

    def _season_factor(self, product: DemoProduct, month: int) -> Decimal | None:
        """這個月份的季節價格倍率。回 `None` 代表當月沒有供應。"""
        if not self.config.respect_seasons:
            return IN_SEASON_FACTOR

        in_season = False
        for season in product.seasons:
            if not _covers(season.start_month, season.end_month, month):
                continue
            if season.is_peak:
                return PEAK_FACTOR  # 盛產期優先，價格最低
            in_season = True

        if in_season:
            return IN_SEASON_FACTOR
        # 產季外：有冷藏或其他產區調入的才有貨，而且明顯偏貴
        return OFF_SEASON_FACTOR if product.year_round else None

    def _make_price(
        self, market: DemoMarket, product: DemoProduct, factor: Decimal, day: date
    ) -> RawPrice:
        # 用雜湊當亂數種子：同樣的輸入永遠得到同樣的價格，測試才好斷言
        seed = hashlib.sha256(f"{market.code}{product.code}{day}".encode()).digest()

        price = Decimal(product.base_price) * factor

        # 雨季：不耐儲運的蔬果供應受阻。各地雨季月份不同
        monsoon = product.perishable and day.month in market.monsoon_months
        if monsoon:
            price *= MONSOON_FACTOR

        swing = (seed[0] / 255 * 2 - 1) * self.config.volatility_pct / 100
        price *= Decimal(1) + Decimal(str(round(swing, 4)))
        avg = price.quantize(_CENT, rounding=ROUND_HALF_UP)

        # 到貨量：盛產期最大，產季外最小。雨季再打折（運輸受阻）
        arrivals = Decimal(3000 + seed[1] * 40)
        if factor == PEAK_FACTOR:
            arrivals *= Decimal("2.4")
        elif factor == OFF_SEASON_FACTOR:
            arrivals *= Decimal("0.35")
        if monsoon:
            arrivals *= Decimal("0.7")

        return RawPrice(
            market_external_id=market.code,
            market_name=market.name,
            product_code=product.code,
            product_name=product.name_en,
            trade_date=day,
            currency=self.manifest.currency,
            unit=self.manifest.default_unit,
            # FAQ = Fair Average Quality，印度 mandi 的標準品級用語
            grade="FAQ" if product.category.value == "grain" else "",
            price_avg=avg,
            price_high=(avg * Decimal("1.18")).quantize(_CENT, rounding=ROUND_HALF_UP),
            price_low=(avg * Decimal("0.85")).quantize(_CENT, rounding=ROUND_HALF_UP),
            volume=arrivals.quantize(Decimal("1")),
            volume_unit="kg",
            raw={
                "seed": seed[:4].hex(),
                "base_price": product.base_price,
                "season_factor": str(factor),
                "monsoon": monsoon,
                "state": market.region,
                "subdivision_code": market.subdivision,
            },
        )

    async def healthcheck(self) -> bool:
        return True


def _covers(start: int, end: int, month: int) -> bool:
    """月份區間含頭含尾，且允許跨年（11–4 代表 11、12、1、2、3、4）。"""
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end
