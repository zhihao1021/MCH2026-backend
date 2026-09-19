# AgriPrice API 使用文件（前端）

給 Cloud Phone 功能機 App 前端使用的 API 參考文件。所有範例以本機開發伺服器
`http://127.0.0.1:8000` 為基準。互動式文件（Swagger UI）在 `/docs`，
OpenAPI schema 在 `/openapi.json`。

---

## 1. 基本資訊

| 項目 | 值 |
| --- | --- |
| Base URL | `http://<host>:8000` |
| API 前綴 | `/v1`（所有 API 都在 `/v1` 之下；健康檢查 `/healthz` 除外） |
| 資料格式 | JSON（請求與回應皆為 `application/json`） |
| 認證方式 | `Authorization: Bearer <access_token>` |
| 版本 | `0.1.0` |

### 1.1 健康檢查

```
GET /healthz
```

無需認證。回應範例：

```json
{
  "status": "ok",
  "environment": "development",
  "version": "0.1.0",
  "database": "ok",
  "extensions_loaded": 1,
  "extensions_failed": 0
}
```

- `status`：`ok` 或 `degraded`（資料庫連線失敗時）
- `database`：`ok` 或 `error: <例外型別>`

---

## 2. 通用規範

### 2.1 錯誤回應格式

所有非 2xx 的回應都是同一個形狀：

```json
{
  "error": {
    "code": "otp_invalid",
    "message": "驗證碼錯誤",
    "details": { "attempts_remaining": 4 }
  }
}
```

| 欄位 | 說明 |
| --- | --- |
| `code` | 機器可讀的錯誤代碼（前端判斷分支請用這個，不要比對 message） |
| `message` | 人類可讀訊息（繁體中文為主） |
| `details` | 選填，附帶結構化資訊（例如 `retry_after`） |

常見錯誤代碼一覽：

| HTTP | code | 情境 |
| --- | --- | --- |
| 401 | `missing_token` | 未帶 `Authorization` header |
| 401 | `invalid_token` / `token_expired` / `invalid_token_type` | access token 無效 / 過期 / 型別錯誤 |
| 401 | `otp_not_found` | 尚未索取驗證碼 |
| 401 | `otp_expired` | 驗證碼已過期 |
| 401 | `otp_invalid` | 驗證碼錯誤（`details.attempts_remaining` 為剩餘次數） |
| 401 | `otp_too_many_attempts` | 驗證碼嘗試次數過多，請重新索取 |
| 401 | `invalid_refresh_token` | refresh token 無效 |
| 401 | `refresh_token_expired` | refresh token 已過期 |
| 401 | `refresh_token_reused` | refresh token 重複使用（已作廢全部登入狀態） |
| 401 | `account_disabled` | 帳號停用 |
| 401 | `invalid_admin_token` | 管理端點 token 錯誤 |
| 400 | `role_required` | 註冊時未指定身分。**驗證碼不會被消耗**，補上 `role` 重試即可 |
| 403 | `role_cannot_quote` | 身分不是小農 / 盤商，不能報價 |
| 403 | `not_quote_owner` | 試圖修改 / 下架別人的報價 |
| 403 | `admin_disabled` | 伺服器未設定管理 token，管理端點全停用 |
| 404 | `product_not_found` / `market_not_found` / `quote_not_found` / `user_not_found` | 資源不存在 |
| 409 | `quote_limit_reached` | 有效報價數已達上限 |
| 409 | `quote_not_editable` | 已下架的報價不可修改 |
| 422 | `validation_error` | 參數格式錯誤（`details.fields` 列出每個欄位） |
| 429 | `otp_cooldown` | OTP 重寄冷卻中（`details.retry_after` 秒數） |
| 429 | `otp_hourly_limit` | 該號碼每小時索取次數已達上限 |

401 的回應會帶 `WWW-Authenticate: Bearer` header。

### 2.2 分頁

所有「清單型」API 共用同一組分頁參數與回應形狀。

**請求參數**：

| 參數 | 型別 | 預設 | 範圍 | 說明 |
| --- | --- | --- | --- | --- |
| `limit` | int | `20` | 1–200 | 每頁筆數 |
| `offset` | int | `0` | ≥0 | 略過的筆數 |

**回應形狀**：

```json
{
  "items": [ ... ],
  "total": 123,
  "limit": 20,
  "offset": 0,
  "has_more": true
}
```

- `total`：符合條件的總筆數（不是本頁筆數）
- `has_more`：`offset + 本頁筆數 < total` 時為 `true`，可直接用來判斷是否顯示「載入更多」

> 功能機頻寬有限，預設頁數刻意壓小。清單頁建議用較小的 `limit`（例如 10）。

