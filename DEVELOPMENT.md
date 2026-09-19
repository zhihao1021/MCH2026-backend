# AgriPrice 後端開發說明

給維護與擴充這個後端的人看的文件。說明每個部件的職責、彼此如何串接、
以及資料怎麼從「官方資料源」一路流到前端 API。

- 面向前端的 API 規格：**[API.md](API.md)**
- 新增資料來源的完整契約：**[EXTENSIONS.md](EXTENSIONS.md)**
- 快速開始與維運清單：**[README.md](README.md)**

---

## 1. 總覽

這是一個給 Cloud Phone 功能機 App（QVGA 240×320 遠端渲染）使用的後端：

1. **官方批發行情** — 由可插拔的 extension 供應（每個國家 / 機構一個），
   框架負責排程抓取、正規化、單位換算、去重入庫。
2. **小農 / 盤商自行報價** — 手機 OTP 登入後自行刊登買 / 賣報價。

技術棧：**FastAPI + SQLAlchemy 2.0（async）+ PostgreSQL + Alembic + APScheduler**。
全非同步（asyncpg driver），背景工作與 API 共用同一個 event loop。

**設計前提**：前端跑在伺服器端渲染、串流到功能機，頻寬與運算都很吃緊。
因此 API 回應刻意壓小（走勢點用縮寫欄位）、聚合都算在後端
（`/products/{ref}/overview` 一次拿齊）、分頁預設只有 20 筆。

---

## 2. 目錄結構與分層職責

```
backend/
├── app/
│   ├── main.py                應用組裝 + lifespan
│   ├── core/                  框架層（不碰商業邏輯）
│   │   ├── config.py          .env → Settings（全專案唯一讀環境變數的地方）
│   │   ├── database.py        async engine / session 管理
│   │   ├── security.py        JWT、HMAC 雜湊、E.164 電話正規化
│   │   ├── deps.py            FastAPI 依賴注入（登入者、分頁、語系、admin 守衛）
│   │   ├── errors.py          統一錯誤型別與 JSON 形狀
│   │   ├── pagination.py      PageParams / Page[T]
│   │   └── logging.py         輕量 logging（可切 JSON）
│   ├── models/                SQLAlchemy ORM（11 張表）
│   ├── schemas/               Pydantic 請求 / 回應模型
│   ├── services/              商業邏輯
│   ├── api/v1/                HTTP 路由（薄，只做參數解析與呼叫 service）
│   └── extensions/            官方價格資料源（可插拔）
│       ├── base.py            契約（SourceManifest / RawPrice / PriceSource…）
│       ├── registry.py        目錄掃描與載入
│       ├── http.py            共用 HTTP client + 重試
│       └── demo_mock/         離線可用的假資料來源（範本）
├── alembic/                   DB migration
├── scripts/                   seed_products.py、smoke_test.py
├── tests/                     單元測試（不碰 DB）
├── requirements.txt
└── .env.example               所有環境變數的文件化
```

### 依賴方向（只能單向）

```
api/v1 ──▶ services ──▶ models / core
               │
               ▼
          extensions ──(只 import extensions.base / extensions.http，絕不 import models)
```

關鍵規則：

- **route 層薄**：只做參數解析、呼叫 service、把結果轉成 schema。商業邏輯全在 services。
- **services 是唯一能碰 ORM 的地方**（models + session）。
- **extension 完全不碰資料庫與 ORM**：只 yield `RawPrice`，正規化與寫入由
  `services/ingest.py` 代勞（見第 6 節）。
- **core 不依賴任何上層**；`config.py` 是唯一讀環境變數的入口，其他模組一律
  `from app.core.config import settings`。

---

## 3. 啟動流程

`app/main.py` 的 `lifespan` 依序做四件事：

