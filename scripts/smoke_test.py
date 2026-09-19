"""端對端煙霧測試：把整條流程跑一遍。

    python scripts/smoke_test.py [--base http://127.0.0.1:8000] [--admin-token xxx]

會做的事：健康檢查 → 觸發 demo_mock 抓取 → 建品項 → 對照代碼 →
查行情走勢 → OTP 登入 → 發一筆報價 → 看總覽。

需要 `.env` 中 `OTP_DEBUG_ECHO=true` 才能自動取得驗證碼。
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from typing import Any

import httpx

from app.core.config import settings

# ISO 3166-2 沒有收錄行政區的國家（小島居多），用來測 fallback 路徑
NO_SUBDIVISION_COUNTRY = "AW"   # 阿魯巴


class Smoke:
    def __init__(self, base: str, admin_token: str) -> None:
        self.base = base.rstrip("/")
        self.api = f"{self.base}/v1"
        self.admin = {"X-Admin-Token": admin_token}
        self.client = httpx.Client(timeout=120.0)
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, extra: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + extra) if extra else ''}")
        if not ok:
            self.failures.append(label)
        return ok

    def req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.api}{path}"
        return self.client.request(method, url, **kw)

    # -- 步驟 -------------------------------------------------------------
    def health(self) -> None:
        print("\n[1] 健康檢查")
        r = self.client.get(f"{self.base}/healthz")
        body = r.json()
        self.check("GET /healthz", r.status_code == 200, str(body))
        self.check("資料庫連線", body.get("database") == "ok")
        self.check("extension 已載入", body.get("extensions_loaded", 0) >= 1)

    def sources(self) -> None:
        print("\n[2] 資料來源")
        r = self.req("GET", "/sources")
        body = r.json()
        keys = [s["key"] for s in body["sources"]]
        self.check("GET /sources", r.status_code == 200, f"keys={keys}")
        self.check("沒有載入失敗的 extension", not body["load_errors"], str(body["load_errors"]))

    def ingest(self) -> dict[str, Any]:
        print("\n[3] 觸發 demo_mock 抓取（近 7 天）")
        end = date.today()
        start = end - timedelta(days=6)
        r = self.req(
            "POST",
            f"/admin/sources/demo_mock/sync?start={start}&end={end}",
            headers=self.admin,
        )
        body = r.json()
        self.check("POST /admin/sources/demo_mock/sync", r.status_code == 200, str(body)[:300])
        self.check("有抓到資料", body.get("fetched", 0) > 0, f"fetched={body.get('fetched')}")
        self.check("有寫入資料", body.get("written", 0) > 0, f"written={body.get('written')}")
        self.check("狀態為成功", body.get("status") in ("success", "partial"), str(body.get("status")))
        return body

    def mappings(self) -> str | None:
        print("\n[4] 品項與代碼對照")
        r = self.req("GET", "/admin/mappings?source_key=demo_mock&limit=50", headers=self.admin)
        all_maps = r.json()["items"]
        self.check("GET /admin/mappings", r.status_code == 200, f"{len(all_maps)} 筆")

        unmapped = [m for m in all_maps if m["product_id"] is None]
        if not unmapped:
            # 代碼可能已經被 automap 全部對照過了。這時候不重複建品項，
            # 直接拿一個既有的對照來驗證後面的價格查詢。
            if not all_maps:
                self.check("有可用的來源代碼", False, "demo_mock 完全沒有對照，請先跑一次 sync")
                return None
            product_id = all_maps[0]["product_id"]
            self.check("代碼皆已對照，沿用既有品項", True, f"product_id={product_id}")
            return product_id

        first = unmapped[0]
        r = self.req(
            "POST",
            "/admin/products",
            headers=self.admin,
            json={
                "slug": f"smoke-{first['external_code'].lower()}",
                "category": "vegetable",
                "default_unit": "kg",
                "names": [
                    {"locale": "zh-Hant", "name": first["external_name"] or "測試品項", "is_primary": True},
                    {"locale": "en", "name": "Smoke Test Product", "is_primary": True},
                ],
            },
        )
        self.check("POST /admin/products", r.status_code == 201, r.text[:200])
        if r.status_code != 201:
            return None
        product = r.json()

        r = self.req(
            "PUT",
            f"/admin/mappings/{first['id']}",
            headers=self.admin,
            json={"product_id": product["id"], "unit_factor": 1.0},
        )
        self.check("PUT /admin/mappings/{id}（含回填）", r.status_code == 200, r.text[:200])
        return product["id"]

    def read_prices(self, product_id: str) -> None:
        print("\n[5] 價格查詢")
        r = self.req("GET", f"/products/{product_id}/prices/official")
        body = r.json()
        self.check("GET 各市場最新官方價", r.status_code == 200, f"{body.get('total')} 筆")
        self.check("回填後查得到價格", body.get("total", 0) > 0)

        r = self.req("GET", f"/products/{product_id}/prices/series?days=7")
        series = r.json()
        self.check("GET 走勢", r.status_code == 200, f"{len(series.get('points', []))} 個點")
        self.check("走勢有資料點", len(series.get("points", [])) > 0)

        r = self.req("GET", f"/products/{product_id}/markets")
        self.check("GET 有資料的市場", r.status_code == 200, f"{len(r.json())} 個市場")

        # 用這個品項自己的名稱回頭搜尋，確認別名索引有建起來
        detail = self.req("GET", f"/products/{product_id}?locale=zh-Hant").json()
        name = detail["name"]
        r = self.req("GET", "/products", params={"q": name, "locale": "zh-Hant"})
        found = [i["id"] for i in r.json().get("items", [])]
        self.check(f"GET 品項搜尋（{name}）",
                   r.status_code == 200 and product_id in found,
                   f"total={r.json().get('total')}")

    def auth(self) -> str | None:
        print("\n[6] OTP 登入")
        # 每次跑用不同號碼，才不會撞到 OTP 冷卻與每小時上限
        phone = f"09{random.randint(10_000_000, 99_999_999)}"
        r = self.req("POST", "/auth/otp/request", json={"phone": phone, "country_code": "TW"})
        body = r.json()
        self.check("POST /auth/otp/request", r.status_code == 202, str(body)[:200])
        self.check("新號碼 is_registered=false", body.get("is_registered") is False,
                   str(body.get("is_registered")))
        code = body.get("debug_code")
        if not code:
            self.check("取得 debug 驗證碼", False, "請設定 OTP_DEBUG_ECHO=true")
            return None

        # 註冊時沒帶身分要被擋下，而且驗證碼不能被消耗掉
        r = self.req("POST", "/auth/otp/verify",
                     json={"phone": phone, "country_code": "TW", "code": code})
        self.check("註冊未指定身分被拒",
                   r.status_code == 400 and r.json()["error"]["code"] == "role_required",
                   r.text[:160])

        r = self.req(
            "POST",
            "/auth/otp/verify",
            json={"phone": phone, "country_code": "TW", "code": code,
                  "role": "farmer", "display_name": "煙霧測試小農"},
        )
        self.check("同一組驗證碼補上身分後可註冊", r.status_code == 200, r.text[:200])
        if r.status_code != 200:
            return None
        tokens = r.json()
        self.check("使用者可報價", tokens["user"]["can_quote"] is True)

        # 錯誤的驗證碼要被擋下來
        other = f"09{random.randint(10_000_000, 99_999_999)}"
        self.req("POST", "/auth/otp/request", json={"phone": other})
        # 新號碼要帶 role，不然會先被 role_required 擋下，測不到驗證碼的檢查
        bad = self.req("POST", "/auth/otp/verify",
                       json={"phone": other, "code": "000000", "role": "consumer"})
        self.check("錯誤驗證碼被拒絕",
                   bad.status_code == 401 and bad.json()["error"]["code"] == "otp_invalid",
                   bad.text[:160])

        # 身分綁定：登入時帶別的 role 不會生效
        auth_hdr = {"Authorization": f"Bearer {tokens['access_token']}"}
        r = self.req("PATCH", "/me", headers=auth_hdr, json={"role": "trader"})
        self.check("PATCH /me 不接受 role", r.status_code == 422, r.text[:160])
        r = self.req("PATCH", "/me", headers=auth_hdr, json={"business_name": "阿明果園"})
        self.check("PATCH /me 可改個人檔案且身分不變",
                   r.status_code == 200 and r.json()["role"] == "farmer"
                   and r.json()["business_name"] == "阿明果園", r.text[:160])

        # refresh 旋轉
        r3 = self.req("POST", "/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.check("POST /auth/refresh", r3.status_code == 200, r3.text[:160])
        reused = self.req("POST", "/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.check("舊 refresh token 不能重用", reused.status_code == 401, reused.text[:160])

        return r3.json()["access_token"] if r3.status_code == 200 else tokens["access_token"]

    def geo(self) -> None:
        print("\n[6b] 國家 / 行政區參考資料")
        r = self.req("GET", "/geo/countries?locale=zh-Hant")
        countries = r.json() if r.status_code == 200 else []
        codes = [c["code"] for c in countries]
        self.check("GET /geo/countries", r.status_code == 200, f"{len(codes)} 國")
        self.check("包含台灣與日本", {"TW", "JP"} <= set(codes), str(codes))

        tw = next((c for c in countries if c["code"] == "TW"), {})
        self.check("國家帶出撥號碼與幣別",
                   tw.get("dialing_code") == "886" and tw.get("currency") == "TWD", str(tw))
        self.check("涵蓋範圍是整份 ISO 3166-1 而非手寫清單",
                   len(codes) > 200, f"{len(codes)} 國")
        us = next((c for c in countries if c["code"] == "US"), {})
        self.check("美國為英制", us.get("unit_system") == "imperial", str(us.get("unit_system")))
        ug = next((c for c in countries if c["code"] == "UG"), {})
        self.check("烏干達可用",
                   ug.get("dialing_code") == "256" and ug.get("currency") == "UGX"
                   and ug.get("default_timezone") == "Africa/Kampala", str(ug))
        self.check("烏干達國名已在地化", ug.get("name") == "烏干達", str(ug.get("name")))

        r = self.req("GET", "/geo/countries/TW/subdivisions?locale=zh-Hant")
        subs = r.json() if r.status_code == 200 else []
        self.check("GET 台灣行政區", r.status_code == 200 and len(subs) == 22, f"{len(subs)} 筆")
        self.check("行政區用 ISO 3166-2 代碼",
                   any(s["code"] == "TW-YUN" and s["name"] == "雲林縣" for s in subs))

        r = self.req("GET", "/geo/countries/JP/subdivisions?locale=ja")
        subs = r.json() if r.status_code == 200 else []
        self.check("GET 日本都道府県", r.status_code == 200 and len(subs) == 47, f"{len(subs)} 筆")

        # 行政區的階層：烏干達的一級太粗，實際單位在第二層
        self.check("烏干達有第二層行政區",
                   ug.get("has_second_level") is True
                   and ug.get("subdivision_label_level2") == "District", str(ug)[:160])
        regions = self.req("GET", "/geo/countries/UG/subdivisions").json()
        self.check("烏干達一級是 4 個 Region",
                   len(regions) == 4 and all(r["level"] == 1 for r in regions),
                   f"{len(regions)} 筆")
        self.check("一級有標記底下還有下一層",
                   all(r["has_children"] for r in regions))
        districts = self.req("GET", "/geo/countries/UG/subdivisions?parent=UG-E").json()
        self.check("可往下鑽出 district",
                   len(districts) > 20
                   and all(d["level"] == 2 and d["parent_code"] == "UG-E" for d in districts),
                   f"{len(districts)} 筆")
        self.check("district 帶出 ISO 類型",
                   any(d["code"] == "UG-203" and d["type"] == "District" for d in districts))

        r = self.req("GET", f"/geo/countries/{NO_SUBDIVISION_COUNTRY}/subdivisions")
        self.check("ISO 未收錄行政區的國家回空陣列而非錯誤",
                   r.status_code == 200 and r.json() == [], r.text[:120])
        r = self.req("GET", "/geo/countries/ZZ")
        self.check("未支援的國家回 404", r.status_code == 404, r.text[:120])

    def profile_and_location(self, token: str) -> None:
        print("\n[6c] 個人檔案與位置")
        auth = {"Authorization": f"Bearer {token}"}

        r = self.req("GET", "/me", headers=auth)
        me = r.json() if r.status_code == 200 else {}
        self.check("GET /me 帶出位置區塊", "location" in me, str(list(me))[:160])
        self.check("尚未登記位置", me.get("has_location") is False, str(me.get("has_location")))
        self.check("幣別依國家推導", me.get("currency") == "TWD", str(me.get("currency")))

        r = self.req("PUT", "/me/location", headers=auth, json={
            "country_code": "TW",
            "subdivision_code": "TW-YUN",
            "locality": "西螺鎮",
            "address_line": "延平路 100 號",
            "postal_code": "648",
            "latitude": 23.797512,
            "longitude": 120.465843,
            "visibility": "region",
        })
        body = r.json() if r.status_code == 200 else {}
        self.check("PUT /me/location", r.status_code == 200, r.text[:200])
        loc = body.get("location", {})
        self.check("位置已登記", body.get("has_location") is True)
        self.check("行政區名稱已解析", loc.get("subdivision_name") == "雲林縣", str(loc)[:160])
        self.check("時區依國家補上", loc.get("timezone") == "Asia/Taipei", str(loc.get("timezone")))
        self.check("地址由大到小組好",
                   loc.get("formatted") == "648 台灣 雲林縣 西螺鎮 延平路 100 號",
                   str(loc.get("formatted")))

        r = self.req("PUT", "/me/location", headers=auth,
                     json={"country_code": "JP", "subdivision_code": "TW-YUN"})
        self.check("行政區不屬於該國被拒", r.status_code == 422, r.text[:160])
        r = self.req("PUT", "/me/location", headers=auth, json={"country_code": "ZZ"})
        self.check("未支援的國家被拒", r.status_code == 422, r.text[:160])
        r = self.req("PUT", "/me/location", headers=auth,
                     json={"country_code": "TW", "latitude": 23.7})
        self.check("經緯度未成對被拒", r.status_code == 422, r.text[:160])

        r = self.req("PUT", "/me/location", headers=auth, json={
            "country_code": "US", "locality": "Fresno, CA", "postal_code": "93721",
        })
        us_body = r.json() if r.status_code == 200 else {}
        us_loc = us_body.get("location", {})
        self.check("未收錄行政區的國家可用 locality 登記", r.status_code == 200, r.text[:200])
        # 順序是小到大（歐美慣例），國名用的是「使用者自己的語系」，
        # 所以 zh-Hant 的使用者看到的是「美國」而不是 United States
        self.check("美國地址由小到大",
                   us_loc.get("formatted") == "Fresno, CA, 93721, 美國",
                   str(us_loc.get("formatted")))
        self.check("換國家後幣別跟著變", us_body.get("currency") == "USD",
                   str(us_body.get("currency")))
        self.check("換國家後單位制跟著變",
                   us_body.get("effective_unit_system") == "imperial",
                   str(us_body.get("effective_unit_system")))
        self.check("換國家會清掉舊的行政區代碼",
                   us_loc.get("subdivision_code") is None, str(us_loc.get("subdivision_code")))

        # 烏干達：用第二層的 district 登記位置
        r = self.req("PUT", "/me/location", headers=auth, json={
            "country_code": "UG", "subdivision_code": "UG-203", "locality": "Iganga Town",
        })
        ug_body = r.json() if r.status_code == 200 else {}
        ug_loc = ug_body.get("location", {})
        self.check("可用第二層 district 登記位置", r.status_code == 200, r.text[:200])
        self.check("烏干達幣別與時區正確",
                   ug_body.get("currency") == "UGX"
                   and ug_loc.get("timezone") == "Africa/Kampala", str(ug_loc)[:160])
        self.check("地址補上上一層的大區",
                   ug_loc.get("formatted") == "Iganga Town, Iganga, Eastern, 烏干達",
                   str(ug_loc.get("formatted")))
        r = self.req("PUT", "/me/location", headers=auth,
                     json={"country_code": "UG", "subdivision_code": "TW-YUN"})
        self.check("跨國的行政區代碼仍被拒", r.status_code == 422, r.text[:140])

        user_id = me.get("id")

        self.req("PUT", "/me/location", headers=auth, json={
            "country_code": "TW", "subdivision_code": "TW-YUN", "locality": "西螺鎮",
            "visibility": "region",
        })
        r = self.req("GET", "/me/location", headers=auth)
        self.check("GET /me/location", r.status_code == 200, r.text[:160])

        r = self.req("GET", f"/users/{user_id}")
        pub = r.json() if r.status_code == 200 else {}
        self.check("GET /users/{id}", r.status_code == 200, r.text[:200])
        self.check("公開檔案不含電話", "phone" not in pub, str(list(pub))[:160])
        self.check("region 層級不給座標",
                   pub.get("location", {}).get("latitude") is None
                   and pub.get("location", {}).get("precision") == "hidden",
                   str(pub.get("location"))[:160])

        self.req("PUT", "/me/location", headers=auth, json={
            "country_code": "TW", "subdivision_code": "TW-YUN", "visibility": "private",
        })
        pub = self.req("GET", f"/users/{user_id}").json()
        self.check("private 只露出國家",
                   pub["location"]["subdivision_code"] is None
                   and pub["location"]["formatted"] == "台灣", str(pub["location"])[:160])

        self.req("PUT", "/me/location", headers=auth, json={
            "country_code": "TW", "subdivision_code": "TW-YUN", "locality": "西螺鎮",
            "latitude": 23.797512, "longitude": 120.465843, "visibility": "approximate",
        })
        pub = self.req("GET", f"/users/{user_id}").json()
        self.check("approximate 給模糊座標",
                   pub["location"]["latitude"] == 23.8
                   and pub["location"]["precision"] == "approximate_1km",
                   str(pub["location"])[:160])
        self.check("approximate 不給街道地址", pub["location"]["address_line"] is None)

        r = self.req("GET", f"/users/{user_id}/quotes")
        self.check("GET /users/{id}/quotes", r.status_code == 200, r.text[:160])
        r = self.req("GET", "/users/00000000-0000-0000-0000-000000000000")
        self.check("不存在的使用者回 404", r.status_code == 404, r.text[:120])

        r = self.req("PATCH", "/me", headers=auth, json={
            "bio": "種了 20 年的西螺米", "website_url": "https://example.org/farm",
            "preferred_currency": "jpy",
        })
        body = r.json() if r.status_code == 200 else {}
        self.check("PATCH /me 個人檔案", r.status_code == 200, r.text[:200])
        self.check("幣別轉大寫", body.get("preferred_currency") == "JPY",
                   str(body.get("preferred_currency")))
        self.check("明確設定的幣別優先於國家預設", body.get("currency") == "JPY",
                   str(body.get("currency")))

        r = self.req("PATCH", "/me", headers=auth, json={"website_url": "javascript:alert(1)"})
        self.check("非 http 網址被拒", r.status_code == 422, r.text[:160])

        self.req("PATCH", "/me", headers=auth, json={"preferred_currency": None})
        self.req("PUT", "/me/location", headers=auth, json={
            "country_code": "TW", "subdivision_code": "TW-YUN", "locality": "西螺鎮",
        })

        r = self.req("DELETE", "/me/location", headers=auth)
        self.check("DELETE /me/location 清除位置但保留國家",
                   r.status_code == 200 and r.json()["has_location"] is False
                   and r.json()["country_code"] == "TW", r.text[:200])

    def quotes(self, token: str, product_id: str) -> None:
        print("\n[7] 報價")
        auth = {"Authorization": f"Bearer {token}"}
        r = self.req("POST", "/quotes", headers=auth, json={
            "product_id": product_id,
            "price": "33.5",
            "side": "sell",
            "quantity": "120",
            "note": "煙霧測試報價",
            "valid_hours": 24,
        })
        self.check("POST /quotes", r.status_code == 201, r.text[:250])
        if r.status_code != 201:
            return
        quote = r.json()

        r = self.req("GET", f"/quotes?product_id={product_id}")
        self.check("GET /quotes", r.status_code == 200 and r.json()["total"] > 0,
                   f"total={r.json().get('total')}")

        r = self.req("PATCH", f"/quotes/{quote['id']}", headers=auth, json={"price": "35.0"})
        # 價格是 Decimal，序列化後的小數位數不固定，所以比數值不比字串
        patched = float(r.json()["price"]) if r.status_code == 200 else None
        self.check("PATCH /quotes/{id}", patched == 35.0, f"price={patched}")

        r = self.req("GET", "/me/quotes", headers=auth)
        self.check("GET /me/quotes", r.status_code == 200 and r.json()["total"] > 0)

        # 未登入不能報價
        r = self.req("POST", "/quotes", json={"product_id": product_id, "price": "1"})
        self.check("未登入無法報價", r.status_code == 401, r.text[:120])

        r = self.req("DELETE", f"/quotes/{quote['id']}", headers=auth)
        self.check("DELETE /quotes/{id}（下架）",
                   r.status_code == 200 and r.json()["status"] == "withdrawn", r.text[:160])

    def overview(self, product_id: str) -> None:
        print("\n[8] 品項總覽")
        r = self.req("GET", f"/products/{product_id}/overview?days=7")
        body = r.json()
        self.check("GET /products/{id}/overview", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            self.check("含官方價", len(body.get("official", [])) > 0,
                       f"{len(body.get('official', []))} 個市場")
            self.check("含走勢", len(body.get("official_series", {}).get("points", [])) > 0)
            self.check("含報價摘要", "count" in body.get("quotes", {}))

    def admin_guard(self) -> None:
        print("\n[9] 管理端點保護")
        r = self.req("GET", "/admin/mappings")
        self.check("沒帶 X-Admin-Token 被拒", r.status_code == 401, r.text[:120])
        r = self.req("GET", "/admin/mappings", headers={"X-Admin-Token": "wrong"})
        self.check("錯誤的 X-Admin-Token 被拒", r.status_code == 401, r.text[:120])

    def run(self) -> int:
        print(f"針對 {self.base} 執行煙霧測試")
        self.health()
        self.sources()
        self.ingest()
        product_id = self.mappings()
        if product_id:
            self.read_prices(product_id)
            token = self.auth()
            if token:
                self.geo()
                self.profile_and_location(token)
                self.quotes(token, product_id)
            self.overview(product_id)
        self.admin_guard()

        print("\n" + "=" * 60)
        if self.failures:
            print(f"失敗 {len(self.failures)} 項：")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print("全部通過")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-token", default=settings.admin_api_token or "")
    args = parser.parse_args()
    if not args.admin_token:
        print("需要 --admin-token 或在 .env 設定 ADMIN_API_TOKEN")
        return 2
    return Smoke(args.base, args.admin_token).run()


if __name__ == "__main__":
    sys.exit(main())