### 2.3 語系（品項名稱）

多數回應中的 `product.name` 已由後端依語系解析好，直接顯示即可。
語系解析優先序：

1. Query 參數 `?locale=zh-Hant`
2. Request header `Accept-Language: zh-Hant`（取第一個值）
3. 伺服器預設 `DEFAULT_LOCALE`（預設 `zh-Hant`）

支援的語系視各品項的 `product_names` 資料而定（常見有 `zh-Hant` / `ja` / `en`）。
找不到指定語系時自動退回 `en`，再退回任一語系。

### 2.4 價格欄位

所有價格與數量欄位都是 **JSON 字串**型態的 Decimal（例如 `"19.20"`），
目的是避免浮點誤差。顯示或計算前請自行 parse。小數位數不保證固定
（可能是 `"19.2"` 也可能是 `"19.20"`），**請用數值比較，不要比字串**。

### 2.5 圖片與授權 ⚠️

每個品項都有一張圖，來自 **Wikimedia Commons**。

```
image_url  https://thumb.wikimedia.org/.../330px-Cabbage_and_cross_section_on_white.jpg
```

縮圖寬度 330px（功能機螢幕寬 240px，這個尺寸放大不糊、又不浪費頻寬）。

**這些圖幾乎都有授權條件，不是「隨便用」。** 目前 134 個品項的授權分佈：

| 授權 | 數量 | 是否必須標示 |
| --- | --- | --- |
| CC BY-SA（2.0–4.0） | 73 | ✅ 需標示來源、作者、授權 |
| Public domain / CC0 | 34 | 不需要（但標了更好） |
| CC BY（1.0–4.0） | 15 | ✅ 需標示 |
| GFDL | 10 | ✅ 需標示 |
| Attribution | 1 | ✅ 需標示 |

所以 **顯示圖片的畫面必須同時讓使用者看得到出處**。API 在品項詳情
（5.2）與總覽（5.3）回一個 `image` 物件：

```json
{
  "url": "https://thumb.wikimedia.org/.../330px-Cabbage_and_cross_section_on_white.jpg",
  "source": "Wikimedia Commons",
  "source_url": "https://commons.wikimedia.org/wiki/File:Cabbage_and_cross_section_on_white.jpg",
  "license": "GFDL 1.2",
  "author": "fir0002 flagstaffotos [at] gmail.com"
}
```

**最低限度的做法**：圖片下方放一行小字

```
圖片：Wikimedia Commons / fir0002（GFDL 1.2）
```

`author` 可能是 `null`（少數圖在 Commons 上沒標作者），這時顯示
`圖片：Wikimedia Commons（CC BY-SA 3.0）` 並讓它可以連到 `source_url` 即可。

> **清單（5.1）只回 `image_url`，不回 `image` 物件**——一頁 20 筆各帶一份
> 授權字串太浪費頻寬。清單的縮圖請在「關於 / 圖片來源」頁面統一標示，
> 或讓使用者點進詳情頁看。

### 2.6 時間格式

- 日期：`YYYY-MM-DD`（例如 `2026-09-17`）
- 日期時間：ISO 8601，帶時區（例如 `2026-09-19T08:30:00Z` 或 `...+00:00`）

### 2.7 認證

登入成功後拿到 `access_token` 與 `refresh_token`：

- 需要登入的端點一律帶 header：`Authorization: Bearer <access_token>`
- `access_token` 過期（預設 12 小時）時，用 `POST /v1/auth/refresh` 換新的
- refresh token 每次使用都會「旋轉」：舊的立刻作廢，**請務必儲存回應中的新 refresh token**
- 公開端點（查價格、看報價）可不登入；有登入時報價中「自己的電話」會回完整號碼

---

## 3. 認證（手機 + OTP）

登入與註冊走同一組端點，由號碼有沒有帳號決定：

```
POST /auth/otp/request  → 收到簡訊驗證碼，回應的 is_registered 告訴你接下來是登入還是註冊
POST /auth/otp/verify   → 註冊（需帶 role）或登入，回 access + refresh token
POST /auth/refresh      → access token 過期後換發（refresh token 旋轉）
POST /auth/logout       → 登出
```

### 身分（role）綁定規則 ⚠️

**身分在註冊時決定，之後不能自己改。** 這是刻意的：報價會記下報價者當下的身分
（`role_snapshot`），如果身分能隨時切換，報價上標的「小農」「盤商」就不可信了。

| 情境 | `role` 欄位 | 行為 |
| --- | --- | --- |
| 註冊（`is_registered: false`） | **必填** | 沒帶會回 400 `role_required` |
| 登入（`is_registered: true`） | 忽略 | 帶了也不會改變既有身分 |
| `PATCH /v1/me` | 不接受 | 送了會回 422 |
| 更正身分 | — | 只能由維運走 `PATCH /v1/admin/users/{id}` |

