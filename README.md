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

**身分在註冊時綁定。** `otp/request` 的回應會帶 `is_registered`，`false` 時
`verify` 必須指定 `role`（`consumer` / `farmer` / `trader`），否則回 400
`role_required`——而且這個檢查排在驗證碼比對之前，所以驗證碼不會被消耗，
補上 role 可以用同一組碼重試。之後使用者無法自行更改身分（`PATCH /me` 不接受
`role`），只能由維運走 `PATCH /v1/admin/users/{id}`。這樣報價上標示的
「小農 / 盤商」才有意義。

### 讀取（公開）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| GET | `/products` | 搜尋品項，`q` 比對所有語系名稱與別名 |
| GET | `/products/{ref}` | 品項詳情（`ref` 可以是 UUID 或 slug） |
| GET | `/products/{ref}/overview` | **詳情頁一次拿齊**：官方價 + 走勢 + 報價摘要 |
| GET | `/products/{ref}/prices/official` | 各市場最新官方價 |
| GET | `/products/{ref}/prices/series` | 每日走勢（跨市場以交易量加權） |
| GET | `/products/{ref}/markets` | 有此品項資料的市場 |
| GET | `/markets` | 市場清單，可依 `country_code` / `region` 篩選 |
| GET | `/markets/regions` | 有市場資料的縣市與市場數 |
| GET | `/quotes` | 瀏覽報價，可依品項 / 買賣別 / 身分 / 地區篩選 |
| GET | `/sources` | 資料來源與載入狀態 |

### 報價（需登入）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| POST | `/quotes` | 新增。需 `farmer` 或 `trader` 身分 |
| PATCH | `/quotes/{id}` | 修改自己的報價 |
| DELETE | `/quotes/{id}` | 下架（軟刪除，狀態轉 `withdrawn`） |
| GET | `/me` / PATCH `/me` | 個人資料（暱稱 / 地區 / 語系）。**不能改身分** |
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
| GET | `/admin/users` | 使用者清單，可依 `role` / `phone` 篩選 |
| PATCH | `/admin/users/{id}` | 更正使用者身分（唯一能改 role 的管道） |
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

### 立即執行某個資料源

不想等排程時，由快到慢有三種方式：

```bash
# 1. CLI（不必啟動服務，開發時最方便）
python scripts/run_ingest.py --list                      # 看有哪些來源
python scripts/run_ingest.py tw_moa --dry-run --limit 5  # 只印結果，不寫 DB
python scripts/run_ingest.py tw_moa                      # 抓 lookback_days 天
python scripts/run_ingest.py tw_moa --start 2026-09-01 --end 2026-09-18
python scripts/run_ingest.py --all

# 2. 管理端點（服務執行中）
curl -X POST "localhost:8000/v1/admin/sources/tw_moa/sync" -H "X-Admin-Token: $ADMIN_API_TOKEN"
curl -X POST "localhost:8000/v1/admin/sources/sync-all"   -H "X-Admin-Token: $ADMIN_API_TOKEN"

# 3. 啟動時就跑一次
#    .env: SCHEDULER_RUN_ON_STARTUP=true
```

新增了 extension **資料夾**：`POST /v1/admin/sources/reload` 重新掃描即可。
改的是 **程式碼**：必須重啟服務（Python 不會重新載入已 import 的模組）。

### 開發時怎麼做 OTP 驗證

不會真的發簡訊。`.env` 裡兩個開關決定驗證碼從哪裡拿：

```dotenv
SMS_PROVIDER=console    # 驗證碼印在伺服器 log 的 [SMS:console] 那行
OTP_DEBUG_ECHO=true     # 驗證碼直接回在 API response 的 debug_code 欄位
```

`OTP_DEBUG_ECHO` 在 `ENVIRONMENT=production` 時**強制失效**（程式裡寫死的），
所以不小心帶上正式環境也不會外洩驗證碼。

最省事的方式是用腳本一行拿到 token：