```
啟動
 │
 ├─ 1. setup_logging()                依 LOG_LEVEL / LOG_JSON 設定 root logger
 │
 ├─ 2. registry.discover(force=True)  掃描 app/extensions/，載入所有 extension
 │      （失敗只記進 registry.errors，不會讓服務起不來）
 │
 ├─ 3. ingest.sync_all_source_rows()  把 manifest 內容 upsert 進 data_sources 表；
 │      消失的 extension 標 is_installed=false。
 │      DB 還沒 migrate 時只 log 警告，不阻止啟動（解決首次部署雞生蛋問題）
 │
 ├─ 4. start_scheduler()              依各 manifest.schedule 註冊 APScheduler cron 工作
 │      + 一個每 10 分鐘的過期報價清理工作
 │      （SCHEDULER_RUN_ON_STARTUP=true 時再非同步補跑一次全來源）
 │
 └─ yield（提供服務）… shutdown → 停排程、dispose engine
```

`/healthz` 每次執行 `SELECT 1` 檢查 DB，並回報 extension 載入數量與失敗數。

---

## 4. 設定系統（app/core/config.py）

`Settings` 是 pydantic-settings 的 `BaseSettings`，讀 `backend/.env`（`extra="ignore"`，
大小寫不敏感）。全專案以 `settings` 單例（lru_cache）存取。

分類摘要（完整註解見 `.env.example`）：

| 群組 | 關鍵變數 | 說明 |
| --- | --- | --- |
| 應用 | `ENVIRONMENT` `DEBUG` `API_PREFIX` `CORS_ORIGINS` | CORS 允許逗號分隔或 JSON 陣列 |
| 資料庫 | `DATABASE_URL`（asyncpg） `DB_POOL_*` | `sqlalchemy_url` property 供 async 引擎；`sync_sqlalchemy_url` 自動換成 psycopg2 給 Alembic |
| 安全 | `SECRET_KEY` `JWT_ALGORITHM` `ACCESS_TOKEN_TTL_MINUTES` `REFRESH_TOKEN_TTL_DAYS` `OTP_PEPPER` | JWT 與 OTP 雜湊分用兩把鑰，可獨立輪替 |
| OTP | `OTP_LENGTH` `OTP_TTL_SECONDS` `OTP_MAX_ATTEMPTS` `OTP_RESEND_COOLDOWN_SECONDS` `OTP_MAX_PER_PHONE_PER_HOUR` `OTP_DEBUG_ECHO` | `OTP_DEBUG_ECHO` 只在非 production 生效 |
| 簡訊 | `SMS_PROVIDER`（console/twilio） `TWILIO_*` | 見 8.2 |
| 報價 | `QUOTE_DEFAULT_TTL_HOURS`(48) `QUOTE_MAX_ACTIVE_PER_USER`(50) | |
| Extension | `EXTENSIONS_DIR` `EXTENSIONS_ENABLED/DISABLED` `EXTENSIONS_CONFIG` `EXTENSION_HTTP_TIMEOUT` | enabled 為白名單、disabled 為黑名單；config 是 JSON dict（key=資料夾名） |
| 排程 | `SCHEDULER_ENABLED` `SCHEDULER_TIMEZONE` `SCHEDULER_RUN_ON_STARTUP` | 多 instance 部署時只有一台開 scheduler |
| 維運 | `ADMIN_API_TOKEN` `LOG_LEVEL` `LOG_JSON` | `ADMIN_API_TOKEN` 未設定時 `/admin/*` 全部 403（fail-closed） |

---

## 5. 資料模型（app/models/）

11 張表。ER 圖：

```
data_sources ──┬── markets ──────────────┐
               │                          ├── official_prices ──┬── product_source_mappings ── products ── product_names
               └── product_source_mappings ┤                    └───────────────────────────────────────┘
                                          │
users ── quotes ──────────────────────────┘（quotes.product_id / quotes.market_id）
users ── otp_codes / refresh_tokens
data_sources ── ingest_runs
```

