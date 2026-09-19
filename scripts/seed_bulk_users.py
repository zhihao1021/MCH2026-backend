"""批次產生小農與盤商，以及他們的報價。

    python scripts/seed_bulk_users.py                        # 30 小農 + 10 盤商
    python scripts/seed_bulk_users.py --farmers 50 --traders 15
    python scripts/seed_bulk_users.py --countries TW         # 只做台灣
    python scripts/seed_bulk_users.py --purge                # 清掉這批

與 `seed_demo_farmers.py` 的分工：那支是手寫的少量精緻資料（有商號簡介、
網址、四種位置公開程度），用來示範完整的個人檔案；這支是**批量填充**，
讓看板與清單頁看起來像有人在用。

## 價格怎麼決定

不是亂數。每個人的報價都**錨定在該作物近期的真實官方行情**上：

    盤商收購價  <  小農直售價  <  官方批發價  <  消費者意向價

這個順序是實際的產銷鏈。亂數只用在各自的浮動區間內，所以看板上的
數字彼此對得起來——不然 Demo 時一眼就會看出是假的。

## 為什麼要挑有行情的區域

小農被放在**該作物確實有官方行情的區域**，否則「梅山的高麗菜」點進去
沒有任何官方價可以對照，看板會是空的。區域清單是執行時從
`official_prices` 查出來的，不寫死。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_scope
from app.data.countries import default_currency, list_subdivisions
from app.models.catalog import Market, Product
from app.models.enums import LocationVisibility, QuoteSide, QuoteStatus, UserRole
from app.models.intent import PriceIntent, UserReputation
from app.models.price import OfficialPrice
from app.models.quote import Quote
from app.models.user import User

# 這批帳號的號碼前綴，方便 --purge 一次清掉，也不會撞到手寫的示範帳號
PHONE_PREFIX = {"TW": "+886955", "UG": "+256755"}

# 一個人掛幾筆報價
QUOTES_PER_FARMER = (2, 4)
QUOTES_PER_TRADER = (3, 6)

# 相對官方批發價的倍率區間
FARMER_SELL_RATIO = (0.75, 0.95)   # 產地直售，省掉中間層所以比批發便宜
TRADER_BUY_RATIO = (0.55, 0.75)    # 盤商收購，要留運銷與耗損的空間

# 區域 -> (緯度, 經度)。取各縣市 / 城鎮中心，是示範用的概略位置。
# 沒列到的區域會落在 None，那些使用者就不給座標（合法，位置是選填）。
REGION_COORDS: dict[str, tuple[float, float]] = {
    # 台灣（鍵是 markets.region 的寫法，用「台」不用「臺」）
    "台北市": (25.0330, 121.5654), "新北市": (25.0169, 121.4627),
    "桃園市": (24.9936, 121.3010), "台中市": (24.1477, 120.6736),
    "台南市": (22.9999, 120.2270), "高雄市": (22.6273, 120.3014),
    "宜蘭縣": (24.7021, 121.7378), "雲林縣": (23.7092, 120.4313),
    "嘉義市": (23.4801, 120.4491), "彰化縣": (24.0518, 120.5161),
    "南投縣": (23.9609, 120.9719), "台東縣": (22.7583, 121.1444),
    "花蓮縣": (23.9871, 121.6015),
    # 烏干達（markets.region 用的是縣名）
    "Kampala": (0.3476, 32.5825), "Iganga": (0.6093, 33.4686),
    "Mbale": (1.0827, 34.1750), "Mbarara": (-0.6072, 30.6545),
    "Lira": (2.2350, 32.9097), "Soroti": (1.7148, 33.6111),
    "Tororo": (0.6928, 34.1808), "Hoima": (1.4353, 31.3521),
    "Kabarole": (0.6710, 30.2748),
}

# markets.region（資料源自訂）-> ISO 3166-2 代碼。
# 使用者的 subdivision_code 要用 ISO，市場用自由文字，兩邊得對一次。
REGION_TO_ISO: dict[str, str] = {
    "台北市": "TW-TPE", "新北市": "TW-NWT", "桃園市": "TW-TAO",
    "台中市": "TW-TXG", "台南市": "TW-TNN", "高雄市": "TW-KHH",
    "宜蘭縣": "TW-ILA", "雲林縣": "TW-YUN", "嘉義市": "TW-CYI",
    "彰化縣": "TW-CHA", "南投縣": "TW-NAN", "台東縣": "TW-TTT",
    "花蓮縣": "TW-HUA",
    # 烏干達的一級行政區只有四個大區，縣名對到所屬大區
    "Kampala": "UG-C", "Iganga": "UG-E", "Mbale": "UG-E",
    "Mbarara": "UG-W", "Lira": "UG-N", "Soroti": "UG-E",
    "Tororo": "UG-E", "Hoima": "UG-W", "Kabarole": "UG-W",
}

SURNAMES_TW = "陳林黃張李王吳劉蔡楊許鄭謝洪郭邱曾廖賴徐周葉蘇莊呂江何蕭羅高"
GIVEN_TW = [
    "志明", "淑芬", "家豪", "美玲", "建宏", "雅婷", "俊傑", "怡君", "宗翰", "佳蓉",
    "明德", "秀英", "文彬", "麗華", "永昌", "春嬌", "國強", "月娥", "世傑", "惠美",
    "阿財", "金水", "阿珠", "水源", "進發", "秋月", "萬來", "玉蘭", "添丁", "罔市",
]
FARM_SUFFIX_TW = ["農園", "果園", "農場", "產銷班", "青果行", "菜園", "合作社", "自然農法"]
TRADER_SUFFIX_TW = ["青果行", "農產貿易", "蔬果批發", "物產行", "運銷合作社", "food supply"]

FIRST_UG = [
    "Sarah", "Joseph", "Grace", "Robert", "Betty", "Daniel", "Alice", "Moses",
    "Esther", "Patrick", "Rose", "Samuel", "Harriet", "Emmanuel", "Justine",
    "Peter", "Florence", "David", "Agnes", "Isaac", "Prossy", "Charles",
]
LAST_UG = [
    "Nakato", "Wanyama", "Akello", "Tumusiime", "Nabirye", "Mugisha", "Okello",
    "Namutebi", "Kigongo", "Achieng", "Byaruhanga", "Nakimuli", "Otim",
    "Ssempala", "Auma", "Kyomuhendo", "Wasswa", "Nalubega", "Odong", "Katusiime",
]
FARM_SUFFIX_UG = ["Farm", "Gardens", "Produce", "Farmers Group", "Estate", "Growers"]
TRADER_SUFFIX_UG = ["Traders", "Produce Dealers", "Agro Supplies", "Commodities", "Wholesalers"]


@dataclass
class RegionInfo:
    country_code: str
    region: str
    product_ids: list[tuple[str, Decimal, str, str]]  # (product_id, 參考價, currency, unit)


async def load_regions(
    session: AsyncSession, countries: list[str], days: int = 30
) -> list[RegionInfo]:
    """找出有官方行情的區域，以及各區域有哪些作物與參考價。

    參考價取近 `days` 天該區域該作物的平均價——報價要錨在這上面。
    """
    since = datetime.now(UTC).date() - timedelta(days=days)
    rows = await session.execute(
        select(
            Market.country_code,
            Market.region,
            OfficialPrice.product_id,
            func.avg(OfficialPrice.price_avg).label("ref"),
            func.min(OfficialPrice.currency).label("currency"),
            func.min(OfficialPrice.unit).label("unit"),
        )
        .join(Market, Market.id == OfficialPrice.market_id)
        .where(
            Market.country_code.in_([c.upper() for c in countries]),
            Market.region.is_not(None),
            OfficialPrice.product_id.is_not(None),
            OfficialPrice.price_avg.is_not(None),
            OfficialPrice.trade_date >= since,
        )
        .group_by(Market.country_code, Market.region, OfficialPrice.product_id)
        .having(func.avg(OfficialPrice.price_avg) > 0)
    )

    grouped: dict[tuple[str, str], list] = {}
    for cc, region, pid, ref, currency, unit in rows:
        grouped.setdefault((cc, region), []).append(
            (pid, Decimal(str(ref)), currency, unit)
        )
    # 作物太少的區域拿來配置小農沒意義
    return [
        RegionInfo(cc, region, prods)
        for (cc, region), prods in grouped.items()
        if len(prods) >= 3
    ]


def _name(country: str, is_trader: bool) -> tuple[str, str]:
    """回 (顯示名, 商號)。"""
    if country == "TW":
        person = random.choice(SURNAMES_TW) + random.choice(GIVEN_TW)
        suffix = random.choice(TRADER_SUFFIX_TW if is_trader else FARM_SUFFIX_TW)
        return person, f"{person[0]}記{suffix}"
    person = f"{random.choice(FIRST_UG)} {random.choice(LAST_UG)}"
    suffix = random.choice(TRADER_SUFFIX_UG if is_trader else FARM_SUFFIX_UG)
    return person, f"{person.split()[-1]} {suffix}"


def _bio(country: str, region: str, is_trader: bool) -> str:
    if country == "TW":
        return (
            f"{region}一帶的蔬果收購，可整車配送，量大另議。"
            if is_trader
            else f"在{region}種了十幾年，自產自銷，當日採收當日出貨。"
        )
    return (
        f"Buying produce across {region} and nearby districts. Bulk lots available."
        if is_trader
        else f"Smallholder farm near {region}. Harvest to order, can deliver to market."
    )


async def seed(
    farmers: int, traders: int, countries: list[str], seed_value: int
) -> dict[str, int]:
    random.seed(seed_value)
    created = updated = quotes_made = 0

    async with session_scope() as session:
        regions = await load_regions(session, countries)
        if not regions:
            raise SystemExit(
                f"{countries} 沒有任何有官方行情的區域，先跑一次 run_ingest.py"
            )
        print(f"可用區域 {len(regions)} 個："
              f"{', '.join(sorted({f'{r.country_code}/{r.region}' for r in regions}))}\n")

        products = {
            str(p.id): p
            for p in (await session.execute(select(Product))).scalars()
        }

        plan = [(True, i) for i in range(traders)] + [(False, i) for i in range(farmers)]
        for is_trader, index in plan:
            region_info = random.choice(regions)
            cc = region_info.country_code
            prefix = PHONE_PREFIX.get(cc, "+886955")
            # 號碼由序號決定，重跑同一個 seed 會更新而不是重複建立
            phone = f"{prefix}{'9' if is_trader else '1'}{index:04d}"

            user = await session.scalar(select(User).where(User.phone == phone))
            is_new = user is None
            if is_new:
                user = User(phone=phone, role=UserRole.TRADER if is_trader else UserRole.FARMER)
                session.add(user)

            display, business = _name(cc, is_trader)
            coords = REGION_COORDS.get(region_info.region)
            user.display_name = display
            user.business_name = business
            user.bio = _bio(cc, region_info.region, is_trader)
            user.country_code = cc
            user.locale = "zh-Hant" if cc == "TW" else "en"
            user.preferred_currency = default_currency(cc)
            user.subdivision_code = REGION_TO_ISO.get(region_info.region)
            user.locality = region_info.region
            user.latitude, user.longitude = coords if coords else (None, None)
            user.location_visibility = random.choice(
                [LocationVisibility.REGION, LocationVisibility.APPROXIMATE,
                 LocationVisibility.EXACT]
            )
            user.location_updated_at = datetime.now(UTC)
            user.phone_verified_at = user.phone_verified_at or datetime.now(UTC)
            user.contact_phone_public = random.random() < 0.7
            await session.flush()

            # 每個帳號都給一份初始信譽，不然聚合時才建會拖慢第一次請求
            if not await session.get(UserReputation, user.id):
                session.add(UserReputation(user_id=user.id))

            # 舊報價先下架再重建，避免重跑累積
            await session.execute(
                delete(Quote).where(Quote.user_id == user.id)
            )

            low, high = QUOTES_PER_TRADER if is_trader else QUOTES_PER_FARMER
            picks = random.sample(
                region_info.product_ids, min(random.randint(low, high), len(region_info.product_ids))
            )
            for pid, reference, currency, unit in picks:
                product = products.get(str(pid))
                if product is None:
                    continue
                ratio = random.uniform(*(TRADER_BUY_RATIO if is_trader else FARMER_SELL_RATIO))
                price = (reference * Decimal(str(ratio))).quantize(Decimal("0.01"))
                if price <= 0:
                    continue
                now = datetime.now(UTC)
                session.add(
                    Quote(
                        user_id=user.id,
                        product_id=product.id,
                        side=QuoteSide.BUY if is_trader else QuoteSide.SELL,
                        role_snapshot=user.role,
                        status=QuoteStatus.ACTIVE,
                        price=price,
                        currency=currency,
                        unit=unit,
                        quantity=Decimal(random.randrange(50, 5000)),
                        min_order=Decimal(random.choice([0, 10, 20, 50, 100])) or None,
                        country_code=cc,
                        region=region_info.region,
                        contact_phone_public=user.contact_phone_public,
                        valid_from=now,
                        # 錯開到期時間，清單才不會整批同時消失
                        valid_until=now + timedelta(hours=random.randint(24, 24 * 14)),
                    )
                )
                quotes_made += 1

            created += is_new
            updated += not is_new

    return {"created": created, "updated": updated, "quotes": quotes_made}


async def purge() -> dict[str, int]:
    async with session_scope() as session:
        rows = await session.execute(
            select(User).where(
                func.coalesce(User.phone, "").startswith(tuple(PHONE_PREFIX.values())[0])
                | User.phone.startswith(tuple(PHONE_PREFIX.values())[1])
            )
        )
        users = list(rows.scalars())
        for u in users:
            await session.execute(delete(Quote).where(Quote.user_id == u.id))
            await session.execute(delete(PriceIntent).where(PriceIntent.user_id == u.id))
            await session.execute(delete(UserReputation).where(UserReputation.user_id == u.id))
            await session.delete(u)
        return {"deleted": len(users)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--farmers", type=int, default=30)
    parser.add_argument("--traders", type=int, default=10)
    parser.add_argument(
        "--countries", default="TW,UG", help="逗號分隔，只會用有官方行情的區域"
    )
    parser.add_argument("--seed", type=int, default=20260920, help="固定亂數種子，可重現")
    parser.add_argument("--purge", action="store_true", help="刪掉這批帳號與其報價")
    args = parser.parse_args()

    if args.purge:
        result = asyncio.run(purge())
        print(f"已刪除 {result['deleted']} 個帳號")
        return 0

    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    result = asyncio.run(seed(args.farmers, args.traders, countries, args.seed))
    print(
        f"新增 {result['created']} 個帳號，更新 {result['updated']} 個，"
        f"建立 {result['quotes']} 筆報價"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