三種身分：`consumer`（消費者，只能看）、`farmer`（小農，可報價）、
`trader`（盤商，可報價）。

**前端該怎麼做**：在 `otp/request` 拿到 `is_registered: false` 時，
就在「輸入驗證碼」的同一個畫面一起顯示身分選擇，`verify` 時一次送出。
功能機上多一個畫面就多一次網路往返，不值得。

> 萬一漏帶 `role` 收到 `role_required`，**驗證碼不會被消耗掉**——
> 這個檢查刻意排在驗證碼比對之前，所以補上 `role` 後可以用同一組碼直接重試，
> 不用叫使用者重新收簡訊。

### 3.1 索取驗證碼

```
POST /v1/auth/otp/request
```

**請求 body**：

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `phone` | string | ✅ | 6–24 字元。接受 `0912345678`（需配 `country_code`）或 `+886912345678`（E.164） |
| `country_code` | string | | 2 字元 ISO 國碼（`TW` / `JP` / `KR` / ...）。只填本地格式號碼時必填 |

```json
{ "phone": "0912345678", "country_code": "TW" }
```

**回應**（HTTP 202）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `phone` | string | 遮罩後的號碼（例如 `+886***678`），讓使用者確認沒打錯 |
| `expires_at` | datetime | 驗證碼到期時間（預設 5 分鐘） |
| `retry_after` | int | 幾秒後才能重新索取（預設 60 秒） |
| `is_registered` | bool | **`false` = 接下來是註冊，`verify` 必須帶 `role`**；`true` = 登入，`role` 會被忽略 |
| `debug_code` | string\|null | **只有開發環境**且 `OTP_DEBUG_ECHO=true` 時才回傳驗證碼；正式環境永遠為 null |

```json
{
  "phone": "+886***678",
  "expires_at": "2026-09-19T08:35:00Z",
  "retry_after": 60,
  "is_registered": false,
  "debug_code": "123456"
}
```

**可能的錯誤**：`invalid_phone`（400）、`otp_cooldown`（429）、`otp_hourly_limit`（429）

> 冷卻時間內連點「重寄」會被拒，請在 UI 上直接倒數 `retry_after` 秒。

### 3.2 驗證並登入

```
POST /v1/auth/otp/verify
```

**請求 body**：

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `phone` | string | ✅ | 同 request，格式與上次一致 |
| `code` | string | ✅ | 4–10 字元的驗證碼 |
| `country_code` | string | | 同上 |
| `role` | string | 註冊時✅ | `consumer` / `farmer` / `trader`。**註冊時必填且之後不可更改**；登入時會被忽略 |
| `display_name` | string | | 暱稱，最多 80 字元。只在註冊時採用，之後改用 `PATCH /v1/me` |

```json
{
  "phone": "0912345678",
  "country_code": "TW",
  "code": "123456",
  "role": "farmer",
  "display_name": "阿明"
}
```

**回應**（HTTP 200）：

```json
{
  "access_token": "<JWT>",
  "refresh_token": "<opaque>",
  "token_type": "Bearer",
  "expires_in": 43199,
  "user": {
    "id": "f2c4d5f6-...",
    "phone": "+886912345678",
    "role": "farmer",
    "display_name": "阿明",
    "country_code": "TW",
    "locale": "zh-Hant",
    "region": null,
    "is_active": true,
    "can_quote": true,
    "created_at": "2026-09-19T08:30:00Z",
    "last_login_at": "2026-09-19T08:30:00Z"
  },
  "is_new_user": true
}
```

- `expires_in`：access token 剩餘有效秒數（預設 12 小時 = 43200）
- `is_new_user`：這次驗證是否順帶建立了新帳號
- `user.role`：已綁定的身分。登入時即使在 request 裡帶了別的值，這裡回的仍是原本的身分
- `user.can_quote`：等同 `role in (farmer, trader)`，前端可直接拿來決定要不要顯示「我要報價」

**可能的錯誤**：

| HTTP | code | 說明 |
| --- | --- | --- |
| 400 | `role_required` | 註冊但沒帶 `role`。**驗證碼未被消耗**，補上 `role` 用同一組碼重試即可 |
| 401 | `otp_invalid` | 驗證碼錯誤，`details.attempts_remaining` 是剩餘次數 |
| 401 | `otp_expired` | 驗證碼過期（預設 5 分鐘） |
| 401 | `otp_not_found` | 還沒索取過驗證碼，或前一組已被新的取代 |
| 401 | `otp_too_many_attempts` | 錯太多次，該組作廢，要重新索取 |
| 401 | `account_disabled` | 帳號已停用 |
| 400 | `invalid_phone` | 號碼格式不對 |