| 表 | 檔 | 用途 |
| --- | --- | --- |
| `data_sources` | catalog.py | 每個 extension 在 DB 的投影：游標、最後執行狀態。啟動時由 manifest upsert |
| `markets` | catalog.py | 批發市場，以 (source_id, external_id) 唯一 |
| `products` | catalog.py | 平台標準品項，**跨國比較的共同軸** |
| `product_names` | catalog.py | 品項多語名稱與別名（BCP 47 locale）；搜尋比對這張表 |
| `product_source_mappings` | catalog.py | 來源代碼 → 標準品項；含 `unit_factor` |
| `official_prices` | price.py | 官方行情。以 (來源, 市場, mapping, 交易日, 等級) 唯一，重跑走 upsert |
| `ingest_runs` | price.py | 每次抓取的執行紀錄 |
| `users` / `otp_codes` / `refresh_tokens` | user.py | 手機 OTP 登入。無密碼欄位 |
| `quotes` | quote.py | 民間報價 |

### 5.1 兩個關鍵設計決定

1. **品項與來源代碼分離。** 各國代碼系統不同（台灣「高麗菜」= `11`、日本另一個碼），
   `product_source_mappings` 做中間對照，不同國家的價格才能掛到同一個 `Product`
   放在同一時間序列上。
2. **沒對應到的價格照樣入庫**（`official_prices.product_id` 可為 NULL）。
   先收資料、之後補對照再回填，比丟資料好。

### 5.2 其他值得知道的設計

- **`official_prices.product_id` 是冗餘展開**：來源的 `mapping_id` 才有權威性；
  `product_id` 是為了「查某品項所有價格」不必 join 兩層。mapping 補上對應後由
  `ingest.backfill_mapping_product()` 一併回填。
- **`grade` 用空字串不用 NULL**：去重鍵含 grade，SQL 中 `NULL != NULL`
  會造成重複寫入。
- **主鍵策略**：大表（`official_prices`、`ingest_runs`、`otp_codes`）用 BIGSERIAL；
  需要跨系統識別的用 UUID（`gen_random_uuid()` server default）。
- **enum 一律存 `.value` 小寫字串**（`base.pg_enum` 的 `values_callable`）：
  DB、JSON API、日誌看到同一個字串，手寫 SQL 不用記兩套。
- **`quotes.role_snapshot`**：報價建立當下快照使用者身分，之後改角色不影響歷史呈現。
- **命名慣例**：`models/base.py` 定義固定 constraint 命名（`ix_`/`uq_`/`fk_`/`pk_`），
  否則 Alembic autogenerate 會產出無名物件。
- **`Base.type_annotation_map`**：`dict[str, Any]` 自動映射成 JSONB、UUID 自動映射。

---

## 6. 官方價格資料流（extension → DB）

這是全系統最核心的一條流水線：

```
                                ┌─────────────────────────── registry ──────────────────────────┐
app/extensions/<key>/           │  discover(): 掃描目錄 → import → 驗證 SOURCE / manifest /     │
  __init__.py (SOURCE)  ──────▶ │  config → LoadedExtension{key, manifest, config, module}      │
                                └────────────────────────────────────────────────────────────────┘
                                                                  │ require(key)
                                                                  ▼
        ┌─────────────────────────────── ingest.run_source ───────────────────────────────┐
        │  1. instantiate(client) → PriceSource 實例                                        │
        │  2. setup() → fetch_markets() → fetch_prices(window)（async generator）→ teardown()│
        │  3. 區間超過 manifest.max_window_days 自動切段（_split_window）                     │
        └──────────────────────────────────────────────────────────────────────────────────┘
                                          │  RawPrice 串流（每 500 筆一批）
                                          ▼
                              ingest._Loader（正規化 + 寫入）
        ┌─────────────────────────────────────────────────────────────────┐
        │ _ensure_market   ：market_external_id → markets（無則自動建）        │
        │ _ensure_mapping  ：product_code → product_source_mappings（無則建   │
        │                   product_id=NULL 的待對應紀錄）                     │
        │ _to_row          ：單位換算（unit_factor：價格÷、數量×）              │
        │ write_batch      ：pg_insert ... ON CONFLICT DO UPDATE（upsert）    │
        └─────────────────────────────────────────────────────────────────┘
                                          ▼
                    official_prices / markets / product_source_mappings
                                          ▼
                    ingest_runs 執行紀錄 + data_sources 游標 / 狀態更新
                                          ▼
                    api/v1/products.py ──▶ services/prices.py ──▶ 前端
```

