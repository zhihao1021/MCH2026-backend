# Extension 規範：接一個新國家的官方價格

這份文件是寫給「要幫某個國家 / 機構接官方批發行情」的人看的。
讀完你應該能在一個檔案裡完成一個資料來源，不需要碰資料庫、ORM 或 API 層。

---

## 1. 設計前提

Extension 只負責三件事：

1. **宣告自己是誰** — `SourceManifest`（國家、幣別、時區、更新頻率）
2. **回報有哪些市場** — `fetch_markets()`（選用）
3. **吐出價格紀錄** — `fetch_prices(window)`（必要）

其餘一律由框架（`app/services/ingest.py`）處理：

| 框架負責 | 說明 |
| --- | --- |
| 建立 / 更新市場 | 依 `external_id` upsert 到 `markets` |
| 品項代碼對照 | 未見過的代碼自動建一筆待對應的 `product_source_mappings` |
| 單位換算 | 依對照表的 `unit_factor` 換算成品項標準單位 |
| 去重與更新 | 以 (來源, 市場, 品項代碼, 交易日, 等級) 為鍵做 upsert |
| 分批寫入 | 每 500 筆一次，不會把整份資料堆在記憶體 |
| 區間切段 | 超過 `max_window_days` 會自動拆成多段呼叫 |
| 游標保存 | 成功才更新，失敗保留原值下次重試 |
| 執行紀錄 | 寫入 `ingest_runs`，可從 `/v1/admin/ingest-runs` 查 |
| 排程 | 依 `manifest.schedule` 自動註冊 cron |

**你不會、也不應該在 extension 裡 import 任何 `app.models.*` 或 session。**

---

## 2. 目錄與載入規則

載入方式只有一種：掃描 `app/extensions/` 底下的子資料夾。三條硬規則：

```
app/extensions/
├── base.py          ← 框架，不要動
├── registry.py      ← 框架，不要動
├── http.py          ← 框架，不要動
├── demo_mock/       ← 範例，可以照抄
│   ├── __init__.py
│   └── source.py
└── tw_moa/          ← 你的新來源
    ├── __init__.py  ← 必須匯出 SOURCE
    └── source.py
```

1. 是個資料夾，含 `__init__.py`，名稱不以 `_` 開頭；
2. `__init__.py` 匯出名為 **`SOURCE`** 的屬性，且它是 `PriceSource` 的子類別；
3. **資料夾名稱必須等於 `SOURCE.manifest.key`。**

第 3 點是刻意的：目錄名就是這個來源在資料庫、API 與設定檔中的識別碼。
不讓兩者分岔，才不會出現「改了 manifest 但沒人發現」的情況。

載入失敗**不會**讓服務起不來。錯誤會被收集起來，從兩個地方看得到：

- 啟動日誌
- `GET /v1/sources` 回應中的 `load_errors`

| `reason` | 意思 |
| --- | --- |
| `import_failed` | `import app.extensions.<key>` 就炸了，多半是語法錯或缺套件 |
| `missing_source_attr` | `__init__.py` 沒有 `SOURCE` |
| `invalid_source_attr` | `SOURCE` 不是 `PriceSource` 的子類別 |
| `missing_manifest` | 類別上沒有 `manifest`，或型別不對 |
| `key_mismatch` | 資料夾名 ≠ `manifest.key` |
| `invalid_config` | `EXTENSIONS_CONFIG` 裡的設定不符合 `config_model` |

---

## 3. 契約

### 3.1 `SourceManifest`

```python
SourceManifest(
    key="tw_moa",                  # 必填，小寫 snake_case，= 資料夾名
    name="農業部農產品交易行情",      # 必填，顯示名稱
    country_code="TW",             # 必填，ISO 3166-1 alpha-2
    currency="TWD",                # 必填，ISO 4217
    timezone="Asia/Taipei",        # IANA 時區，預設 UTC
    default_unit="kg",             # 這個來源價格的預設單位
    version="1.0.0",
    description="...",
    homepage_url="https://data.moa.gov.tw/",
    license="政府資料開放授權條款第1版",
    schedule="30 6 * * *",         # 5 欄位 cron（分 時 日 月 週）
    lookback_days=3,               # 每次排程往回補幾天，吸收來源的延遲更新
    max_window_days=31,            # 單次抓取的最長區間，超過會自動切段
    provides_markets=True,         # False 代表不實作 fetch_markets()
)
```