註冊的完整範例：

```jsonc
// 1) 先問
POST /v1/auth/otp/request
{ "phone": "0912345678", "country_code": "TW" }
→ { "is_registered": false, ... }        // 要顯示身分選擇

// 2) 使用者選了「小農」
POST /v1/auth/otp/verify
{ "phone": "0912345678", "country_code": "TW", "code": "123456",
  "role": "farmer", "display_name": "阿明" }
→ 200, is_new_user: true, user.role: "farmer", user.can_quote: true
```

### 3.3 換發 token

```
POST /v1/auth/refresh
```

**請求 body**：

```json
{ "refresh_token": "<refresh_token>" }
```

**回應**：與 3.2 相同形狀（含 `user`），沒有 `is_new_user`。

**重點**：

- 每次呼叫都**旋轉**：回應裡的 `refresh_token` 是新的一組，舊的立刻作廢
- 拿已作廢的 token 再來換（例如兩台裝置各存一份舊 token），會觸發**該使用者全部 token 作廢**，需重新登入（錯誤碼 `refresh_token_reused`）
- 前端務必以「寫入 → 使用」順序保存新 token，避免競態

**可能的錯誤**：`invalid_refresh_token`、`refresh_token_expired`、`refresh_token_reused`、`account_disabled`

### 3.4 登出

```
POST /v1/auth/logout        （需登入）
```

**請求 body**：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `refresh_token` | string | 只作廢這一個 token |
| `all_devices` | bool | `true` 時作廢此使用者的全部 refresh token |

```json
{ "refresh_token": "<refresh_token>" }
```

**回應**（HTTP 200）：

```json
{ "ok": true, "message": "已登出" }
```

---

## 4. 個人資料（需登入）

### 4.1 取得個人資料

```
GET /v1/me
```

回應即 3.2 中的 `user` 物件。

### 4.2 更新個人資料

```
PATCH /v1/me
```

**請求 body**（全部選填，只傳要改的欄位）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `display_name` | string | 暱稱，≤80 字元 |
| `region` | string | 產地 / 營業地，≤80 字元。報價時的預設值 |
| `locale` | string | 偏好的顯示語系，≤16 字元 |

```json
{ "display_name": "阿明", "region": "雲林縣" }
```

回應為更新後的 `user` 物件。

> **`role` 不能在這裡改。** 身分在註冊時綁定（見第 3 節），
> 送 `role` 會直接回 **422 `validation_error`**（未知欄位），而不是被默默忽略。
> 使用者選錯身分時，請他聯絡維運，由 `PATCH /v1/admin/users/{id}` 更正。

### 4.3 我的報價

```
GET /v1/me/quotes
```

分頁參數同 2.2。**含已下架（withdrawn）與已過期（expired）的報價**，
讓使用者可以管理自己的歷史報價。回應為 `Page<QuoteOut>`（見 6.5）。

---

## 5. 品項與官方價格（公開）

### 5.1 搜尋品項

```
GET /v1/products
```

**Query 參數**：

| 參數 | 型別 | 說明 |
| --- | --- | --- |
| `q` | string | 關鍵字，比對**所有語系**的名稱與別名（例如 `q=高麗菜`、`q=甘藍`、`q=cabbage` 都能找到同一個品項） |
| `category` | string | 分類：`vegetable` / `fruit` / `flower` / `grain` / `livestock` / `fishery` / `other` |
| `locale` | string | 回應名稱的語系（見 2.3） |
| `limit` / `offset` | int | 分頁（見 2.2） |

排序：`popularity` 高的在前。

**回應**：`Page<ProductOut>`

```json
{
  "items": [
    {
      "id": "a1b2c3d4-...",
      "slug": "cabbage",
      "name": "高麗菜",
      "category": "vegetable",
      "default_unit": "kg",
      "image_url": null
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0,
  "has_more": false
}
```

`ProductOut` 欄位：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | UUID | 平台品項 ID |
| `slug` | string | URL 用的穩定代號 |
| `name` | string | 已依語系解析好的名稱，直接顯示 |
| `category` | string | 分類 |
| `default_unit` | string | 標準單位（例如 `kg`） |
| `image_url` | string\|null | 品項縮圖（330px 寬）。**顯示時需標示出處，見 2.5**；完整授權資訊請取品項詳情 |

### 5.2 品項詳情

```
GET /v1/products/{ref}
```

`ref` 可以是 **UUID 或 slug**，兩者等效（例如 `/products/cabbage`）。