執行結果（`IngestResult`）：`status`（success / partial / failed）、`fetched` /
`written` / `skipped`、`markets_created` / `mappings_created`、`skip_reasons`。

失敗處理：整段失敗時 `data_sources.last_error` 記錄原因、**游標保留原值**，
下次從同一點重試；部分失敗（有 skip）標 `partial`。`trigger` 標示觸發來源
（`schedule` / `manual` / `startup`），回補時 `is_backfill=true`。

### 6.1 registry（app/extensions/registry.py）

載入規則（詳見 EXTENSIONS.md）：

1. 掃描 `app/extensions/` 下子套件（跳過 `base` / `registry` / `http` 與 `_` 開頭）
2. `__init__.py` 匯出 `SOURCE`（`PriceSource` 子類別）
3. **資料夾名 == `SOURCE.manifest.key`**（刻意強制：目錄名就是全域識別碼）

任何一個載入失敗只會進 `registry.errors`，不影響啟動；透過 `/v1/sources` 的
`load_errors` 與啟動日誌暴露。`discover(force=True)` 會 `importlib.invalidate_caches()`
——這讓 `/admin/sources/reload` 能發現新丟進去的資料夾（但已 import 的模組不會重載，
改程式碼還是要重啟）。

### 6.2 共用 HTTP（app/extensions/http.py）

- `http_client()`：每次 ingest 一個共用 `httpx.AsyncClient`（連線池 20、keepalive 10、
  統一 User-Agent 與逾時）。
- `request_with_retry()`：對 `429/500/502/503/504` 重試 3 次，尊重 `Retry-After`，
  否則指數退避（1.5^n）。

### 6.3 demo_mock

離線可用的假來源，同時是照抄範本。特點：用 hash 當亂數種子（同輸入同輸出，
測試可斷言）、週日休市、`EXTENSIONS_CONFIG` 可調 `volatility_pct` 與 `markets`。
正式環境用 `EXTENSIONS_DISABLED=demo_mock` 關掉。

---

## 7. 排程（app/services/scheduler.py）

- 每個 extension 依**自己的** `manifest.schedule`（5 欄位 cron）註冊一個
  `ingest:<key>` 工作 —— **新增來源不需要動排程設定**。
- 另有一個固定工作 `quotes:expire`：每 10 分鐘把 `valid_until` 已過的
  `active` 報價標成 `expired`。
- 每個工作 `max_instances=1`（同一來源不併行）、`coalesce=True`
  （停機期間堆積的觸發只補跑一次）、`misfire_grace_time=600`。
- cron 的時區由 `SCHEDULER_TIMEZONE` 決定（不是 manifest 的 timezone——
  後者只描述來源的交易日語意）。
- **in-process、無跨節點鎖**：多 instance 部署時只在其中一台開
  `SCHEDULER_ENABLED=true`。
- `GET /v1/admin/scheduler` 回每個工作的 `id` / `name` / `next_run_at`。

---

## 8. 認證（services/auth.py + core/security.py）

### 8.1 流程

```
POST /auth/otp/request
  ├─ normalize_phone()：0912345678+TW → +886912345678（吸收空白/破折號/前導 0）
  ├─ 冷卻檢查（60s）＋ 每小時上限（5 次）＋ 同號碼舊碼全作廢
  ├─ 產生 6 位 OTP → 只存 HMAC-SHA256(OTP_PEPPER, code) 雜湊
  └─ 經 SMS provider 送出；OTP_DEBUG_ECHO=true 且非 production 時回在 response

POST /auth/otp/verify
  ├─ 找最新未使用 OTP，SELECT ... FOR UPDATE 防併發
  ├─ 檢查過期 / 嘗試次數上限（5 次），失敗計數、成功即作廢
  └─ get_or_create_user()：首登自動建帳號 → issue_tokens()

issue_tokens()
  ├─ access token：JWT（sub=user_id, typ=access, role claim），TTL 12h
  └─ refresh token：urlsafe 隨機字串，只存雜湊，TTL 60 天

POST /auth/refresh —— 旋轉式換發：
  ├─ 已 revoke 的 token 又出現 → 判定外洩 → revoke_all_tokens() 該使用者全部作廢
  └─ 成功 → 舊 token 立刻 revoke、發新的一組
```

