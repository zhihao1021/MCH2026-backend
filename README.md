# MCH2026 — 農產品即時價格 API

給 Cloud Phone 功能機 App 用的後端。同時提供**官方批發行情**（由可插拔的
extension 供應，支援多國）與**小農 / 盤商自行報價**。

FastAPI + PostgreSQL + SQLAlchemy 2.0（async）。

前端在 `../frontend`，是 QVGA 240×320 的遠端渲染 widget，
所以 API 設計上刻意壓小回應、把聚合算在後端（見「給前端的注意事項」）。

---

## 快速開始

```bash
# 1. 依賴（repo 已附 .venv）
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 設定
cp .env.example .env
#    至少要填 DATABASE_URL、SECRET_KEY、OTP_PEPPER、ADMIN_API_TOKEN

# 3. 建表
.venv/Scripts/python.exe -m alembic upgrade head

# 4. 起服務
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

- API 文件：<http://localhost:8000/docs>
- 健康檢查：<http://localhost:8000/healthz>

驗證整條流程（需要服務已啟動、`.env` 設 `OTP_DEBUG_ECHO=true`）：

```bash
.venv/Scripts/python.exe scripts/smoke_test.py --base http://127.0.0.1:8000
.venv/Scripts/python.exe -m pytest        # 單元測試，不需要資料庫
```

> Windows note：`alembic.ini` 會以系統語系編碼讀取，所以該檔刻意維持純 ASCII。
> 若 console 出現中文亂碼，設 `PYTHONUTF8=1`。

---

## 架構

```
app/
├── main.py              應用組裝 + lifespan（掃描 extension、起排程）
├── core/
│   ├── config.py        .env → Settings（唯一讀環境變數的地方）
│   ├── database.py      async engine / session
│   ├── security.py      JWT、OTP 雜湊、E.164 電話正規化
│   ├── deps.py          依賴注入（登入者、分頁、語系、admin 守衛）
│   ├── errors.py        統一錯誤型別與 JSON 形狀
│   └── pagination.py
├── models/              SQLAlchemy ORM（11 張表）
├── schemas/             Pydantic 請求 / 回應
├── services/            商業邏輯
│   ├── auth.py          OTP 登入、token 旋轉
│   ├── catalog.py       品項 / 市場 / 代碼對照查詢
│   ├── prices.py        官方價聚合與走勢
│   ├── quotes.py        報價 CRUD 與統計
│   ├── ingest.py        extension → DB 的正規化與寫入
│   ├── scheduler.py     APScheduler，依 manifest 排程
│   └── sms.py           簡訊供應商介面
├── api/v1/              路由
└── extensions/          官方價格資料源（見 EXTENSIONS.md）
    ├── base.py          契約
    ├── registry.py      目錄掃描與載入
    ├── http.py          共用 HTTP client + 重試
    └── demo_mock/       範例來源（離線可用的假資料）
```

### 資料模型

```
data_sources ──┬── markets ──────────┐
               │                     ├── official_prices ── products
               └── product_source_mappings ─┘                  │
                                                               │