**回應**（`ProductDetailOut`）：`ProductOut` 加上：

```json
{
  "...": "同上",
  "names": [
    { "locale": "zh-Hant", "name": "高麗菜", "is_primary": true },
    { "locale": "zh-Hant", "name": "甘藍", "is_primary": false }
  ],
  "popularity": 100,
  "image": {
    "url": "https://thumb.wikimedia.org/.../330px-Cabbage_and_cross_section_on_white.jpg",
    "source": "Wikimedia Commons",
    "source_url": "https://commons.wikimedia.org/wiki/File:Cabbage_and_cross_section_on_white.jpg",
    "license": "GFDL 1.2",
    "author": "fir0002 flagstaffotos [at] gmail.com"
  }
}
```

`image` 的欄位：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `url` | string | 圖片網址，與外層的 `image_url` 相同 |
| `source` | string\|null | 來源平台，目前一律是 `Wikimedia Commons` |
| `source_url` | string\|null | 圖片說明頁，可讓使用者點進去看完整授權 |
| `license` | string\|null | 授權簡稱，例如 `CC BY-SA 4.0` / `Public domain` |
| `author` | string\|null | 作者。少數圖沒有標，這時只顯示來源與授權即可 |

沒有圖時 `image` 為 `null`（目前 134 個品項都有圖）。

### 5.3 品項總覽（詳情頁一次拿齊）⭐

```
GET /v1/products/{ref}/overview
```

> **前端詳情頁請打這支**，不要為了官方價 + 走勢 + 報價摘要打三支 API。

**Query 參數**：

| 參數 | 型別 | 預設 | 範圍 | 說明 |
| --- | --- | --- | --- | --- |
| `days` | int | `14` | 2–365 | 走勢天數 |
| `country_code` | string | | 只看某個國家的資料（ISO 3166-1 alpha-2） |
| `markets_limit` | int | `10` | 1–50 | 最新官方價最多回幾個市場 |

**回應**：

```json
{
  "product": { "...": "ProductOut，見 5.1" },
  "image": { "...": "圖片與出處，見 5.2；詳情頁會大張顯示，務必標示" },
  "official": [
    {
      "market_id": "f8e7d6c5-...",
      "market_name": "示範第一市場",
      "region": "台北市",
      "country_code": "TW",
      "source_key": "demo_mock",
      "trade_date": "2026-09-18",
      "currency": "TWD",
      "unit": "kg",
      "grade": null,
      "price_avg": "19.20",
      "price_high": "25.90",
      "price_low": "13.40",
      "volume": "1294.000",
      "volume_unit": "kg"
    }
  ],
  "official_series": {
    "product_id": "a1b2c3d4-...",
    "market_id": null,
    "currency": "TWD",
    "unit": "kg",
    "change_pct": 1.234,
    "points": [
      { "d": "2026-09-16", "avg": "18.90", "high": "25.50", "low": "13.20", "vol": "1180.000" },
      { "d": "2026-09-17", "avg": "19.10", "high": "25.80", "low": "13.30", "vol": "1260.000" },
      { "d": "2026-09-18", "avg": "19.20", "high": "25.90", "low": "13.40", "vol": "1294.000" }
    ]
  },
  "quotes": {
    "count": 3,
    "price_min": "30.00",
    "price_max": "45.00",
    "price_avg": "38.50",
    "currency": "TWD",
    "unit": "kg"
  },
  "updated_at": "2026-09-19T08:30:00Z"
}
```

欄位說明：

- `official`：各市場**最新一筆**官方行情（每市場取最後一個交易日）。市場名稱、來源 key 都已併入，直接顯示
- `official_series`：每日走勢，**跨市場以交易量加權平均**（有 `market_id` 時為單市場）
  - `points[].d`：交易日；`avg` / `high` / `low`：均價 / 最高 / 最低；`vol`：交易量
  - 欄位名刻意縮短（`d` / `avg` / `vol`…）以節省頻寬
  - `change_pct`：最新一點相對前一點均價的漲跌幅（%），資料不足兩點時為 `null` —— **直接顯示，不用自己抓兩天相減**
- `quotes`：民間報價摘要（count / min / max / avg），與官方價並排顯示
- `official_series` 可能為 `null`（完全沒有走勢資料時）

### 5.4 各市場最新官方價

```
GET /v1/products/{ref}/prices/official
```

**Query 參數**：

| 參數 | 型別 | 預設 | 範圍 | 說明 |
| --- | --- | --- | --- | --- |
| `country_code` | string | | 只看某國 |
| `market_id` | UUID | | 只看某市場 |
| `max_age_days` | int | `14` | 1–90 | 只取此天數內最後一個交易日的資料 |
| `limit` / `offset` | int | | 分頁 |

