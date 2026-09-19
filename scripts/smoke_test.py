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
        r = self.req("GET", "/admin/mappings?source_key=demo_mock&unmapped_only=true&limit=50",
                     headers=self.admin)
        unmapped = r.json()["items"]
        self.check("有待對應的來源代碼", len(unmapped) > 0, f"{len(unmapped)} 筆")
        if not unmapped:
            return None

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

        r = self.req("GET", "/products?q=Smoke&locale=en")
        self.check("GET 品項搜尋（英文別名）", r.status_code == 200 and r.json()["total"] > 0,
                   f"total={r.json().get('total')}")

    def auth(self) -> str | None:
        print("\n[6] OTP 登入")
        # 每次跑用不同號碼，才不會撞到 OTP 冷卻與每小時上限
        phone = f"09{random.randint(10_000_000, 99_999_999)}"
        r = self.req("POST", "/auth/otp/request", json={"phone": phone, "country_code": "TW"})
        body = r.json()
        self.check("POST /auth/otp/request", r.status_code == 202, str(body)[:200])
        code = body.get("debug_code")
        if not code:
            self.check("取得 debug 驗證碼", False, "請設定 OTP_DEBUG_ECHO=true")
            return None

        r = self.req(
            "POST",
            "/auth/otp/verify",
            json={"phone": phone, "country_code": "TW", "code": code,
                  "role": "farmer", "display_name": "煙霧測試小農"},
        )
        self.check("POST /auth/otp/verify", r.status_code == 200, r.text[:200])
        if r.status_code != 200:
            return None
        tokens = r.json()
        self.check("使用者可報價", tokens["user"]["can_quote"] is True)

        # 錯誤的驗證碼要被擋下來
        other = f"09{random.randint(10_000_000, 99_999_999)}"
        self.req("POST", "/auth/otp/request", json={"phone": other})
        bad = self.req("POST", "/auth/otp/verify", json={"phone": other, "code": "000000"})
        self.check("錯誤驗證碼被拒絕", bad.status_code == 401, bad.text[:160])

        # refresh 旋轉
        r3 = self.req("POST", "/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.check("POST /auth/refresh", r3.status_code == 200, r3.text[:160])
        reused = self.req("POST", "/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.check("舊 refresh token 不能重用", reused.status_code == 401, reused.text[:160])

        return r3.json()["access_token"] if r3.status_code == 200 else tokens["access_token"]

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