安全設計重點：

- OTP 與 refresh token 都**只存 HMAC 雜湊**（pepper 與 SECRET_KEY 分開，可獨立輪替）；
  DB 外洩無法直接登入。
- refresh token **每次使用都旋轉**，重複使用舊 token 觸發全裝置登出（`refresh_token_reused`）。
- OTP 驗證失敗計數寫在該筆上，超過上限作廢，擋暴力猜碼。
- JWT 驗證時檢查 `typ`（access 或 refresh 不可混用）。
- 使用者帳號停用（`is_active=false`）即時生效於登入與 token 解析。

### 8.2 簡訊（services/sms.py）

`SmsProvider` 抽象介面，`send()` 回 `bool`，**送不出去不丟例外中斷登入流程**。
目前兩個實作：

- `console`：印到日誌（開發用）
- `twilio`：純 REST 呼叫 Messages API（不拉 SDK）

新增供應商 = 實作 `SmsProvider` 並在 `_PROVIDERS` 註冊。

---

## 9. API 層（app/api/v1/）

路由清單見 API.md。幾個組裝重點：

- **`core/deps.py` 的依賴注入型別**：
  - `DbSession`：每 request 一個 session，成功 commit / 失敗 rollback
  - `CurrentUser`（必登入）與 `OptionalUser`（公開端點用：有帶 token 就認人，
    過期 token 不影響公開內容）
  - `QuoterUser`：登入 + 身分為 farmer/trader 才放行
  - `Paging`：`?limit` / `?offset` 的解析（上限 200）
  - `Locale`：`?locale=` > `Accept-Language` > `DEFAULT_LOCALE`
  - `ClientIp`：取 `X-Forwarded-For` 第一個（Cloud Phone 一定在反代後面）
  - `AdminGuard`：`X-Admin-Token` 比對；**未設定 token 時一律 403（fail-closed）**
- **錯誤處理**（`core/errors.py`）：所有商業錯誤都 raise `AppError` 子類別，
  全域 handler 把它們轉成統一形狀 `{"error": {code, message, details}}`；
  `RequestValidationError` → 422；Starlette `HTTPException` → `http_<status>`。
- **電話遮罩**：`QuoteOut.from_model` 依 `viewer_id` 決定給完整號碼還是遮罩；
  報價者本人永遠看得到自己的完整號碼。
- **品項名稱語系解析**：`Product.display_name(locale)` 依「指定語系 → en → 任一」
  順序找 `is_primary` 名稱。

---

## 10. 測試策略

刻意分成兩層，互補：

| 層 | 檔 | 需要 DB？ | 測什麼 |
| --- | --- | --- | --- |
| 單元測試 | `tests/test_security.py`、`tests/test_extensions.py` | ❌ | 電話正規化、OTP 雜湊、JWT、extension 載入契約（用 `temp_extension` fixture 在真實目錄建臨時套件） |
| 端對端煙霧測試 | `scripts/smoke_test.py` | ✅（需起服務） | 健康檢查 → 抓取 → 建品項 → 對照 → 查價 → OTP 登入 → 發報價 → 總覽 → admin 守衛 |

```bash
.venv/Scripts/python.exe -m pytest          # 單元測試（不需要資料庫）
.venv/Scripts/python.exe scripts/smoke_test.py --base http://127.0.0.1:8000
```

煙霧測試需要 `.env` 設 `OTP_DEBUG_ECHO=true`（自動取得驗證碼）。

`tests/conftest.py` 的 `temp_extension` fixture 之所以要在**真實** `app/extensions/`
目錄建套件，是因為 registry 用 `app.extensions.<name>` 匯入；測完會刪檔並清
`sys.modules` 快取。