> 為什麼是「最後一個交易日」而非「今天」：各市場休市日不同，
> 硬指定今天多半查不到。後端會自動取 `max_age_days` 內的最新一天。

**回應**：`Page<OfficialPriceOut>`，元素形狀同 5.3 的 `official[]`。

### 5.5 官方價走勢

```
GET /v1/products/{ref}/prices/series
```

**Query 參數**：

| 參數 | 型別 | 預設 | 範圍 | 說明 |
| --- | --- | --- | --- | --- |
| `days` | int | `30` | 2–365 | 回看天數 |
| `market_id` | UUID | | 指定則回單市場走勢；省略則跨市場加權 |
| `country_code` | string | | 只看某國 |

**回應**：同 5.3 的 `official_series` 物件。

### 5.6 有此品項資料的市場

```
GET /v1/products/{ref}/markets
```

回應為 `MarketOut[]`（不分頁，依最近交易日排序）。給前端做市場下拉選單。

`MarketOut` 欄位：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `id` | UUID | 平台市場 ID |
| `external_id` | string\|null | 來源系統的市場代碼 |
| `name` | string | 市場名稱 |
| `name_en` | string\|null | 英文名稱 |
| `country_code` | string | 國碼 |
| `region` | string\|null | 行政區 |
| `timezone` | string | 時區 |
| `latitude` / `longitude` | float\|null | 座標 |
| `source_key` | string\|null | 資料來源 key |

### 5.7 市場清單

```
GET /v1/markets
```

**Query 參數**：

| 參數 | 型別 | 說明 |
| --- | --- | --- |
| `country_code` | string | 國碼篩選 |
| `source_key` | string | 只看某個資料來源 |
| `q` | string | 名稱關鍵字（比對中 / 英文名） |
| `limit` / `offset` | int | 分頁 |

**回應**：`Page<MarketOut>`。

### 5.8 單一市場

```
GET /v1/markets/{market_id}
```

**回應**：`MarketOut`。`market_id` 為 UUID，需為合法 UUID 格式（非 UUID 會回 422）。

---

## 6. 民間報價（小農 / 盤商）

### 6.1 瀏覽報價（公開）

```
GET /v1/quotes
```

只回**有效（active）**的報價。可匿名瀏覽。

**Query 參數**（全部選填）：

| 參數 | 型別 | 說明 |
| --- | --- | --- |
| `product_id` | UUID | 只看某品項 |
| `side` | string | `sell`（我要賣）/ `buy`（我要收） |
| `role` | string | 只看小農 `farmer` 或盤商 `trader` |
| `country_code` | string | 國碼篩選 |
| `region` | string | 地區篩選 |
| `market_id` | UUID | 只看指定市場的報價 |
| `limit` / `offset` | int | 分頁 |

排序：最新建立的在前。

### 6.2 新增報價（需登入，身分為 farmer / trader）

```
POST /v1/quotes
```

**請求 body**：

| 欄位 | 型別 | 必填 | 限制 / 說明 |
| --- | --- | --- | --- |
| `product_id` | UUID | ✅ | 品項 ID |
| `price` | string(Decimal) | ✅ | 報價金額，>0 且 ≤ 99999999 |
| `side` | string | | `sell`（預設）/ `buy` |
| `unit` | string | | 單位，≤16 字元。省略則用品項 `default_unit` |
| `currency` | string | | 3 字元 ISO 4217。省略則依使用者國碼自動帶入（TW→TWD…） |
| `grade` | string | | 等級 / 規格，≤40 字元 |
| `quantity` | string(Decimal) | | 數量，>0 |
| `min_order` | string(Decimal) | | 最小訂購量，>0 |
| `market_id` | UUID | | 針對某批發市場時填 |
| `region` | string | | 地區，≤80 字元。省略則用個人資料的 `region` |
| `location_text` | string | | 位置描述，≤160 字元 |
| `latitude` / `longitude` | float | | 座標（-90~90 / -180~180） |
| `note` | string | | 備註，≤500 字元 |
| `contact_phone_public` | bool | | 是否公開電話，預設 `true`。`false` 時其他人只能看到遮罩號碼 |
| `valid_hours` | int | | 幾小時後過期，0–720（30 天）。`0` = 永不過期。省略用預設 48 小時 |

```json
{
  "product_id": "a1b2c3d4-...",
  "price": "33.5",
  "side": "sell",
  "quantity": "120",
  "note": "今日現採",
  "contact_phone_public": true,
  "valid_hours": 24
}
```

**回應**（HTTP 201）：`QuoteOut`（見 6.5）。