```bash
python scripts/dev_login.py --base http://localhost:8000
python scripts/dev_login.py --role trader --phone 0987654321

# 直接塞進 shell 變數
eval "$(python scripts/dev_login.py --export)"
curl -s localhost:8000/v1/me -H "Authorization: Bearer $ACCESS_TOKEN"
```

手動走也可以：

```bash
curl -X POST localhost:8000/v1/auth/otp/request -H "Content-Type: application/json" \
     -d '{"phone":"0912345678","country_code":"TW"}'
# -> {"phone":"+88*******678","expires_at":"…","retry_after":60,"debug_code":"101120"}

curl -X POST localhost:8000/v1/auth/otp/verify -H "Content-Type: application/json" \
     -d '{"phone":"0912345678","country_code":"TW","code":"101120","role":"farmer"}'
# -> access_token / refresh_token / user
```

**會擋到你的三個限制**（都是刻意的，正式環境要留著）：

| 限制 | 預設 | 開發時的解法 |
| --- | --- | --- |
| 同號碼重寄冷卻 | 60 秒 | 換號碼，或 `OTP_RESEND_COOLDOWN_SECONDS=0` |
| 同號碼每小時次數 | 5 次 | 換號碼，或 `OTP_MAX_PER_PHONE_PER_HOUR=100` |
| 驗證碼有效期 | 5 分鐘 | `OTP_TTL_SECONDS=3600` |

`dev_login.py` 省略 `--phone` 時會隨機產生號碼，所以連續跑不會撞到前兩項。
另外每次索取新碼都會把同號碼的舊碼作廢，別拿上一封的號碼去驗。

### 品項圖片

每個品項配一張 Wikimedia Commons 的圖，連同出處一起存進 `products`：

```bash
python scripts/fetch_product_images.py                    # 試算
python scripts/fetch_product_images.py --apply            # 只補沒圖的
python scripts/fetch_product_images.py --apply --all      # 全部重抓
python scripts/fetch_product_images.py --apply --slug pear
```

**這些圖的授權（多為 CC BY-SA）要求標示來源、作者與條款**，所以 DB 存的不只是
網址，還有 `image_source` / `image_source_url` / `image_license` / `image_author`。
API 在品項詳情與 overview 回一個 `image` 物件，**前端顯示圖片時有義務一併顯示出處**。

腳本用英文名去查維基條目，對得到 98/134；其餘靠 `TITLE_OVERRIDES` 人工指定
（白蘿蔔 → `Daikon`、蓮霧 → `Syzygium samarangense`、蘆筍的條目沒有代表圖
所以直接指定 `File:Asparagus-Bundle.jpg`）。新增品項後跑一次 `--apply` 即可補圖。

> 呼叫 Wikimedia 要帶符合他們 robot policy 的 User-Agent（含專案網址與聯絡方式），
> 否則直接 403。腳本裡的 `USER_AGENT` 換專案時記得改。

### 品項對照

抓進來的官方價要**對照到標準品項**才會出現在 `/products/{id}/prices/*`。
沒對照的資料照樣入庫（`official_prices.product_id` 為 NULL），只是查不到。

```bash
# 試算：看會對到什麼，不寫入
python scripts/automap_products.py --source tw_moa

# 確認後寫入（同時回填既有價格列的 product_id）
python scripts/automap_products.py --source tw_moa --apply

# 看對不到哪些，決定要補什麼品項或別名
python scripts/automap_products.py --source tw_moa --show-unmatched
```

比對對象是 `product_names` 的**所有語系與別名**，所以 MOA 的「甘藍」
會對到 `cabbage`（別名裡有「甘藍」），不需要另外維護對照表。
規則是「全名相同」優先，其次「基底名相同」（`甘藍-初秋` → `甘藍`）。
基底名會誤判的（例如 `蘿蔔-甜菜根`）寫在腳本的 `EXCLUDE_EXACT` 裡。

要擴大覆蓋率就往 `scripts/seed_products.py` 加品項與別名，再跑一次 automap。

> 有 ingest 正在跑時**不要**同時套用對照：ingest 在開始時就快取了對照表，
> 它寫入的列會拿到當時的（NULL）`product_id`。等它跑完再 `--apply`，
> 回填會一次補齊。

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