`schedule` 的時區由 `.env` 的 `SCHEDULER_TIMEZONE` 決定，不是 manifest 的 `timezone`。
`manifest.timezone` 只用來描述來源的交易日語意。

`lookback_days` 值得多想一下：多數官方站台會在幾天後訂正數字，設成 0 會永遠拿到第一版。

### 3.2 `RawMarket`

```python
RawMarket(
    external_id="104",        # 必填，來源系統的市場代碼，之後都靠它對應
    name="台北二",
    name_en="Taipei No.2",
    region="臺北市",
    timezone="Asia/Taipei",
    latitude=25.07, longitude=121.51,
    is_active=True,
    raw={"任何原始欄位": "..."},
)
```

不實作 `fetch_markets()` 也可以：框架會依價格紀錄中的 `market_external_id`
自動建立市場，名稱取自 `RawPrice.market_name`。有清單就實作，資料會漂亮很多。

### 3.3 `RawPrice`

Extension 與框架之間**唯一**的資料交換格式。

```python
RawPrice(
    market_external_id="104",   # 必填，對應 RawMarket.external_id
    market_name="台北二",         # 選填，沒有 fetch_markets 時用來命名
    product_code="11",          # 必填，來源的品項代碼
    product_name="椰子",         # 選填，但強烈建議給，維運對照時看得懂
    trade_date=date(2026, 9, 17),
    currency="TWD",             # 省略則用 manifest.currency
    unit="kg",                  # 省略則用 manifest.default_unit
    grade="",                   # 等級 / 規格；沒有分級就留空字串
    price_avg=Decimal("19.2"),
    price_high=Decimal("27.1"),
    price_mid=Decimal("18.2"),
    price_low=Decimal("14.1"),
    volume=Decimal("1294"),
    volume_unit="kg",
    raw={...},                  # 整筆原始資料，日後回溯用
)
```

幾個約定：

- **至少要有一個價格欄位**，全空會被 pydantic 擋下來。
- `product_code` 必須**穩定**。來源沒有代碼時可以用名稱當代碼，
  但同一個品項在不同日期要拿到同一個字串，否則對照表會長出一堆重複。
- `grade` 用空字串而不是 `None`。去重鍵包含它，而 SQL 的 `NULL != NULL`
  會讓同一筆資料重複寫入。
- 錢用 `Decimal`，不要用 `float`。

### 3.4 `PriceSource`

```python
class MySource(PriceSource):
    manifest = SourceManifest(...)
    config_model = MyConfig            # 選填，預設是空組態

    async def setup(self) -> None: ...          # 每次 ingest 前呼叫一次
    async def teardown(self) -> None: ...       # 結束後必定呼叫
    async def fetch_markets(self) -> Sequence[RawMarket]: ...
    async def fetch_prices(self, window) -> AsyncIterator[RawPrice]: ...   # 必須實作
    async def next_cursor(self, window, fetched) -> dict: ...
    async def healthcheck(self) -> bool: ...
```

執行期可以用的東西（由框架注入）：

| 屬性 | 內容 |
| --- | --- |
| `self.http` | 共用的 `httpx.AsyncClient`，已帶好 User-Agent 與逾時 |
| `self.config` | `config_model` 驗證後的組態物件 |
| `self.log` | 專屬 logger，名稱是 `app.extensions.<key>` |

`fetch_prices` **必須是 async generator**（`async def` + `yield`）。
框架邊收邊寫，所以請不要在裡面把整份資料收集成 list 再一次 yield。

丟出例外 = 整次 ingest 標記失敗、游標保留原值、錯誤寫進 `ingest_runs.error`。
如果只是某幾筆資料有問題，就 `continue` 跳過並自己 `self.log.warning`，不要讓整批陣亡。

---

## 4. 組態

Extension 的私有設定放在 `.env` 的 `EXTENSIONS_CONFIG`，是一段 JSON：

```dotenv
EXTENSIONS_CONFIG={"tw_moa":{"api_key":"xxx","markets":["104","109"]},"jp_tokyo":{"dataset":"seika"}}
```

對應的宣告：

```python
from pydantic import Field
from app.extensions.base import ExtensionConfig

class TwMoaConfig(ExtensionConfig):
    api_key: str = ""
    markets: list[str] = Field(default_factory=list)   # 空 = 全部
```

`ExtensionConfig` 設了 `extra="forbid"`，打錯欄位名會在啟動時就以
`invalid_config` 報出來，而不是默默被忽略。

開關個別來源：

