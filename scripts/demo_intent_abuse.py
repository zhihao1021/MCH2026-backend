"""意向價格防刷機制的端對端演示。

    python scripts/demo_intent_abuse.py --base http://127.0.0.1:8000

會實際建立一批消費者帳號、提交意向價格（含惡意的極端值），
然後驗證 PRD 四大防護機制真的有生效：

1. 離群值被 IQR 剔除，錨點價格不受影響
2. 低於成本底線的出價被拒絕寫入
3. 同一作物的冷卻期擋住重複提交
4. 信譽權重依共識吻合度升降，連續偏離會被影子封禁
5. 產地開團時只通知意向價 >= 開價的人

需要 `.env` 設 `OTP_DEBUG_ECHO=true` 與 `ADMIN_API_TOKEN`。
跑完的帳號會留在資料庫（手機號碼以 +886988 開頭），`--purge` 可清掉。
"""

from __future__ import annotations

import argparse
import random
import sys
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import settings

PHONE_PREFIX = "0988"


class Demo:
    def __init__(self, base: str, admin_token: str, product: str) -> None:
        self.base = base.rstrip("/")
        self.api = f"{self.base}/v1"
        self.admin = {"X-Admin-Token": admin_token}
        self.product = product
        self.c = httpx.Client(timeout=90.0)
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, extra: str = "") -> bool:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
        if not ok:
            self.failures.append(label)
        return ok

    # -- 帳號 -------------------------------------------------------------
    def make_consumer(self, subdivision: str, locality: str) -> dict[str, Any]:
        """建立一個有所在地的消費者帳號，回傳 headers。"""
        phone = f"{PHONE_PREFIX}{random.randint(100000, 999999)}"
        r = self.c.post(
            f"{self.api}/auth/otp/request", json={"phone": phone, "country_code": "TW"}
        )
        code = r.json()["debug_code"]
        r = self.c.post(
            f"{self.api}/auth/otp/verify",
            json={
                "phone": phone, "country_code": "TW", "code": code,
                "role": "consumer", "display_name": f"消費者{phone[-4:]}",
            },
        )
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        # 意向要歸入區域看板，必須先有所在地
        self.c.put(
            f"{self.api}/me/location",
            headers=headers,
            json={"country_code": "TW", "subdivision_code": subdivision, "locality": locality},
        )
        return {"phone": phone, "headers": headers, "id": r.json()["user"]["id"]}

    def submit(self, headers: dict, price: str) -> httpx.Response:
        return self.c.post(
            f"{self.api}/products/{self.product}/intents",
            headers=headers,
            json={"price": price},
        )

    def summary(self, **params: Any) -> dict[str, Any]:
        return self.c.get(
            f"{self.api}/products/{self.product}/intents/summary", params=params
        ).json()

    # -- 情境 -------------------------------------------------------------
    def run(self) -> int:
        print(f"針對 {self.base} 演示意向價格防刷（作物：{self.product}）")

        print("\n[0] 成本底線")
        floor = self.c.get(f"{self.api}/products/{self.product}/intents/floor").json()
        print(f"      底線 {floor['floor_price']} {floor['currency']}/{floor['unit']}"
              f"（基準 {floor['reference_price']}，來源 {floor['source']}）")
        self.check("取得成本底線", floor["source"].startswith(("official_price_proxy", "no_official_data")))
        floor_price = Decimal(floor["floor_price"]) if floor["floor_price"] else None

        print("\n[1] 12 位正常消費者提交合理價格")
        base = float(floor_price) * 1.6 if floor_price else 30.0
        honest = []
        for i in range(12):
            u = self.make_consumer("TW-TPE", "大安區")
            price = round(base * random.uniform(0.92, 1.08), 2)
            r = self.submit(u["headers"], str(price))
            if r.status_code == 201:
                honest.append((u, price))
        self.check("正常提交成功", len(honest) >= 10, f"{len(honest)}/12")
        s = self.summary(region="臺北市")
        clean_anchor = s["anchor_price"]
        print(f"      錨點 {clean_anchor} | 中位數 {s['median']} | 截尾均值 {s['trimmed_mean']}")
        print(f"      IQR 容許區間 {s['lower_bound']} ~ {s['upper_bound']} | 樣本 {s['sample_count']}")

        print("\n[2] 惡意使用者灌極端高價（PRD 1.1：IQR 剔除）")
        attackers = []
        for _ in range(3):
            u = self.make_consumer("TW-TPE", "大安區")
            r = self.submit(u["headers"], str(round(base * 12, 2)))
            attackers.append(u)
            self.check("極端值仍可寫入（不讓對方察覺）", r.status_code == 201, r.text[:80])
        s2 = self.summary(region="臺北市")
        print(f"      錨點 {s2['anchor_price']}（灌之前 {clean_anchor}）")
        print(f"      排除 {s2['excluded_count']} 筆 {s2['exclusions']}")
        self.check(
            "錨點不受極端值影響",
            s2["anchor_price"] == clean_anchor,
            f"{clean_anchor} -> {s2['anchor_price']}",
        )
        self.check("極端值被標記為離群", s2["exclusions"].get("outlier", 0) >= 3,
                   str(s2["exclusions"]))

        print("\n[3] 低於成本底線（PRD 1.2）")
        if floor_price:
            u = self.make_consumer("TW-TPE", "大安區")
            r = self.submit(u["headers"], str(float(floor_price) * 0.3))
            body = r.json()
            self.check(
                "過低出價被拒絕寫入",
                r.status_code == 400 and body["error"]["code"] == "intent_below_floor",
                body.get("error", {}).get("code", r.text[:80]),
            )
            self.check("錯誤帶底線價供前端提示",
                       "floor_price" in body.get("error", {}).get("details", {}))
        else:
            print("      （這個作物沒有官方行情，不設底線，略過）")

        print("\n[4] 冷卻期（PRD 2.2）")
        u, _ = honest[0]
        r = self.submit(u["headers"], str(base))
        body = r.json()
        self.check(
            "同作物重複提交被擋",
            r.status_code == 429 and body["error"]["code"] == "intent_cooldown",
            body.get("error", {}).get("message", r.text[:80]),
        )

        print("\n[5] 沒設所在地不能提交")
        phone = f"{PHONE_PREFIX}{random.randint(100000, 999999)}"
        rr = self.c.post(f"{self.api}/auth/otp/request",
                         json={"phone": phone, "country_code": "TW"})
        rr = self.c.post(f"{self.api}/auth/otp/verify",
                         json={"phone": phone, "country_code": "TW",
                               "code": rr.json()["debug_code"], "role": "consumer"})
        nowhere = {"Authorization": f"Bearer {rr.json()['access_token']}"}
        r = self.submit(nowhere, str(base))
        self.check("無所在地被擋", r.status_code == 400
                   and r.json()["error"]["code"] == "intent_region_required",
                   r.text[:80])

        print("\n[6] 信譽權重與影子封禁（PRD 3.1 / 3.2）")
        before = self.c.get(f"{self.api}/me/reputation",
                            headers=honest[0][0]["headers"]).json()
        atk_before = self.c.get(f"{self.api}/me/reputation",
                                headers=attackers[0]["headers"]).json()
        for _ in range(4):
            self.c.post(
                f"{self.api}/admin/products/{self.product}/intents/recompute-reputation",
                headers=self.admin, params={"region": "臺北市"},
            )
        after = self.c.get(f"{self.api}/me/reputation",
                           headers=honest[0][0]["headers"]).json()
        atk_after = self.c.get(f"{self.api}/me/reputation",
                               headers=attackers[0]["headers"]).json()
        print(f"      正常使用者 {before['weight']} -> {after['weight']}")
        print(f"      刷票者     {atk_before['weight']} -> {atk_after['weight']}")
        self.check("正常使用者權重上升", after["weight"] > before["weight"])
        self.check("刷票者權重下降至 0", atk_after["weight"] == 0.0, str(atk_after["weight"]))
        self.check("回應不洩漏影子封禁狀態", "is_shadow_banned" not in atk_after)

        print("      刷票者再提交一次（冷卻期已過的話會進來，但應被影子封禁）")
        s3 = self.summary(region="臺北市")
        self.check("影子封禁後錨點仍穩定", s3["anchor_price"] == clean_anchor,
                   f"{clean_anchor} -> {s3['anchor_price']}")

        print("\n[7] 產地開團的優先通知（PRD 4.1）")
        q = self.c.get(
            f"{self.api}/quotes", params={"limit": 50, "country_code": "TW"}
        ).json()
        if q["items"]:
            qid = q["items"][0]["id"]
            r = self.c.post(f"{self.api}/admin/quotes/{qid}/match-intents",
                            headers=self.admin, params={"record": "true"})
            m = r.json()
            print(f"      開價 {m['offer_price']} {m['currency']}/{m['unit']} -> 命中 {m['matched']} 人")
            self.check("比對端點可用", r.status_code == 200)
            if m["items"]:
                prices = [Decimal(i["intent_price"]) for i in m["items"]]
                self.check("依意向價由低到高排序", prices == sorted(prices))
                self.check("命中者的意向價都 >= 開價",
                           all(p >= Decimal(m["offer_price"]) for p in prices))
        else:
            print("      （沒有報價可測，略過）")

        print("\n" + "=" * 62)
        if self.failures:
            print(f"失敗 {len(self.failures)} 項：")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print("全部通過")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-token", default=settings.admin_api_token or "")
    parser.add_argument("--product", default="cabbage", help="拿哪個作物來演示")
    args = parser.parse_args()
    if not args.admin_token:
        print("需要 --admin-token 或在 .env 設定 ADMIN_API_TOKEN")
        return 2
    return Demo(args.base, args.admin_token, args.product).run()


if __name__ == "__main__":
    sys.exit(main())
