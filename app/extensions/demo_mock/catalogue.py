"""印度示範資料的作物與市場目錄。

**這是單一事實來源。** extension（產生價格）與 `scripts/seed_india_demo.py`
（建立品項與產季）都從這裡讀，兩邊才不會各自維護一份而慢慢走鐘——
市場代碼、作物代碼、產季月份只要在這裡改一次。

資料取自印度實際的 APMC mandi 與各作物的一般產季。價格是 ₹/kg 的批發
行情量級（印度 mandi 官方報價其實以 quintal 為單位，但存成 ₹/quintal
會跟其他來源的 ₹/kg 差 100 倍，在同一個時間序列上並排顯示會嚴重誤導，
所以這裡統一換算成 kg）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import ProductCategory

__all__ = ["MARKETS", "PRODUCTS", "SEASON_NAMES", "DemoMarket", "DemoProduct", "DemoSeason"]


@dataclass(frozen=True)
class DemoMarket:
    code: str
    name: str
    region: str            # 邦名，對應 markets.region
    subdivision: str       # ISO 3166-2
    latitude: float
    longitude: float
    # 這個市場所在地的雨季月份。印度不是全國同時進入雨季：
    # 西南季風 6–9 月影響大部分地區，但坦米爾那都靠的是 10–12 月的
    # 東北季風。蔬菜在雨季供應受阻、價格上揚，時點錯了整條曲線就不像
    monsoon_months: tuple[int, ...]


@dataclass(frozen=True)
class DemoSeason:
    slug: str
    start_month: int
    end_month: int
    # 主產季：到貨量最大、價格最低的那一段
    is_peak: bool = False
    note: str | None = None


@dataclass(frozen=True)
class DemoProduct:
    code: str                      # 來源代碼（product_source_mappings.external_code）
    # 平台品項 slug。**刻意對到既有品項**：平台的 `rice` / `peanut` /
    # `eggplant` 已經有台灣與烏干達的資料，印度的稻穀、花生、茄子要接到
    # 同一條時間序列上才有跨國比價的意義，不能另開 paddy-rice、brinjal
    slug: str
    name_en: str
    name_hi: str
    name_zh: str
    category: ProductCategory
    base_price: int                # ₹/kg，盛產期的基準批發價
    markets: tuple[str, ...]       # 有在交易的市場代碼
    seasons: tuple[DemoSeason, ...]
    # 雨季會不會明顯推高價格。葉菜、番茄這類不耐儲運的會，穀物豆類不會
    perishable: bool = False
    # 全年供應（有冷藏或分批產區）。False 代表產季以外就沒有到貨
    year_round: bool = False


# 季節 slug 的顯示名。印度的農季與園藝季是兩套詞彙，不該硬併成一套——
# 大田作物走 kharif/rabi/zaid，蔬果則按上市期分
SEASON_NAMES: dict[str, str] = {
    "kharif": "Kharif (खरीफ़)",
    "rabi": "Rabi (रबी)",
    "zaid": "Zaid (ज़ायद)",
    "summer_fruit": "Summer fruit (ग्रीष्म फल)",
    "winter_fruit": "Winter fruit (शीत फल)",
    "winter_veg": "Winter vegetable (शीत सब्ज़ी)",
    "year_round": "Year-round (सालभर)",
}


MARKETS: tuple[DemoMarket, ...] = (
    DemoMarket("AZDP", "Azadpur Mandi", "Delhi", "IN-DL", 28.7167, 77.1756, (7, 8, 9)),
    DemoMarket("LSGN", "Lasalgaon APMC", "Maharashtra", "IN-MH", 20.1436, 74.2380, (6, 7, 8, 9)),
    DemoMarket("KOYB", "Koyambedu Market", "Tamil Nadu", "IN-TN", 13.0694, 80.1948, (10, 11, 12)),
    DemoMarket("BNGL", "Yeshwanthpur APMC", "Karnataka", "IN-KA", 13.0230, 77.5390, (6, 7, 8, 9)),
    DemoMarket("KOLK", "Koley Market", "West Bengal", "IN-WB", 22.5675, 88.3720, (6, 7, 8, 9)),
    DemoMarket("LDHN", "Ludhiana Grain Market", "Punjab", "IN-PB", 30.9010, 75.8573, (7, 8, 9)),
    DemoMarket("INDR", "Chhawni Mandi", "Madhya Pradesh", "IN-MP", 22.7196, 75.8577, (7, 8, 9)),
    DemoMarket("AHMD", "Jamalpur Market", "Gujarat", "IN-GJ", 23.0100, 72.5800, (6, 7, 8, 9)),
    DemoMarket("LCKN", "Navin Galla Mandi", "Uttar Pradesh", "IN-UP", 26.8467, 80.9462, (7, 8, 9)),
    DemoMarket("JAIP", "Muhana Mandi", "Rajasthan", "IN-RJ", 26.8100, 75.7600, (7, 8, 9)),
)


PRODUCTS: tuple[DemoProduct, ...] = (
    # ---- 大田作物：Kharif（西南季風播種，秋收） ----------------------
    DemoProduct(
        "IN-PADDY", "rice", "Paddy (Rice)", "धान", "稻穀",
        ProductCategory.GRAIN, 22, ("LDHN", "KOLK", "LCKN", "KOYB"),
        (
            DemoSeason("kharif", 9, 11, is_peak=True, note="西南季風稻，全國最大一期"),
            DemoSeason("rabi", 3, 5, note="Boro 稻，主要在西孟加拉與南印"),
        ),
        year_round=True,
    ),
    DemoProduct(
        "IN-MAIZE", "maize", "Maize", "मक्का", "玉米",
        ProductCategory.GRAIN, 20, ("INDR", "BNGL", "LCKN"),
        (DemoSeason("kharif", 9, 11, is_peak=True), DemoSeason("rabi", 3, 4)),
        year_round=True,
    ),
    DemoProduct(
        "IN-SOYBEAN", "soybean", "Soybean", "सोयाबीन", "大豆",
        ProductCategory.GRAIN, 46, ("INDR", "LSGN"),
        (DemoSeason("kharif", 10, 12, is_peak=True, note="中央邦為主產區"),),
    ),
    DemoProduct(
        "IN-GROUNDNUT", "peanut", "Groundnut", "मूंगफली", "花生",
        ProductCategory.GRAIN, 62, ("AHMD", "JAIP", "BNGL"),
        (DemoSeason("kharif", 10, 12, is_peak=True), DemoSeason("rabi", 3, 4)),
    ),
    DemoProduct(
        "IN-TUR", "pigeon-pea", "Tur / Arhar Dal", "अरहर दाल", "木豆",
        ProductCategory.GRAIN, 98, ("LSGN", "BNGL", "INDR"),
        (DemoSeason("kharif", 12, 2, is_peak=True, note="採收跨年，12 月到隔年 2 月"),),
    ),

    # ---- 大田作物：Rabi（冬播，春收） --------------------------------
    DemoProduct(
        "IN-WHEAT", "wheat", "Wheat", "गेहूँ", "小麥",
        ProductCategory.GRAIN, 25, ("LDHN", "INDR", "LCKN", "JAIP"),
        (DemoSeason("rabi", 3, 5, is_peak=True, note="旁遮普與中央邦集中於 4 月上市"),),
        year_round=True,
    ),
    DemoProduct(
        "IN-MUSTARD", "mustard-seed", "Mustard Seed", "सरसों", "芥菜籽",
        ProductCategory.GRAIN, 56, ("JAIP", "LCKN", "AHMD"),
        (DemoSeason("rabi", 3, 4, is_peak=True, note="拉賈斯坦為最大產區"),),
    ),
    DemoProduct(
        "IN-GRAM", "chickpea", "Chana (Bengal Gram)", "चना", "鷹嘴豆",
        ProductCategory.GRAIN, 60, ("INDR", "JAIP", "LCKN"),
        (DemoSeason("rabi", 3, 5, is_peak=True),),
        year_round=True,
    ),
    DemoProduct(
        "IN-ONION", "onion", "Onion", "प्याज़", "洋蔥",
        ProductCategory.VEGETABLE, 28, ("LSGN", "AHMD", "BNGL", "AZDP"),
        (
            DemoSeason("rabi", 3, 6, is_peak=True, note="Lasalgaon 的 Rabi 洋蔥可儲存數月"),
            DemoSeason("kharif", 10, 12, note="雨季洋蔥不耐儲，價格波動最劇"),
        ),
        year_round=True,
    ),
    DemoProduct(
        "IN-POTATO", "potato", "Potato", "आलू", "馬鈴薯",
        ProductCategory.VEGETABLE, 18, ("LCKN", "KOLK", "LDHN", "AZDP"),
        (DemoSeason("rabi", 1, 3, is_peak=True, note="北方邦與西孟加拉，冷藏後全年供應"),),
        year_round=True,
    ),

    # ---- Zaid（春夏短期作） ------------------------------------------
    DemoProduct(
        "IN-WATERMELON", "watermelon", "Watermelon", "तरबूज़", "西瓜",
        ProductCategory.FRUIT, 14, ("JAIP", "LCKN", "LSGN"),
        (DemoSeason("zaid", 3, 6, is_peak=True),),
    ),
    DemoProduct(
        "IN-MUSKMELON", "muskmelon", "Muskmelon", "खरबूजा", "哈密瓜",
        ProductCategory.FRUIT, 24, ("JAIP", "LCKN"),
        (DemoSeason("zaid", 4, 6, is_peak=True),),
    ),
    DemoProduct(
        "IN-CUCUMBER", "cucumber", "Cucumber", "खीरा", "小黃瓜",
        ProductCategory.VEGETABLE, 20, ("AZDP", "LCKN", "BNGL"),
        (DemoSeason("zaid", 3, 6, is_peak=True), DemoSeason("year_round", 1, 12)),
        perishable=True,
        year_round=True,
    ),

    # ---- 冬季蔬菜 -----------------------------------------------------
    DemoProduct(
        "IN-CAULIFLOWER", "cauliflower", "Cauliflower", "फूलगोभी", "花椰菜",
        ProductCategory.VEGETABLE, 21, ("KOLK", "LCKN", "AZDP"),
        (DemoSeason("winter_veg", 11, 2, is_peak=True),),
        perishable=True,
    ),
    DemoProduct(
        "IN-CABBAGE", "cabbage", "Cabbage", "पत्तागोभी", "高麗菜",
        ProductCategory.VEGETABLE, 16, ("KOLK", "BNGL", "AZDP"),
        (DemoSeason("winter_veg", 11, 2, is_peak=True),),
        perishable=True,
    ),
    DemoProduct(
        "IN-PEAS", "pea", "Green Peas", "मटर", "豌豆",
        ProductCategory.VEGETABLE, 45, ("LCKN", "KOLK", "JAIP"),
        (DemoSeason("winter_veg", 12, 2, is_peak=True),),
        perishable=True,
    ),

    # ---- 全年蔬菜（雨季漲價最明顯） ------------------------------------
    DemoProduct(
        "IN-TOMATO", "tomato", "Tomato", "टमाटर", "番茄",
        ProductCategory.VEGETABLE, 26, ("BNGL", "LSGN", "KOYB", "AZDP"),
        (
            DemoSeason("year_round", 1, 12),
            DemoSeason("winter_veg", 12, 3, is_peak=True, note="冬季到貨量最大、價格最低"),
        ),
        perishable=True,
        year_round=True,
    ),
    DemoProduct(
        "IN-BRINJAL", "eggplant", "Brinjal (Eggplant)", "बैंगन", "茄子",
        ProductCategory.VEGETABLE, 23, ("KOLK", "BNGL", "KOYB"),
        (DemoSeason("year_round", 1, 12), DemoSeason("winter_veg", 11, 2, is_peak=True)),
        perishable=True,
        year_round=True,
    ),
    DemoProduct(
        "IN-OKRA", "okra", "Okra (Bhindi)", "भिंडी", "秋葵",
        ProductCategory.VEGETABLE, 32, ("LCKN", "KOLK", "AHMD"),
        (DemoSeason("year_round", 1, 12), DemoSeason("zaid", 4, 7, is_peak=True)),
        perishable=True,
        year_round=True,
    ),

    # ---- 水果 ---------------------------------------------------------
    DemoProduct(
        "IN-MANGO", "mango", "Mango", "आम", "芒果",
        ProductCategory.FRUIT, 65, ("LCKN", "LSGN", "BNGL", "AHMD"),
        (DemoSeason("summer_fruit", 4, 7, is_peak=True, note="Alphonso 與 Dasheri 產期"),),
    ),
    DemoProduct(
        "IN-BANANA", "banana", "Banana", "केला", "香蕉",
        ProductCategory.FRUIT, 34, ("KOYB", "LSGN", "AHMD"),
        (DemoSeason("year_round", 1, 12, is_peak=True),),
        year_round=True,
    ),
    DemoProduct(
        "IN-GRAPES", "grape", "Grapes", "अंगूर", "葡萄",
        ProductCategory.FRUIT, 72, ("LSGN", "AHMD", "BNGL"),
        (DemoSeason("winter_fruit", 1, 4, is_peak=True, note="Nashik 產區，1–4 月上市"),),
    ),
)


def market_by_code(code: str) -> DemoMarket:
    for m in MARKETS:
        if m.code == code:
            return m
    raise KeyError(code)