```dotenv
EXTENSIONS_DISABLED=demo_mock          # 黑名單
EXTENSIONS_ENABLED=tw_moa,jp_tokyo     # 白名單，設了就只載這些
```

---

## 5. 單位換算

價格能不能跨國比較，全看單位有沒有對齊。約定是：

> `unit_factor` = **1 個來源單位等於幾個品項標準單位**

例如來源報的是「一箱」，而品項的 `default_unit` 是 `kg`，一箱 10 公斤 → `unit_factor = 10`。
框架換算時**價格除以 factor、數量乘以 factor**，兩邊才會落在同一個尺度。

`unit_factor` 不是 extension 設的，而是維運時在對照表上設定：

```http
PUT /v1/admin/mappings/{mapping_id}
X-Admin-Token: ...

{"product_id": "…", "unit_factor": 10}
```

只有在對照已經指到某個品項時才會換算。還沒對應的資料照原樣存，
之後補上對照時 `product_id` 會被回填，但**歷史列的價格不會重算**——
所以如果來源的單位不是 1:1，請在第一次抓取前就把 `unit_factor` 設好。

---

## 6. 實作步驟

### 6.1 建立骨架

```bash
mkdir -p app/extensions/tw_moa
```

`app/extensions/tw_moa/__init__.py`：

```python
from app.extensions.tw_moa.source import TwMoaSource

SOURCE = TwMoaSource

__all__ = ["SOURCE", "TwMoaSource"]
```

`app/extensions/tw_moa/source.py`：

```python
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import date
from decimal import Decimal

from pydantic import Field

from app.extensions.base import (
    ExtensionConfig, FetchWindow, PriceSource, RawMarket, RawPrice, SourceManifest,
)
from app.extensions.http import request_with_retry

API = "https://example.org/api/v1/prices"


class TwMoaConfig(ExtensionConfig):
    api_key: str = ""
    markets: list[str] = Field(default_factory=list)


class TwMoaSource(PriceSource):
    manifest = SourceManifest(
        key="tw_moa",
        name="農業部農產品交易行情",
        country_code="TW",
        timezone="Asia/Taipei",
        currency="TWD",
        default_unit="kg",
        schedule="30 6 * * *",
        lookback_days=5,
        max_window_days=31,
    )
    config_model = TwMoaConfig

    async def fetch_markets(self) -> Sequence[RawMarket]:
        resp = await request_with_retry(self.http, "GET", f"{API}/markets")
        return [
            RawMarket(external_id=m["code"], name=m["name"], region=m.get("city"))
            for m in resp.json()
        ]

    async def fetch_prices(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
        page = 0
        while True:
            resp = await request_with_retry(
                self.http, "GET", API,
                params={"start": window.start.isoformat(),
                        "end": window.end.isoformat(),
                        "page": page},
            )
            rows = resp.json()["data"]
            if not rows:
                return

            for row in rows:
                try:
                    yield self._parse(row)
                except (KeyError, ValueError) as exc:
                    # 單筆壞掉不要拖垮整批
                    self.log.warning("跳過一筆無法解析的資料：%s（%s）", row, exc)
                    continue
            page += 1

    def _parse(self, row: dict) -> RawPrice:
        return RawPrice(
            market_external_id=row["MarketCode"],
            product_code=row["CropCode"],
            product_name=row.get("CropName"),
            trade_date=date.fromisoformat(row["TransDate"]),
            price_avg=Decimal(str(row["Avg_Price"])),
            price_high=Decimal(str(row["Upper_Price"])),
            price_low=Decimal(str(row["Lower_Price"])),
            volume=Decimal(str(row["Trans_Quantity"])),
            volume_unit="kg",
            raw=row,
        )

    async def healthcheck(self) -> bool:
        try:
            resp = await self.http.get(API, params={"limit": 1})
            return resp.status_code < 400
        except Exception:
            return False
```

### 6.2 確認有被載到

```bash
curl -s localhost:8000/v1/sources | python -m json.tool
```

`sources` 裡要有 `tw_moa`、`installed: true`，且 `load_errors` 是空的。

### 6.3 立即試抓

開發時用 CLI 最快，不必啟動服務：

```bash
# 先 dry-run：只呼叫 extension 並印出結果，完全不碰資料庫
python scripts/run_ingest.py tw_moa --dry-run --limit 5 --start 2026-09-01 --end 2026-09-07

# 確認解析正確後再真的寫入
python scripts/run_ingest.py tw_moa --start 2026-09-01 --end 2026-09-07
```

