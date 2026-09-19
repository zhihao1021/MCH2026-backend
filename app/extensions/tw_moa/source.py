"""農業部農產品交易行情的資料源實作。

端點：https://data.moa.gov.tw/api/v1/AgriProductsTransType/
欄位：上/中/下/均價（元/公斤）、交易量（公斤）、作物代碼、市場代碼、
交易日（民國年 YYY.MM.DD）、種類代碼（N04 蔬菜 / N05 水果 / N06 花卉）。

兩個已知的 API 限制決定了實作方式：

1. 日期參數（Start_time / End_time）用的是民國年，例如 115.09.03。
2. 非會員每筆查詢最多只回傳第一頁（1000 筆），Page 參數形同虛設；
   有會員 API key（`api_key` 查詢參數）才能跨頁取完整資料。

因此 fetch_prices 有兩條路徑：

- 有 api_key：整個區間一次查詢、靠 Page/Next 翻頁；
- 沒有 api_key：逐日查詢，單日超過 1000 筆時改為逐市場切片
  （單一市場單日筆數遠小於 1000），市場清單以「近期的交易資料」
  動態發現，並以內建的市場代碼表當備援。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import Field

from app.extensions.base import (
    ExtensionConfig,
    FetchWindow,
    PriceSource,
    RawMarket,
    RawPrice,
    SourceManifest,
)
from app.extensions.http import request_with_retry

API = "https://data.moa.gov.tw/api/v1/AgriProductsTransType/"

# 非會員每筆查詢的上限：剛好等於上限也視為「可能被截斷」
PAGE_SIZE = 1000

# 農產品種類代碼：蔬菜 / 水果 / 花卉。用來在無 key 模式下輔助市場發現。
_TCTYPES = ("N04", "N05", "N06")

# 目前農產品交易行情站上有公告的批發市場代碼表。
# 用於：1) 市場發現失敗時的備援；2) 確保不會漏掉只交易單一類別的市場。
# 發現到的市場會覆蓋這裡的名稱，所以名稱過時也無妨。
_DEFAULT_MARKETS: dict[str, str] = {
    "104": "台北二",
    "105": "台北市場",
    "109": "台北一",
    "220": "板橋區",
    "241": "三重區",
    "260": "宜蘭市",
    "338": "桃農",
    "400": "台中市",
    "420": "豐原區",
    "423": "東勢鎮",
    "514": "彰化市場",
    "600": "嘉義市",
    "700": "台南市場",
    "800": "高雄市",
    "830": "鳳山區",
    "930": "台東市",
}

# 會員 key 無效、或非會員要翻第 2 頁之後時，伺服器會回 RS=ERROR + MSG
# （例：'API_ID&API_KEY不符,請檢查'、'非會員只限回傳第一頁資料'）
_MSG_KEY_REQUIRED = ("非會員", "限回傳", "不符", "會員")


class TwMoaConfig(ExtensionConfig):
    """`EXTENSIONS_CONFIG` 裡 tw_moa 這個 key 底下可以放的欄位。"""

    # data.moa.gov.tw 會員 API key；有 key 才能跨頁抓完整資料
    api_key: str = ""
    # 只抓這些市場代碼；留空代表全部
    markets: list[str] = Field(default_factory=list)
    # 每筆請求之間的間隔（秒），對官方站台禮貌一點
    request_delay: float = Field(default=0.2, ge=0, le=10)


class TwMoaSource(PriceSource):
    manifest = SourceManifest(
        key="tw_moa",
        name="農業部農產品交易行情",
        country_code="TW",
        timezone="Asia/Taipei",
        currency="TWD",
        default_unit="kg",
        version="1.0.0",
        description="農業部農產品批發市場交易行情：上/中/下/平均價與交易量",
        homepage_url="https://data.moa.gov.tw/",
        license="政府資料開放授權條款第1版",
        schedule="30 6 * * *",
        lookback_days=5,
        max_window_days=31,
        provides_markets=True,
    )
    config_model = TwMoaConfig

    async def setup(self) -> None:
        """每次 ingest 前重置快取：市場清單與 api_key 有效性。"""
        self._markets: dict[str, str] | None = None
        self._key_works: bool | None = None

    # -- 市場 -------------------------------------------------------------

    async def fetch_markets(self) -> Sequence[RawMarket]:
        markets = await self._load_markets()
        return [
            RawMarket(
                external_id=code,
                name=name,
                timezone=self.manifest.timezone,
                raw={"source": "data.moa.gov.tw"},
            )
            for code, name in sorted(markets.items())
        ]

    async def _load_markets(self) -> dict[str, str]:
        """回傳 {市場代碼: 名稱}。

        以內建代碼表當底，再從最近的交易資料補進新市場並更新名稱。
        結果會快取在 self._markets，整個 ingest 只發現一次。
        """
        if self._markets is not None:
            return self._markets

        found: dict[str, str] = dict(_DEFAULT_MARKETS)
        # 從今天往回找第一個有交易的日子；市場每日開市，一天就夠發現大部分市場
        for offset in range(10):
            probe = date.today() - timedelta(days=offset)
            try:
                rows = await self._query(probe)
            except Exception:
                self.log.warning("市場發現：查詢 %s 失敗，換前一天", probe, exc_info=True)
                continue
            if rows:
                for row in rows:
                    code = str(row.get("MarketCode") or "").strip()
                    name = str(row.get("MarketName") or "").strip()
                    if code:
                        found[code] = name or found.get(code, code)
                # 蔬菜 / 水果 / 花卉切片可以看到更完整的市場分佈
                for ttype in _TCTYPES:
                    try:
                        for row in await self._query(probe, TcType=ttype):
                            code = str(row.get("MarketCode") or "").strip()
                            name = str(row.get("MarketName") or "").strip()
                            if code:
                                found[code] = name or found.get(code, code)
                    except Exception:
                        self.log.warning(
                            "市場發現：TcType=%s 查詢失敗，忽略", ttype, exc_info=True
                        )
                break
        else:  # pragma: no cover - 網路全掛時走備援表
            self.log.warning("市場發現失敗，使用內建的市場代碼表（%d 個）", len(found))

        if self.config.markets:
            wanted = set(self.config.markets)
            unknown = wanted - set(found)
            if unknown:
                self.log.warning(
                    "組態指定的市場代碼不在發現結果中：%s（以代碼當名稱建立）",
                    sorted(unknown),
                )
            for code in unknown:
                found[code] = code
            found = {code: found[code] for code in found if code in wanted}

        self._markets = found
        self.log.info("市場清單：%s", found)
        return found

    # -- 價格 -------------------------------------------------------------

    async def fetch_prices(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        if self.config.api_key:
            if self._key_works is None:
                self._key_works = await self._probe_api_key()
            if self._key_works:
                async for raw in self._fetch_paged(window):
                    yield raw
                return
            self.log.warning("api_key 未生效（伺服器仍回非會員限制），改用切片模式")

        async for raw in self._fetch_sliced(window):
            yield raw

    async def _probe_api_key(self) -> bool:
        """用第 2 頁的查詢試探 api_key 是否有效：非會員只回得動第一頁。"""
        probe = date.today() - timedelta(days=1)
        try:
            data = await self._get(
                Start_time=_roc(probe), End_time=_roc(probe), Page="1",
                api_key=self.config.api_key,
            )
        except Exception:
            self.log.warning("api_key 試探失敗，假設無效", exc_info=True)
            return False
        # key 無效時 RS=ERROR（例如 'API_ID&API_KEY不符'）；有效時 RS=OK
        if data.get("RS") == "ERROR":
            self.log.warning("api_key 試探回 RS=ERROR：%s", data.get("MSG"))
            return False
        msg = data.get("MSG")
        if msg and any(token in msg for token in _MSG_KEY_REQUIRED):
            return False
        return True

    async def _fetch_paged(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        """有會員 key：整個區間一次查，靠 Page/Next 翻頁。"""
        params_base = {
            "Start_time": _roc(window.start),
            "End_time": _roc(window.end),
            "api_key": self.config.api_key,
        }
        page = 0
        while True:
            data = await self._get(**params_base, Page=str(page))
            await self._delay()
            _check_api(data)
            rows = data.get("Data") or []
            if not rows:
                break
            for row in rows:
                raw = self._parse(row)
                if raw is not None:
                    yield raw
            if not data.get("Next"):
                break
            page += 1

    async def _fetch_sliced(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        """無會員 key：逐日查詢；單日被 1000 筆截斷時改逐市場切片。"""
        markets = await self._load_markets()
        day = window.start
        while day <= window.end:
            data = await self._get(Start_time=_roc(day), End_time=_roc(day))
            await self._delay()
            _check_api(data)
            rows = data.get("Data") or []
            if len(rows) < PAGE_SIZE and not data.get("Next"):
                for row in rows:
                    raw = self._parse(row)
                    if raw is not None:
                        yield raw
            else:
                # 被截斷：改以市場代碼切片，每片都遠小於 1000 筆
                self.log.info(
                    "%s 超過 %d 筆（截斷），改用 %d 個市場切片", day, PAGE_SIZE, len(markets)
                )
                for code in markets:
                    try:
                        mdata = await self._get(
                            Start_time=_roc(day), End_time=_roc(day), MarketCode=code
                        )
                        await self._delay()
                    except Exception:
                        self.log.warning("市場 %s 查詢失敗，跳過", code, exc_info=True)
                        continue
                    # 系統性錯誤（例如配額用盡）就中止整批，留游標下次重試
                    _check_api(mdata)
                    mrows = mdata.get("Data") or []
                    if len(mrows) >= PAGE_SIZE:
                        self.log.warning(
                            "市場 %s 單日竟有 %d 筆（>= 上限），可能仍有漏抓", code, len(mrows)
                        )
                    for row in mrows:
                        raw = self._parse(row)
                        if raw is not None:
                            yield raw
            day += timedelta(days=1)

    # -- HTTP 基礎 --------------------------------------------------------

    async def _get(self, **params: str) -> dict:
        resp = await request_with_retry(self.http, "GET", API, params=params)
        return resp.json()

    async def _query(self, day: date, **extra: str) -> list[dict]:
        data = await self._get(Start_time=_roc(day), End_time=_roc(day), **extra)
        await self._delay()
        return data.get("Data") or []

    async def _delay(self) -> None:
        if self.config.request_delay > 0:
            await asyncio.sleep(self.config.request_delay)

    # -- 解析 -------------------------------------------------------------

    def _parse(self, row: dict) -> RawPrice | None:
        try:
            trade_date = _parse_roc_date(row["TransDate"])
            product_code = str(row.get("CropCode") or "").strip()
            if not product_code:
                self.log.warning("跳過一筆沒有作物代碼的資料：%s", row)
                return None

            prices = {
                "avg": _dec(row.get("Avg_Price")),
                "high": _dec(row.get("Upper_Price")),
                "mid": _dec(row.get("Middle_Price")),
                "low": _dec(row.get("Lower_Price")),
            }
            present = [v for v in prices.values() if v is not None]
            if not present or all(v == 0 for v in present):
                return None

            product_name = str(row.get("CropName") or "").strip() or None
            return RawPrice(
                market_external_id=str(row.get("MarketCode") or "").strip(),
                market_name=str(row.get("MarketName") or "").strip() or None,
                product_code=product_code,
                product_name=product_name,
                trade_date=trade_date,
                unit=self.manifest.default_unit,
                price_avg=prices["avg"],
                price_high=prices["high"],
                price_mid=prices["mid"],
                price_low=prices["low"],
                volume=_dec(row.get("Trans_Quantity")),
                volume_unit="kg",
                raw=row,
            )
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            self.log.warning("跳過一筆無法解析的資料：%s（%s）", row, exc)
            return None

    # -- 維運 -------------------------------------------------------------

    async def healthcheck(self) -> bool:
        probe = date.today() - timedelta(days=1)
        try:
            resp = await request_with_retry(
                self.http, "GET", API,
                params={
                    "Start_time": _roc(probe),
                    "End_time": _roc(probe),
                    "CropCode": "11",
                    "Page": "0",
                },
                attempts=1,
            )
            if resp.status_code >= 400:
                return False
            data = resp.json()
            return data.get("RS") == "OK"
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 日期換算：API 用民國年（YYYY.MM.DD），例如 2026-09-03 -> 115.09.03
# ---------------------------------------------------------------------------


def _check_api(data: dict) -> None:
    """伺服器明確回錯誤（例如配額用盡、key 無效）時丟例外中止整批。"""
    if data.get("RS") == "ERROR":
        raise ValueError(f"API 回 RS=ERROR：{data.get('MSG')}")


def _roc(day: date) -> str:
    return f"{day.year - 1911}.{day.month:02d}.{day.day:02d}"


def _parse_roc_date(value: str) -> date:
    parts = [int(p) for p in re.split(r"[^\d]+", str(value).strip()) if p]
    if len(parts) != 3:
        raise ValueError(f"無法解析日期 {value!r}")
    year, month, day = parts
    if year < 1000:  # 民國年；西元年不可能小於 1000
        year += 1911
    return date(year, month, day)


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))