---

## 11. Migration（alembic/）

```bash
# 改完 models 後
.venv/Scripts/python.exe -m alembic revision --autogenerate -m "描述"
# 先讀過產出的檔案（尤其 enum 增減）再套用
.venv/Scripts/python.exe -m alembic upgrade head
```

- 命名慣例由 `models/base.py` 的 `NAMING_CONVENTION` 提供，autogenerate 才會產出
  有名字的 constraint。
- `alembic/env.py` 使用 `settings.sync_sqlalchemy_url`（asyncpg → psycopg2 轉換）。
- 注意：`alembic.ini` 刻意維持純 ASCII（Windows 會以系統語系編碼讀取該檔）；
  console 中文亂碼請設 `PYTHONUTF8=1`。

---

## 12. 常用開發任務

### 12.1 新增一個 API 端點

1. `app/schemas/` 加請求 / 回應 Pydantic 模型（從 ORM 轉出用 `from_model` classmethod）
2. `app/services/` 加查詢 / 商業邏輯
3. `app/api/v1/` 對應 router 加 route（掛進 `api_router`，前綴自動是 `/v1`）
4. 錯誤一律 `raise AppError` 子類別；列表型態用 `Page.build()`

### 12.2 新增一個國家 / 機構的官方價格來源

完整規範見 EXTENSIONS.md。三句話版：

1. `app/extensions/<key>/` 建資料夾，`__init__.py` 匯出 `SOURCE`
2. `SOURCE` 是 `PriceSource` 子類別，設 `manifest`、實作 `fetch_prices()`
3. 資料夾名 == `manifest.key`

框架自動處理：排程註冊、市場建立、代碼對照、單位換算、upsert、游標、執行紀錄。

### 12.3 回補歷史資料 / 補對照

```http
POST /v1/admin/sources/{key}/sync?start=2026-01-01&end=2026-06-30   # 回補（自動切段）
GET  /v1/admin/mappings?source_key={key}&unmapped_only=true          # 找待對應
PUT  /v1/admin/mappings/{id}  {"product_id": "...", "unit_factor": 1} # 對照 + 回填
```

`unit_factor` 定義：**1 個來源單位 = factor 個品項標準單位**（1 箱 = 10kg → 10）。
換算時價格÷factor、數量×factor。歷史列**不會**重算，單位非 1:1 時請在第一次
抓取前就設好 factor。

### 12.4 常用腳本

| 指令 | 用途 |
| --- | --- |
| `python scripts/seed_products.py` | 建立 20 個常見品項（zh-Hant/ja/en 名稱與別名）。可重複執行 |
| `python scripts/seed_products.py --reset-popularity` | 並重設熱門度 |
| `python scripts/smoke_test.py` | 端對端煙霧測試 |

---

## 13. 各部件關係速查表

| 部件 | 依賴 | 被誰依賴 |
| --- | --- | --- |
| `core/config` | 無（讀 .env） | 全部 |
| `core/database` | config | deps、services、alembic |
| `core/security` | config、errors | deps、services/auth |
| `core/deps` | database、security、models | 所有路由 |
| `core/errors` | 無 | 全部（raise / handler） |
| `models/*` | base、enums | services |
| `schemas/*` | models（僅型別）、security | 路由、main |
| `services/auth` | models、security、config、sms | api/auth |
| `services/catalog` | models、pagination、errors | api/products、markets、admin |
| `services/prices` | models、pagination | api/products |
| `services/quotes` | models、config、errors | api/quotes、users、products（overview）、scheduler |
| `services/ingest` | extensions.base/registry/http、models、database | main、scheduler、api/admin |
| `services/scheduler` | ingest、quotes、registry、config | main（lifespan） |
| `services/sms` | config | services/auth |
| `extensions/registry` | extensions.base、config、errors | main、ingest、api/sources、api/admin |
| `extensions/*`（來源） | extensions.base、extensions.http | registry |
| `api/v1/*` | services、schemas、deps、pagination | main（api_router） |