**規則**：

- 身分必須是 `farmer` 或 `trader`，否則 403 `role_cannot_quote`。
  身分是註冊時綁定的，`consumer` 無法自己改成小農——這是刻意的限制，
  前端可用 `user.can_quote` 事先判斷要不要顯示「我要報價」入口
- 有效報價數有上限（預設 50 筆），超過 409 `quote_limit_reached`
- 品項不存在或已停用 → 404 `product_not_found`

### 6.3 單筆報價（公開）

```
GET /v1/quotes/{quote_id}
```

**回應**：`QuoteOut`。

### 6.4 修改報價（需登入，僅限本人）

```
PATCH /v1/quotes/{quote_id}
```

**請求 body**：全部選填，只傳要改的欄位。可改：`price` / `unit` / `grade` /
`quantity` / `min_order` / `market_id` / `region` / `location_text` /
`latitude` / `longitude` / `note` / `contact_phone_public` / `valid_hours`

> **不可改**：`product_id`、`side`、`currency`（要改請下架後重發）。

`valid_hours` 傳入時會重設有效期，並把已下架（withdrawn）的報價**重新上架**
（續期 = 重新上架）。

已下架（`withdrawn` / `hidden`）的報價不可修改 → 409 `quote_not_editable`。
非本人 → 403 `not_quote_owner`。

### 6.5 `QuoteOut` 形狀

```json
{
  "id": "9f8e7d6c-...",
  "product": { "...": "ProductOut，見 5.1" },
  "side": "sell",
  "status": "active",
  "price": "33.50",
  "currency": "TWD",
  "unit": "kg",
  "grade": null,
  "quantity": "120.000",
  "min_order": null,
  "country_code": "TW",
  "region": "雲林縣",
  "location_text": null,
  "market_id": null,
  "note": "今日現採",
  "seller": {
    "id": "f2c4d5f6-...",
    "display_name": "阿明",
    "role": "farmer",
    "region": "雲林縣",
    "phone": "+886***678",
    "phone_is_masked": true
  },
  "created_at": "2026-09-19T08:30:00Z",
  "valid_until": "2026-09-20T08:30:00Z"
}
```

| 欄位 | 說明 |
| --- | --- |
| `status` | `active` / `expired` / `withdrawn` / `hidden`。公開列表只會看到 `active`；`/me/quotes` 會含所有狀態 |
| `seller.phone` | 報價者本人永遠看到完整號碼；其他人只有報價者同意公開（`contact_phone_public=true`）時才看得到完整號碼，否則為遮罩字串 |
| `seller.phone_is_masked` | 目前電話是否被遮罩 |
| `valid_until` | 過期時間；`null` = 永不過期 |

### 6.6 下架報價（需登入，僅限本人）

```
DELETE /v1/quotes/{quote_id}
```

**回應**：HTTP 200 + `QuoteOut`（`status` 變為 `withdrawn`）。為軟刪除，報價仍保留在 `/me/quotes`。

---

## 7. 資料來源（公開）

### 7.1 資料來源清單

```
GET /v1/sources
```

讓 App 顯示「資料來自哪裡、更新到什麼時候」。

**回應**：

```json
{
  "sources": [
    {
      "key": "demo_mock",
      "name": "Demo Mock Market",
      "country_code": "TW",
      "currency": "TWD",
      "timezone": "Asia/Taipei",
      "version": "1.0.0",
      "description": "離線開發與測試用的假資料來源",
      "homepage_url": null,
      "license": "MIT",
      "schedule": "*/30 * * * *",
      "installed": true,
      "enabled": true,
      "last_run_at": "2026-09-19T08:00:00Z",
      "last_success_at": "2026-09-19T08:00:00Z",
      "last_error": null
    }
  ],
  "load_errors": [
    { "key": "tw_moa", "reason": "import_failed", "detail": "..." }
  ]
}
```

| 欄位 | 說明 |
| --- | --- |
| `installed` | extension 目前有沒有被載入（資料夾被移除時為 `false`） |
| `enabled` | DB 中的啟用狀態 |
| `last_run_at` / `last_success_at` | 上次執行 / 上次成功時間，可顯示「更新於 X 分鐘前」 |
| `last_error` | 最近一次失敗原因 |
| `load_errors` | 載入失敗的 extension（服務不會因此停掉，但這裡看得到） |

### 7.2 單一資料來源

```
GET /v1/sources/{key}
```

**回應**：單一 `SourceOut`。未知的 key 回 404 `unknown_source`。

---

## 8. 管理端點（維運用，不給 App）

