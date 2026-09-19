"""NAMIS 的實作：解析「Consumer Market Prices」那張表。

表格長這樣（第一列是市場名，第二列把每個市場拆成批發 / 零售兩欄）：

    | Commodity              | Unit | Fort Portal | Hoima | ... |
    |                        |      | W.P  | R.P  | W.P | R.P | ... |
    | Cereals > Maize Grain  | Kg   |      |      | 750 | 950 | ... |

所以一列會展開成「市場數 × 2」筆價格，空白代表該市場當天沒有回報。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from pydantic import Field
from slugify import slugify

from app.data.countries import list_subdivisions
from app.extensions.base import (
    ExtensionConfig,
    FetchWindow,
    PriceSource,
    RawMarket,
    RawPrice,
    SourceManifest,
)
from app.extensions.http import request_with_retry

BASE_URL = "https://www.nmis.infotradeconnect.com/index?tab=index"

# 這張表的標題文字，用來從頁面上眾多表格裡認出它。
# 不靠「第幾個 table」是因為站方加一個區塊就會整個錯位。
TABLE_HEADING = "Consumer Market Prices"

# 批發 / 零售在表頭的寫法
WHOLESALE = "W.P"
RETAIL = "R.P"

# 存進 official_prices.grade 的值。這個來源沒有「等級」的概念，
# 但同一個市場同一天會有兩個價格層級，剛好可以用 grade 區分——
# 去重鍵包含 grade，兩筆才不會互相覆蓋。
GRADE_WHOLESALE = "wholesale"
GRADE_RETAIL = "retail"

# 這個平台只收農作物（見 scripts/seed_products.py 的收錄範圍），
# 所以畜產、漁產、禽產預設濾掉，免得在待對應清單裡累積永遠不會用的代碼。
DEFAULT_EXCLUDED_CATEGORIES = ("Animal Products", "Fish", "Poultry Products")

# 「1,750」「1,750.50」都要吃得下；其他雜訊（"-"、"N/A"）一律視為沒有資料
_NUMBER = re.compile(r"^-?[\d,]+(?:\.\d+)?$")

# NAMIS 的市場名多半就是所在 district 的名字，所以優先用 ISO 3166-2 的
# district 清單去對；對不上的才在這裡指定。這樣新增市場時多半不用改程式。
_MARKET_DISTRICT_OVERRIDES = {
    # Fort Portal 是城市，所在的 district 叫 Kabarole
    "fort-portal": "Kabarole",
    # Owino（St. Balikuddembe）是坎帕拉最大的市場
    "owino": "Kampala",
}


class UgNamisConfig(ExtensionConfig):
    base_url: str = BASE_URL
    # 只抓這些市場（用 external_id，例如 "owino"）。留空 = 全部
    markets: list[str] = Field(default_factory=list)
    # 要不要一併收零售價。關掉就只留批發價
    include_retail: bool = True
    # 排除的品項大類。設成空陣列就全收（含畜產漁產）
    exclude_categories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_CATEGORIES)
    )


class UgNamisSource(PriceSource):
    manifest = SourceManifest(
        key="ug_namis",
        name="NAMIS – Uganda National Agro-Market Information Services",
        country_code="UG",
        timezone="Africa/Kampala",
        currency="UGX",
        default_unit="kg",
        version="1.0.0",
        description="烏干達全國農產品市場行情（消費市場批發價與零售價）",
        homepage_url="https://www.nmis.infotradeconnect.com/",
        license="© INFOTRADE / UWRSA，僅供參考用途",
        # 這個來源沒有歷史資料，錯過當天就補不回來，所以一天跑四次。
        # 同一天重複抓是冪等的（去重鍵一樣，走 upsert），代價只有幾次請求。
        schedule="20 */6 * * *",
        # 網頁上沒有交易日期欄位，只能抓「現在」，所以不往回補
        lookback_days=0,
        max_window_days=1,
        provides_markets=True,
    )
    config_model = UgNamisConfig

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._markets: list[RawMarket] = []

    # -- 生命週期 ---------------------------------------------------------
    async def setup(self) -> None:
        self._markets = []

    # -- 市場 -------------------------------------------------------------
    async def fetch_markets(self) -> Sequence[RawMarket]:
        """市場清單來自表頭，沒有另外的清單頁可以抓。"""
        if not self._markets:
            soup = await self._load()
            self._markets = self._parse_markets(soup)
        return self._markets

    def _parse_markets(self, soup: BeautifulSoup) -> list[RawMarket]:
        table = self._find_table(soup)
        if table is None:
            return []
        names = self._market_names(table)
        wanted = {m.lower() for m in self.config.markets}
        markets = []
        for name in names:
            external_id = slugify(name)
            if wanted and external_id not in wanted:
                continue
            district = self._district_for(external_id, name)
            markets.append(
                RawMarket(
                    external_id=external_id,
                    name=name,
                    name_en=name,
                    # 不填 region 的話，市場就不會出現在 /v1/markets/regions
                    # 的地區選單裡（那支端點會濾掉 region 為 NULL 的）
                    region=district,
                    timezone=self.manifest.timezone,
                    raw={"source": "namis_consumer_prices", "district": district},
                )
            )
        return markets

    @staticmethod
    def _district_for(external_id: str, name: str) -> str | None:
        """把市場對到所在的 district。

        先用 ISO 3166-2 的 district 清單比對同名的，對不上再查覆蓋表。
        用 ISO 而不是自己寫一份，名稱才不會跟平台其他地方對不起來。
        """
        override = _MARKET_DISTRICT_OVERRIDES.get(external_id)
        if override:
            return override
        wanted = name.strip().lower()
        for sub in list_subdivisions("UG", level=2):
            if sub.name.strip().lower() == wanted:
                return sub.name
        return None

    # -- 價格 -------------------------------------------------------------
    async def fetch_prices(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        """產出當天的價格。

        NAMIS 只提供最新快照，頁面上沒有交易日期，也沒有任何方式查歷史。
        所以這裡把抓到的資料記成「烏干達當地的今天」，
        並且在請求的區間不含今天時直接不產出——與其塞一個假的日期，
        不如明白地讓那一段沒有資料。
        """
        today = datetime.now(ZoneInfo(self.manifest.timezone)).date()
        if not (window.start <= today <= window.end):
            self.log.warning(
                "NAMIS 只有最新快照，無法提供 %s ~ %s 的歷史資料，跳過",
                window.start, window.end,
            )
            return

        soup = await self._load()
        table = self._find_table(soup)
        if table is None:
            self.log.error("頁面上找不到 %r 表格，網站結構可能改了", TABLE_HEADING)
            return

        markets = self._market_names(table)
        allowed = {m.external_id for m in self._parse_markets(soup)}
        excluded = {c.strip().lower() for c in self.config.exclude_categories}

        rows = table.select("tbody tr")
        self.log.info("NAMIS：%d 個市場、%d 列品項（交易日 %s）",
                      len(markets), len(rows), today)

        for row in rows:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            category, name = self._split_commodity(cells[0])
            if not name:
                continue
            if category and category.lower() in excluded:
                continue

            unit = self._normalize_unit(cells[1])
            values = cells[2:]

            for index, market_name in enumerate(markets):
                external_id = slugify(market_name)
                if external_id not in allowed:
                    continue
                # 每個市場佔兩欄：批發、零售
                wholesale = self._value(values, index * 2)
                retail = self._value(values, index * 2 + 1)

                if wholesale is not None:
                    yield self._price(
                        external_id, market_name, category, name, unit, today,
                        wholesale, GRADE_WHOLESALE, cells[0],
                    )
                if retail is not None and self.config.include_retail:
                    yield self._price(
                        external_id, market_name, category, name, unit, today,
                        retail, GRADE_RETAIL, cells[0],
                    )

    def _price(
        self,
        market_id: str,
        market_name: str,
        category: str,
        name: str,
        unit: str,
        day: date,
        value: Decimal,
        grade: str,
        raw_label: str,
    ) -> RawPrice:
        return RawPrice(
            market_external_id=market_id,
            market_name=market_name,
            product_code=self._product_code(category, name),
            product_name=name,
            trade_date=day,
            currency=self.manifest.currency,
            unit=unit,
            grade=grade,
            price_avg=value,
            raw={"category": category, "label": raw_label, "price_type": grade},
        )

    # -- 解析工具 ---------------------------------------------------------
    async def _load(self) -> BeautifulSoup:
        response = await request_with_retry(self.http, "GET", self.config.base_url)
        return BeautifulSoup(response.text, "html.parser")

    def _find_table(self, soup: BeautifulSoup):
        """靠標題文字定位表格，而不是靠它排第幾個。"""
        for heading in soup.find_all(string=re.compile(TABLE_HEADING, re.I)):
            table = getattr(heading, "find_next", lambda _: None)("table")
            if table is not None:
                return table
        # 標題文字被改掉時的退路：找欄位長得像的那張表
        for table in soup.find_all("table"):
            header = table.get_text(" ", strip=True)[:120]
            if "Commodity" in header and "Unit" in header:
                return table
        return None

    @staticmethod
    def _market_names(table) -> list[str]:
        """第一列表頭扣掉 Commodity / Unit 就是市場清單。"""
        head_rows = table.select("thead tr")
        if not head_rows:
            return []
        cells = [c.get_text(" ", strip=True) for c in head_rows[0].find_all("th")]
        skip = {"commodity", "unit", ""}
        return [c for c in cells if c.lower() not in skip]

    @staticmethod
    def _split_commodity(text: str) -> tuple[str, str]:
        """`Cereals > Maize Grain` → ("Cereals", "Maize Grain")。"""
        cleaned = re.sub(r"\s+", " ", text).strip()
        if ">" in cleaned:
            category, _, name = cleaned.partition(">")
            return category.strip(), name.strip()
        return "", cleaned

    def _product_code(self, category: str, name: str) -> str:
        """穩定的品項代碼。

        這個來源沒有自己的代碼系統，只有顯示用的名稱，所以用 slug 當代碼。
        大小寫、空白與標點的變動都會被 slugify 吸收掉，
        代碼才不會因為站方改個排版就整批跑掉、長出一堆重複的對照。
        """
        parts = [slugify(category), slugify(name)] if category else [slugify(name)]
        return ":".join(p for p in parts if p)[:80]

    @staticmethod
    def _normalize_unit(text: str) -> str:
        unit = re.sub(r"\s+", "", text).lower()
        return unit or "kg"

    @staticmethod
    def _value(values: list[str], index: int) -> Decimal | None:
        """把 "1,750" 轉成 Decimal；空白或非數字一律當作沒有資料。"""
        if index >= len(values):
            return None
        text = values[index].strip()
        if not text or not _NUMBER.match(text):
            return None
        try:
            price = Decimal(text.replace(",", ""))
        except InvalidOperation:
            return None
        # 0 或負數在行情裡沒有意義，多半是佔位符
        return price if price > 0 else None

    # -- 健康檢查 ---------------------------------------------------------
    async def healthcheck(self) -> bool:
        try:
            response = await self.http.get(self.config.base_url)
            if response.status_code >= 400:
                return False
            return self._find_table(BeautifulSoup(response.text, "html.parser")) is not None
        except Exception:
            return False
