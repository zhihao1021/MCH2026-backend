"""建立一批常見的標準品項。

    python scripts/seed_products.py [--reset-popularity]

可重複執行：已存在的 slug 會跳過，只補上缺少的名稱與別名。
品項名稱同時給 zh-Hant / ja / en，讓多國來源都能對照得上，
別名（is_primary=False）則是給搜尋用的。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import session_scope
from app.models.catalog import Product, ProductName
from app.models.enums import ProductCategory

# (slug, 分類, 單位, 熱門度, [(locale, 名稱, 是否為主要名稱)])
SEED: list[tuple[str, ProductCategory, str, int, list[tuple[str, str, bool]]]] = [
    ("cabbage", ProductCategory.VEGETABLE, "kg", 100, [
        ("zh-Hant", "高麗菜", True), ("zh-Hant", "甘藍", False), ("zh-Hant", "包心菜", False),
        ("ja", "キャベツ", True),
        ("en", "Cabbage", True),
    ]),
    ("napa-cabbage", ProductCategory.VEGETABLE, "kg", 85, [
        ("zh-Hant", "大白菜", True), ("zh-Hant", "結球白菜", False),
        ("ja", "白菜", True),
        ("en", "Napa Cabbage", True),
    ]),
    ("white-radish", ProductCategory.VEGETABLE, "kg", 80, [
        ("zh-Hant", "白蘿蔔", True), ("zh-Hant", "菜頭", False),
        ("ja", "大根", True),
        ("en", "Daikon Radish", True),
    ]),
    ("tomato", ProductCategory.VEGETABLE, "kg", 95, [
        ("zh-Hant", "番茄", True), ("zh-Hant", "牛番茄", False), ("zh-Hant", "西紅柿", False),
        ("ja", "トマト", True),
        ("en", "Tomato", True),
    ]),
    ("spinach", ProductCategory.VEGETABLE, "kg", 70, [
        ("zh-Hant", "菠菜", True),
        ("ja", "ほうれん草", True),
        ("en", "Spinach", True),
    ]),
    ("carrot", ProductCategory.VEGETABLE, "kg", 78, [
        ("zh-Hant", "紅蘿蔔", True), ("zh-Hant", "胡蘿蔔", False),
        ("ja", "にんじん", True),
        ("en", "Carrot", True),
    ]),
    ("onion", ProductCategory.VEGETABLE, "kg", 88, [
        ("zh-Hant", "洋蔥", True),
        ("ja", "玉ねぎ", True),
        ("en", "Onion", True),
    ]),
    ("potato", ProductCategory.VEGETABLE, "kg", 82, [
        ("zh-Hant", "馬鈴薯", True), ("zh-Hant", "洋芋", False),
        ("ja", "じゃがいも", True),
        ("en", "Potato", True),
    ]),
    ("cucumber", ProductCategory.VEGETABLE, "kg", 72, [
        ("zh-Hant", "小黃瓜", True), ("zh-Hant", "花胡瓜", False),
        ("ja", "きゅうり", True),
        ("en", "Cucumber", True),
    ]),
    ("green-onion", ProductCategory.VEGETABLE, "kg", 90, [
        ("zh-Hant", "青蔥", True), ("zh-Hant", "蔥", False),
        ("ja", "ねぎ", True),
        ("en", "Green Onion", True),
    ]),
    ("banana", ProductCategory.FRUIT, "kg", 92, [
        ("zh-Hant", "香蕉", True), ("zh-Hant", "芎蕉", False),
        ("ja", "バナナ", True),
        ("en", "Banana", True),
    ]),
    ("pineapple", ProductCategory.FRUIT, "kg", 86, [
        ("zh-Hant", "鳳梨", True), ("zh-Hant", "菠蘿", False),
        ("ja", "パイナップル", True),
        ("en", "Pineapple", True),
    ]),
    ("mango", ProductCategory.FRUIT, "kg", 84, [
        ("zh-Hant", "芒果", True), ("zh-Hant", "檨仔", False),
        ("ja", "マンゴー", True),
        ("en", "Mango", True),
    ]),
    ("watermelon", ProductCategory.FRUIT, "kg", 76, [
        ("zh-Hant", "西瓜", True),
        ("ja", "スイカ", True),
        ("en", "Watermelon", True),
    ]),
    ("guava", ProductCategory.FRUIT, "kg", 74, [
        ("zh-Hant", "芭樂", True), ("zh-Hant", "番石榴", False),
        ("ja", "グアバ", True),
        ("en", "Guava", True),
    ]),
    ("wax-apple", ProductCategory.FRUIT, "kg", 68, [
        ("zh-Hant", "蓮霧", True),
        ("ja", "レンブ", True),
        ("en", "Wax Apple", True),
    ]),
    ("papaya", ProductCategory.FRUIT, "kg", 66, [
        ("zh-Hant", "木瓜", True),
        ("ja", "パパイヤ", True),
        ("en", "Papaya", True),
    ]),
    ("apple", ProductCategory.FRUIT, "kg", 80, [
        ("zh-Hant", "蘋果", True),
        ("ja", "りんご", True),
        ("en", "Apple", True),
    ]),
    ("rice", ProductCategory.GRAIN, "kg", 94, [
        ("zh-Hant", "白米", True), ("zh-Hant", "稻米", False),
        ("ja", "米", True),
        ("en", "Rice", True),
    ]),
    ("egg", ProductCategory.LIVESTOCK, "kg", 96, [
        ("zh-Hant", "雞蛋", True), ("zh-Hant", "蛋", False),
        ("ja", "鶏卵", True),
        ("en", "Chicken Egg", True),
    ]),
]


async def seed(reset_popularity: bool) -> tuple[int, int, int]:
    created = updated = names_added = 0

    async with session_scope() as session:
        for slug, category, unit, popularity, names in SEED:
            product = await session.scalar(
                select(Product).options(selectinload(Product.names)).where(Product.slug == slug)
            )

            if product is None:
                # 名稱直接掛在關聯上一起建，不要先 flush 再讀 product.names——
                # 那會在新物件上觸發 lazy load，非同步 session 下會炸 MissingGreenlet
                product = Product(
                    slug=slug,
                    category=category,
                    default_unit=unit,
                    popularity=popularity,
                    names=[
                        ProductName(locale=locale, name=name, is_primary=is_primary)
                        for locale, name, is_primary in names
                    ],
                )
                session.add(product)
                created += 1
                names_added += len(names)
                continue

            if reset_popularity and product.popularity != popularity:
                product.popularity = popularity
                updated += 1

            # 既有品項是用 selectinload 讀進來的，這裡存取 names 不會再打 DB
            existing = {(n.locale, n.name) for n in product.names}
            for locale, name, is_primary in names:
                if (locale, name) in existing:
                    continue
                product.names.append(
                    ProductName(locale=locale, name=name, is_primary=is_primary)
                )
                names_added += 1

    return created, updated, names_added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset-popularity",
        action="store_true",
        help="把既有品項的熱門度覆寫回種子值",
    )
    args = parser.parse_args()

    created, updated, names = asyncio.run(seed(args.reset_popularity))
    print(f"新增品項 {created} 筆，更新 {updated} 筆，補上名稱 / 別名 {names} 筆")
    print(f"種子清單共 {len(SEED)} 個品項")
    return 0


if __name__ == "__main__":
    sys.exit(main())
