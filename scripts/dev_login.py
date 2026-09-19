"""開發用：一行指令走完 OTP 登入，拿到可以直接用的 access token。

    python scripts/dev_login.py
    python scripts/dev_login.py --phone 0987654321 --role trader
    python scripts/dev_login.py --export          # 印出 export 指令，方便餵給 shell

需要 `.env` 設定：

    OTP_DEBUG_ECHO=true      # 驗證碼直接回在 response，才能自動化
    SMS_PROVIDER=console     # 不會真的發簡訊，只印在伺服器 log

`OTP_DEBUG_ECHO` 在 `ENVIRONMENT=production` 時一律無效，
所以就算不小心把 true 帶上正式環境，驗證碼也不會外洩。
"""

from __future__ import annotations

import argparse
import random
import sys

import httpx

from app.core.config import settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="API 位址")
    parser.add_argument(
        "--phone",
        help="手機號碼，省略則隨機產生一個（避開 OTP 冷卻與每小時上限）",
    )
    parser.add_argument("--country-code", default=settings.default_country_code)
    parser.add_argument(
        "--role",
        default="farmer",
        choices=["consumer", "farmer", "trader"],
        help="首次登入時要建立的身分（既有帳號不會被覆寫）",
    )
    parser.add_argument("--name", default="開發測試帳號")
    parser.add_argument("--export", action="store_true", help="只印出 shell 的 export 指令")
    args = parser.parse_args()

    api = args.base.rstrip("/") + "/v1"
    phone = args.phone or f"09{random.randint(10_000_000, 99_999_999)}"
    out = sys.stderr if args.export else sys.stdout

    with httpx.Client(timeout=30.0) as client:
        # 1. 索取驗證碼
        r = client.post(
            f"{api}/auth/otp/request",
            json={"phone": phone, "country_code": args.country_code},
        )
        if r.status_code != 202:
            print(f"索取驗證碼失敗（{r.status_code}）：{r.text}", file=sys.stderr)
            if r.status_code == 429:
                print(
                    "提示：同一號碼有 60 秒冷卻與每小時 5 次上限。"
                    "省略 --phone 會隨機產生號碼，或調低 .env 的 "
                    "OTP_RESEND_COOLDOWN_SECONDS / 調高 OTP_MAX_PER_PHONE_PER_HOUR。",
                    file=sys.stderr,
                )
            return 1

        body = r.json()
        code = body.get("debug_code")
        if not code:
            print(
                "response 沒有 debug_code。請在 .env 設 OTP_DEBUG_ECHO=true "
                "且 ENVIRONMENT 不是 production；\n"
                "或者直接看伺服器 log 裡的 [SMS:console] 那行手動輸入。",
                file=sys.stderr,
            )
            return 1

        print(f"手機 {body['phone']}  驗證碼 {code}", file=out)

        # 2. 驗證並登入
        r = client.post(
            f"{api}/auth/otp/verify",
            json={
                "phone": phone,
                "country_code": args.country_code,
                "code": code,
                "role": args.role,
                "display_name": args.name,
            },
        )
        if r.status_code != 200:
            print(f"驗證失敗（{r.status_code}）：{r.text}", file=sys.stderr)
            return 1

        tok = r.json()
        user = tok["user"]

    if args.export:
        # eval "$(python scripts/dev_login.py --export)"
        print(f'export ACCESS_TOKEN="{tok["access_token"]}"')
        print(f'export REFRESH_TOKEN="{tok["refresh_token"]}"')
        print(f"已登入 {user['phone']}（{user['role']}）", file=sys.stderr)
        return 0

    print(f"新帳號    : {tok['is_new_user']}")
    print(f"身分      : {user['role']}（can_quote={user['can_quote']}）")
    print(f"有效期    : {tok['expires_in']} 秒")
    print()
    print("ACCESS_TOKEN:")
    print(tok["access_token"])
    print()
    print("試用：")
    print(f'  curl -s {args.base}/v1/me -H "Authorization: Bearer $ACCESS_TOKEN"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