users ── quotes ───────────────────────────────────────────────┘
```

| 表 | 用途 |
| --- | --- |
| `data_sources` | 每個已載入 extension 的投影：游標、最後執行狀態 |
| `markets` | 批發市場，以 (來源, 來源市場代碼) 唯一 |
| `products` | 平台標準品項，**跨國比較的共同軸** |
| `product_names` | 品項的多語名稱與別名，搜尋就是比對這張表 |
| `product_source_mappings` | 來源代碼 → 標準品項；未對應的也照收，之後補 |
| `official_prices` | 官方行情。(來源, 市場, 代碼, 交易日, 等級) 唯一，重跑走 upsert |
| `ingest_runs` | 每次抓取的執行紀錄 |
| `users` / `otp_codes` / `refresh_tokens` | 手機 OTP 登入 |
| `quotes` | 小農 / 盤商報價 |

設計上的兩個關鍵決定：

- **`products` 與來源代碼分離。** 各國代碼系統完全不同，靠
  `product_source_mappings` 轉一層，同一個「高麗菜」才能同時掛上
  台灣的 `11` 與日本的某個代碼。
- **沒對應到品項的價格照樣入庫**（`official_prices.product_id` 可為 NULL）。
  資料先收下來，對照之後再補並回填，比起丟掉資料要好。

---

## API

所有路徑前綴 `/v1`。錯誤一律是 `{"error": {"code": ..., "message": ..., "details": ...}}`。

### 認證（手機 + OTP）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| POST | `/auth/otp/request` | 索取驗證碼。有冷卻（60s）與每小時上限 |
| POST | `/auth/otp/verify` | 驗證並登入，首次自動建帳號，回 access + refresh |
| POST | `/auth/refresh` | 換發。refresh token 每次使用都旋轉 |
| POST | `/auth/logout` | 登出，可選 `all_devices` |

電話號碼接受 `0912345678`（配 `country_code`）或 `+886912345678`，
內部統一存成 E.164。重複使用已作廢的 refresh token 會觸發該使用者全部 token 作廢。

### 讀取（公開）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| GET | `/products` | 搜尋品項，`q` 比對所有語系名稱與別名 |
| GET | `/products/{ref}` | 品項詳情（`ref` 可以是 UUID 或 slug） |
| GET | `/products/{ref}/overview` | **詳情頁一次拿齊**：官方價 + 走勢 + 報價摘要 |
| GET | `/products/{ref}/prices/official` | 各市場最新官方價 |
| GET | `/products/{ref}/prices/series` | 每日走勢（跨市場以交易量加權） |
| GET | `/products/{ref}/markets` | 有此品項資料的市場 |
| GET | `/markets` | 市場清單 |
| GET | `/quotes` | 瀏覽報價，可依品項 / 買賣別 / 身分 / 地區篩選 |
| GET | `/sources` | 資料來源與載入狀態 |

### 報價（需登入）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| POST | `/quotes` | 新增。需 `farmer` 或 `trader` 身分 |
| PATCH | `/quotes/{id}` | 修改自己的報價 |
| DELETE | `/quotes/{id}` | 下架（軟刪除，狀態轉 `withdrawn`） |
| GET | `/me` / PATCH `/me` | 個人資料，可切換身分 |
| GET | `/me/quotes` | 我的報價（含已下架） |

報價預設 48 小時後過期，排程每 10 分鐘把過期的轉成 `expired`。
報價者可選擇是否公開電話；不公開時其他人只看得到遮罩後的號碼。

### 維運（需 `X-Admin-Token`）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| POST | `/admin/sources/{key}/sync` | 手動抓取，可指定 `start` / `end` 回補 |
| POST | `/admin/sources/sync-all` | 全部來源跑一次 |
| POST | `/admin/sources/reload` | 重新掃描 extensions 目錄 |
| GET | `/admin/ingest-runs` | 抓取執行紀錄 |
| GET | `/admin/mappings` | 來源代碼對照，`unmapped_only=true` 找待處理的 |
| PUT | `/admin/mappings/{id}` | 指定對照的品項（會回填既有價格） |
| POST | `/admin/products` | 新增標準品項 |
| GET | `/admin/scheduler` | 排程狀態與下次執行時間 |

`ADMIN_API_TOKEN` 沒設定時，所有 `/admin/*` 一律拒絕。

---

## 新增一個國家的官方價格

完整規範在 **[EXTENSIONS.md](EXTENSIONS.md)**。三句話版本：

1. 在 `app/extensions/<key>/` 建資料夾，`__init__.py` 匯出 `SOURCE`；
2. `SOURCE` 是 `PriceSource` 子類別，設好 `manifest`、實作 `fetch_prices()`；
3. 資料夾名必須等於 `manifest.key`。

Extension 不碰資料庫——只要 yield `RawPrice`，正規化、市場建立、代碼對照、
單位換算、去重 upsert、排程都由框架處理。

`demo_mock` 是可以直接照抄的範本，也讓沒有網路時整條 ingest 流程仍跑得動。
正式環境請在 `.env` 設 `EXTENSIONS_DISABLED=demo_mock` 關掉。

---

## 給前端的注意事項

前端跑在 Cloud Phone 上（伺服器端渲染後串流到功能機，4G 下約 20 FPS），
所以：

- **用 `/products/{ref}/overview`**，不要為了詳情頁打三支 API。
- 分頁預設 20 筆、上限 200。清單頁給小一點的 `limit`。
- 走勢的點用縮寫欄位（`d` / `avg` / `high` / `low` / `vol`）省頻寬。
- `change_pct` 已經算好，不用自己抓兩天相減。
- 品項名稱由後端依 `?locale=` 或 `Accept-Language` 解析好，直接顯示即可。
- 價格是字串型態的 `Decimal`（避免浮點誤差），顯示前自行 parse。

---

## 維運

### 排程

每個 extension 的執行頻率來自它自己的 `manifest.schedule`（5 欄位 cron），
所以新增來源不需要改任何排程設定。另有一個每 10 分鐘的工作清理過期報價。

多個 instance 同時跑時請只在其中一台開 `SCHEDULER_ENABLED=true`，
目前的排程是 in-process 的，沒有跨節點鎖。

### Migration

```bash
# 改完 models 後
.venv/Scripts/python.exe -m alembic revision --autogenerate -m "描述"
.venv/Scripts/python.exe -m alembic upgrade head
```

autogenerate 出來的檔案**請先讀過再套用**，特別是 enum 的增減。

### 上線前

- [ ] `SECRET_KEY` / `OTP_PEPPER` / `ADMIN_API_TOKEN` 換成隨機值
- [ ] `ENVIRONMENT=production`、`DEBUG=false`、`OTP_DEBUG_ECHO=false`
- [ ] `SMS_PROVIDER=twilio`（或其他）並填好憑證
- [ ] `CORS_ORIGINS` 改成實際網域，不要留 `*`
- [ ] `EXTENSIONS_DISABLED=demo_mock`
- [ ] `LOG_JSON=true`（給 log 收集器用）
