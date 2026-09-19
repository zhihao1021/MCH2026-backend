"""建立一批有完整個人檔案與位置的示範小農，以及他們的報價。

    python scripts/seed_demo_farmers.py            # 建立 / 更新
    python scripts/seed_demo_farmers.py --purge    # 刪掉這批資料

可重複執行：以手機號碼為鍵，已存在就更新，不會重複建立。

資料刻意跨三個國家、四種位置公開程度，把「不只台灣會用」的路徑都走過一遍：
- 台灣：有 ISO 3166-2 行政區清單，用 subdivision_code
- 日本：同上，但語系、幣別、地址寫法都不同
- 美國：尚未收錄州別清單，只能用自由輸入的 locality，且以磅計價

所有號碼都在保留給測試的區段（NANP 的 555、以及明顯連號的假號碼），
不會撞到真實用戶。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.core.database import session_scope
from app.models.catalog import Market, Product
from app.models.enums import LocationVisibility, QuoteSide, UnitSystem, UserRole
from app.models.quote import Quote
from app.models.user import RefreshToken, User
from app.schemas.profile import LocationIn, ProfileUpdate
from app.services import profile as profile_service
from app.services import quotes as quote_service


@dataclass
class QuoteSpec:
    product_slug: str
    price: str
    unit: str = "kg"
    quantity: str | None = None
    min_order: str | None = None
    grade: str | None = None
    note: str | None = None
    # 批發市場的來源代碼（tw_moa 的 MarketCode），對得到才會掛上去
    market_external_id: str | None = None


@dataclass
class FarmerSpec:
    phone: str
    display_name: str
    business_name: str
    bio: str
    location: dict
    quotes: list[QuoteSpec]
    locale: str = "zh-Hant"
    website_url: str | None = None
    preferred_currency: str | None = None
    unit_system: UnitSystem | None = None
    contact_phone_public: bool = True
    tags: list[str] = field(default_factory=list)


FARMERS: list[FarmerSpec] = [
    # ---- 台灣：四種位置公開程度各一 ----
    FarmerSpec(
        phone="+886900000101",
        display_name="陳文山",
        business_name="西螺溪畔米舖",
        bio="濁水溪沖積平原種了三十年的稻米，自產自銷，可代客碾米。",
        website_url="https://example.org/hsiluo-rice",
        location=dict(
            country_code="TW", subdivision_code="TW-YUN", locality="西螺鎮",
            address_line="延平路 168 號", postal_code="648",
            latitude=23.797512, longitude=120.465843,
            visibility=LocationVisibility.REGION,
        ),
        quotes=[
            QuoteSpec("rice", "48.0", quantity="2000", min_order="30",
                      grade="台梗九號", note="新期稻穀，可分裝 30 台斤一袋",
                      market_external_id="104"),
            QuoteSpec("white-radish", "22.5", quantity="800", min_order="50",
                      note="清晨採收當日出貨"),
        ],
    ),
    FarmerSpec(
        phone="+886900000102",
        display_name="林淑芬",
        business_name="埔里高山農場",
        bio="海拔 800 公尺的冷涼蔬菜，夏季高麗菜為主力。無毒栽培第八年。",
        location=dict(
            country_code="TW", subdivision_code="TW-NAN", locality="埔里鎮",
            address_line="中山路四段 21 巷 5 號", postal_code="545",
            latitude=23.965061, longitude=120.967831,
            # 願意露出大概位置，但不想公開確切地址
            visibility=LocationVisibility.APPROXIMATE,
        ),
        quotes=[
            QuoteSpec("cabbage", "31.0", quantity="1500", min_order="100",
                      grade="初秋", market_external_id="400"),
            QuoteSpec("lettuce", "58.0", quantity="300", note="冷藏配送"),
            QuoteSpec("chinese-chive", "72.0", quantity="120"),
        ],
    ),
    FarmerSpec(
        phone="+886900000103",
        display_name="黃志明",
        business_name="玉井愛文芒果園",
        bio="玉井老欉愛文，產期五月底到七月。開放預約採果與現場自取。",
        website_url="https://example.org/yujing-mango",
        location=dict(
            country_code="TW", subdivision_code="TW-TNN", locality="玉井區",
            address_line="中正路 139 號", postal_code="714",
            latitude=23.124271, longitude=120.460531,
            # 開放自取，所以完整地址與座標都公開
            visibility=LocationVisibility.EXACT,
        ),
        quotes=[
            QuoteSpec("mango", "128.0", quantity="600", min_order="5",
                      grade="愛文 12A", note="可現場自取，需先電話預約",
                      market_external_id="700"),
            QuoteSpec("guava", "46.0", quantity="400"),
        ],
    ),
    FarmerSpec(
        phone="+886900000104",
        display_name="吳美玲",
        business_name="枋山小農直送",
        bio="枋山愛文與蓮霧，只做宅配。不便公開住址，請用電話聯絡。",
        location=dict(
            country_code="TW", subdivision_code="TW-PIF", locality="枋山鄉",
            postal_code="941",
            # 只做宅配，位置完全不公開
            visibility=LocationVisibility.PRIVATE,
        ),
        quotes=[
            QuoteSpec("mango", "152.0", quantity="350", min_order="10",
                      grade="枋山愛文 特選", note="宅配含箱，滿 10 kg 免運"),
            QuoteSpec("wax-apple", "98.0", quantity="200", grade="黑珍珠"),
            QuoteSpec("pineapple", "36.0", quantity="900", min_order="20"),
        ],
    ),
    # ---- 日本：語系、幣別、地址寫法都不同 ----
    FarmerSpec(
        phone="+819000000105",
        display_name="佐藤 健一",
        business_name="さとう農園",
        bio="北海道十勝のじゃがいもと玉ねぎ。契約栽培を中心に、少量からも対応します。",
        locale="ja",
        location=dict(
            country_code="JP", subdivision_code="JP-01", locality="帯広市",
            address_line="西 5 条南 7 丁目 1", postal_code="080-0012",
            latitude=42.923759, longitude=143.196469,
            visibility=LocationVisibility.REGION,
        ),
        quotes=[
            QuoteSpec("potato", "320", quantity="5000", min_order="500",
                      grade="男爵 Lサイズ", note="10 kg 段ボール単位での出荷"),
            QuoteSpec("onion", "285", quantity="3000", min_order="500"),
            QuoteSpec("pumpkin", "410", quantity="800", grade="えびす"),
        ],
    ),
    # ---- 美國：沒有州別清單，只能用自由輸入的城鎮；以磅計價 ----
    FarmerSpec(
        phone="+12125550147",
        display_name="Maria Gonzalez",
        business_name="Gonzalez Family Farm",
        bio="Third-generation table grape and stone fruit grower in the Central Valley. "
            "Wholesale by the bin, pickup available.",
        locale="en",
        website_url="https://example.org/gonzalez-farm",
        location=dict(
            country_code="US",
            # 美國尚未收錄州別清單，州名寫在自由輸入的 locality 裡
            locality="Fresno, CA",
            address_line="4821 E Central Ave", postal_code="93725",
            latitude=36.703500, longitude=-119.717100,
            visibility=LocationVisibility.APPROXIMATE,
        ),
        # 美國農產品以磅計價，這裡不設 preferred_currency，讓它跟著國家走（USD）
        quotes=[
            QuoteSpec("grape", "3.25", unit="lb", quantity="12000", min_order="500",
                      grade="Flame Seedless", note="Sold by the 18 lb lug"),
            QuoteSpec("peach", "2.10", unit="lb", quantity="8000", min_order="250"),
            QuoteSpec("sweet-corn", "0.95", unit="lb", quantity="20000",
                      note="Field run, harvested daily"),
        ],
    ),
]

SEED_PHONES = [f.phone for f in FARMERS]


async def _market_id(session, external_id: str | None):
    if not external_id:
        return None
    return await session.scalar(
        select(Market.id).where(Market.external_id == external_id).limit(1)
    )


async def seed() -> int:
    created_users = updated_users = created_quotes = skipped_quotes = 0

    async with session_scope() as session:
        for spec in FARMERS:
            user = await session.scalar(select(User).where(User.phone == spec.phone))
            if user is None:
                user = User(phone=spec.phone, role=UserRole.FARMER, locale=spec.locale,
                            country_code=spec.location["country_code"],
                            phone_verified_at=datetime.now(UTC))
                session.add(user)
                await session.flush()
                created_users += 1
            else:
                updated_users += 1

            # 走正式的服務層，確保資料跟 API 產生的完全一致（含驗證）
            await profile_service.update_profile(session, user, ProfileUpdate(
                display_name=spec.display_name,
                business_name=spec.business_name,
                bio=spec.bio,
                website_url=spec.website_url,
                locale=spec.locale,
                preferred_currency=spec.preferred_currency,
                unit_system=spec.unit_system,
                contact_phone_public=spec.contact_phone_public,
            ))
            await profile_service.set_location(session, user, LocationIn(**spec.location))

            # 報價每次重建，免得重跑時越積越多
            await session.execute(delete(Quote).where(Quote.user_id == user.id))

            for q in spec.quotes:
                product = await session.scalar(
                    select(Product).where(Product.slug == q.product_slug)
                )
                if product is None:
                    print(f"  ! 找不到品項 {q.product_slug}，略過這筆報價")
                    skipped_quotes += 1
                    continue
                await quote_service.create_quote(
                    session, user,
                    product_id=product.id,
                    price=Decimal(q.price),
                    side=QuoteSide.SELL,
                    unit=q.unit,
                    grade=q.grade,
                    quantity=Decimal(q.quantity) if q.quantity else None,
                    min_order=Decimal(q.min_order) if q.min_order else None,
                    market_id=await _market_id(session, q.market_external_id),
                    note=q.note,
                    valid_hours=72,
                )
                created_quotes += 1

            print(f"  {spec.phone}  {spec.business_name}"
                  f"（{spec.location['country_code']}，{len(spec.quotes)} 筆報價）")

    print(f"\n新增使用者 {created_users} 位，更新 {updated_users} 位，"
          f"建立報價 {created_quotes} 筆" + (f"，略過 {skipped_quotes} 筆" if skipped_quotes else ""))
    return 0


async def purge() -> int:
    async with session_scope() as session:
        users = list((await session.execute(
            select(User).options(selectinload(User.quotes)).where(User.phone.in_(SEED_PHONES))
        )).scalars())
        for user in users:
            await session.execute(delete(Quote).where(Quote.user_id == user.id))
            await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
            await session.delete(user)
            print(f"  已刪除 {user.phone}  {user.business_name or user.display_name}")
    print(f"\n共刪除 {len(users)} 位示範小農")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purge", action="store_true", help="刪除這批示範資料")
    args = parser.parse_args()
    return asyncio.run(purge() if args.purge else seed())


if __name__ == "__main__":
    sys.exit(main())