服務已經在跑的話，也可以打管理端點：

```bash
curl -X POST "localhost:8000/v1/admin/sources/tw_moa/sync?start=2026-09-01&end=2026-09-07" \
     -H "X-Admin-Token: $ADMIN_API_TOKEN"
```

兩種方式都會回 `fetched` / `written` / `skipped`，`skip_reasons` 說明被跳過的原因。

> 新增了 extension **資料夾**但服務已在執行：先 `POST /v1/admin/sources/reload` 重新掃描。
> 改的是 **程式碼** 則必須重啟——Python 不會重新載入已經 import 過的模組。

### 6.4 對照品項

```bash
# 看有哪些代碼還沒對應
curl -s "localhost:8000/v1/admin/mappings?source_key=tw_moa&unmapped_only=true" \
     -H "X-Admin-Token: $ADMIN_API_TOKEN"

# 接到標準品項上（會回填既有價格列的 product_id）
curl -X PUT "localhost:8000/v1/admin/mappings/<mapping_id>" \
     -H "X-Admin-Token: $ADMIN_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"product_id":"<product_id>","unit_factor":1}'
```

沒對應到品項的價格**還是會入庫**，只是不會出現在 `/v1/products/{id}/prices/*`。
先收資料、之後再補對照，比起把資料丟掉要好得多。

---

## 7. 測試

`tests/test_extensions.py` 裡的契約測試對所有 extension 都適用。要測自己的：

```python
@pytest.mark.anyio
async def test_my_source_parses_sample():
    from app.extensions.http import build_client
    from app.extensions.tw_moa import SOURCE

    reg = ExtensionRegistry()
    reg.register(SOURCE)              # 不必放進目錄也能測
    client = build_client(transport=httpx.MockTransport(handler))
    source = reg.get("tw_moa").instantiate(client)
    rows = [r async for r in source.fetch_prices(window)]
    assert rows[0].price_avg == Decimal("19.2")
```

`build_client(transport=...)` 可以塞 `httpx.MockTransport`，不用真的打網路。

---

## 8. 上線前檢查清單

- [ ] 資料夾名 == `manifest.key`，`__init__.py` 有匯出 `SOURCE`
- [ ] `country_code` / `currency` / `timezone` 正確（時區錯會讓交易日整個偏一天）
- [ ] `product_code` 在不同日期之間是穩定的
- [ ] `grade` 用空字串而非 `None`
- [ ] 價格用 `Decimal`
- [ ] 單筆解析失敗會跳過並記 log，不會炸掉整批
- [ ] 分頁有終止條件（別寫出無窮迴圈）
- [ ] `lookback_days` 足以涵蓋來源的訂正週期
- [ ] `schedule` 避開整點（大家都在整點打，官方站台容易掛）
- [ ] 尊重來源的授權條款，`license` 有填
- [ ] `GET /v1/sources` 看得到、`load_errors` 是空的
- [ ] 手動 sync 一次，確認 `written > 0`

---

## 9. 常見問題

**Q：來源沒有市場的概念，是全國均價怎麼辦？**
用一個固定的 `market_external_id`，例如 `"national"`，`market_name` 給「全國平均」。

**Q：要抓的資料是 CSV / Excel，不是 JSON？**
沒差。`fetch_prices` 只要求你 yield `RawPrice`，中間用什麼解析都行。
大檔案請用 `self.http.stream()` 邊下載邊解析。

**Q：一次要抓好幾年的歷史資料？**
用 `POST /v1/admin/sources/{key}/sync?start=...&end=...`。框架會依
`max_window_days` 自動切段，`window.is_backfill` 會是 `True`，
你可以據此調整速率。這是同步呼叫，範圍大時注意 HTTP 逾時。

**Q：要做增量抓取，不想每次都重抓？**
用游標。`next_cursor()` 回傳的 dict 會存進 `data_sources.cursor`，
下次以 `window.cursor` 交還給你。**只有成功才會更新**，失敗會保留舊值重試。

**Q：改了 extension 的程式碼，要重啟嗎？**
要。`POST /v1/admin/sources/reload` 只處理「新增或移除整個資料夾」，
Python 已經 import 過的模組不會重新載入。

**Q：可以在 extension 裡查資料庫嗎？**
不行，也不需要。需要跨批次記住的狀態請用游標。
如果你覺得非查不可，多半代表該邏輯應該放在 `app/services/ingest.py`。