全部需要 header `X-Admin-Token: <ADMIN_API_TOKEN>`。給部署後維運使用，
前端 App 不需要（也無法）呼叫。以下僅列清單：

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| POST | `/v1/admin/sources/{key}/sync` | 手動抓取（`start` / `end` 回補） |
| POST | `/v1/admin/sources/sync-all` | 全部來源跑一次 |
| POST | `/v1/admin/sources/reload` | 重新掃描 extensions 目錄 |
| GET | `/v1/admin/ingest-runs` | 抓取執行紀錄 |
| GET | `/v1/admin/mappings` | 來源代碼對照（`unmapped_only=true` 找待處理） |
| PUT | `/v1/admin/mappings/{id}` | 指定對照品項並回填 |
| POST | `/v1/admin/products` | 新增標準品項 |
| GET | `/v1/admin/users` | 使用者清單（可依 `role` / `phone` 篩選） |
| PATCH | `/v1/admin/users/{id}` | **更正使用者身分**（唯一能改 role 的管道） |
| GET | `/v1/admin/scheduler` | 排程狀態與下次執行時間 |

未設定 `ADMIN_API_TOKEN` 時全部回 403；token 錯誤回 401。

---

## 9. 枚舉值一覽

### `UserRole`（身分）

| 值 | 說明 | `can_quote` |
| --- | --- | --- |
| `consumer` | 一般消費者，只能瀏覽價格與報價 | `false` |
| `farmer` | 小農，可自行報價 | `true` |
| `trader` | 盤商，可自行報價 | `true` |

**註冊時必選，之後綁定不可自行更改**（見第 3 節）。
報價會存下當下的身分（`role_snapshot`），即使日後由維運更正身分，
既有報價顯示的身分也不會被回溯修改——那記錄的是報價當下的事實。

### `ProductCategory`（品項分類）

`vegetable`（蔬菜）、`fruit`（水果）、`flower`（花卉）、`grain`（穀物）、
`livestock`（畜牧）、`fishery`（漁產）、`other`（其他）

### `QuoteSide`

| 值 | 說明 |
| --- | --- |
| `sell` | 我要賣（小農常用） |
| `buy` | 我要收（盤商常用） |

### `QuoteStatus`

| 值 | 說明 |
| --- | --- |
| `active` | 有效 |
| `expired` | 已過期（自動） |
| `withdrawn` | 已下架（使用者主動） |
| `hidden` | 遭檢舉 / 違規下架（管理端） |

---

## 10. 給前端的實作建議

1. **詳情頁用 `/products/{ref}/overview`**：一次拿齊官方價、走勢、報價摘要，
   省兩趟往返（功能機在 4G 下這差別很明顯）。
2. **分頁給小 limit**：清單頁建議 `limit=10`，讓 `has_more` 驅動「載入更多」。
3. **走勢直接畫**：`points` 的 `d/avg/high/low/vol` 已是畫圖用的形狀；
   `change_pct` 已算好，直接顯示漲跌。
4. **價格先 parse 再比較**：Decimal 字串的小數位數不固定，不要比字串。
5. **品項名稱直接顯示**：後端已依 `?locale=` / `Accept-Language` 解析。
6. **refresh token 一定回存**：每次 `/auth/refresh` 回應都是新 token，
   舊的已作廢；弄丟就必須重新走 OTP。
7. **冷卻時間用 `retry_after` 倒數**：不要自己猜 60 秒。
8. **電話遮罩邏輯**：`phone_is_masked=true` 時只顯示 `phone` 字串即可，
   不要嘗試還原。
9. **過期時間**：`valid_until` 為 `null` 表示永不過期，UI 不要顯示倒數。
10. **錯誤處理**：以 `error.code` 分支（例如 `otp_invalid` 顯示剩餘次數、
    `token_expired` 觸發 refresh），`error.message` 可直接顯示給使用者。
11. **身分選擇要跟驗證碼同一畫面**：`otp/request` 回的 `is_registered: false`
    就代表這是註冊，直接在輸入驗證碼的畫面加三個選項（消費者 / 小農 / 盤商），
    `verify` 一次送出。多開一個畫面就多一次往返，功能機上很有感。
12. **圖片一定要標出處**：`image.license` 多是 CC BY-SA，法律上要求標示。
    詳情頁在圖片下放一行「圖片：{source} / {author}（{license}）」即可，
    能連到 `source_url` 更好。清單的縮圖可統一在「關於」頁標示。
    這不是建議，是授權條件（見 2.5）。
13. **用 `user.can_quote` 控制報價入口**：不要自己判斷 `role`，
    後端已經算好。身分註冊後不能改，所以這個值在整個 session 內是穩定的，
    可以安心快取。選錯身分的使用者請導向客服，不要在 App 裡提供切換。
