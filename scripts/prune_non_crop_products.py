"""移除非農作物的品項（畜產、漁產）。

    python scripts/prune_non_crop_products.py            # 只列出，不動資料
    python scripts/prune_non_crop_products.py --apply    # 實際刪除

這個 App 只處理農作物（蔬菜 / 水果 / 花卉 / 穀物）。畜產與漁產的產銷結構、
計價單位與分級方式跟作物差太多，硬塞進同一套品項模型會讓對照與單位換算
沒辦法維護，所以直接排除。`scripts/seed_products.py` 也已經不再收錄它們。

花卉**不會**被刪：它是園藝作物，MOA 的農產品交易行情本來就有，
只是 popularity 壓低避免洗版。

刪除時的連帶影響（由 FK 決定，見 app/models）：
- `product_names`            → 一併刪除（CASCADE）
- `official_prices`          → `product_id` 設為 NULL，價格資料本身保留
- `product_source_mappings`  → `product_id` 設為 NULL，對照變回「待對應」
- `quotes`                   → **一併刪除**（CASCADE）

因為報價會被連帶刪掉，只要偵測到有報價就會要求 `--force` 再確認一次。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.database import session_scope
from app.models.catalog import Product, ProductSourceMapping
from app.models.enums import ProductCategory
from app.models.price import OfficialPrice
from app.models.quote import Quote

# 不屬於農作物的分類。enum 值保留著，將來要做再說。
NON_CROP = (ProductCategory.LIVESTOCK, ProductCategory.FISHERY)


async def run(apply: bool, force: bool) -> int:
    async with session_scope() as session:
        products = list((await session.execute(
            select(Product)
            .options(selectinload(Product.names))
            .where(Product.category.in_(NON_CROP))
            .order_by(Product.category, Product.slug)
        )).scalars())

        if not products:
            print("沒有非農作物品項，不需要處理。")
            return 0

        total_quotes = 0
        print(f"找到 {len(products)} 個非農作物品項：\n")
        for p in products:
            prices = await session.scalar(
                select(func.count()).select_from(OfficialPrice)
                .where(OfficialPrice.product_id == p.id)
            ) or 0
            quotes = await session.scalar(
                select(func.count()).select_from(Quote).where(Quote.product_id == p.id)
            ) or 0
            maps = await session.scalar(
                select(func.count()).select_from(ProductSourceMapping)
                .where(ProductSourceMapping.product_id == p.id)
            ) or 0
            total_quotes += quotes

            names = " / ".join(n.name for n in p.names if n.is_primary)
            print(f"  {p.category.value:10} {p.slug:20} {names}")
            print(f"  {'':10} 官方價 {prices} 筆（會保留，product_id 轉為 NULL）"
                  f"、代碼對照 {maps} 筆（會變回待對應）"
                  f"、報價 {quotes} 筆" + ("（會被刪除）" if quotes else ""))

        if not apply:
            print(f"\n這是預覽。要實際刪除請加 --apply")
            return 0

        if total_quotes and not force:
            print(f"\n中止：這些品項底下還有 {total_quotes} 筆報價，刪除品項會連帶刪掉它們。")
            print("確定要刪的話請加 --force。")
            return 1

        for p in products:
            await session.delete(p)
        print(f"\n已刪除 {len(products)} 個品項"
              + (f"，連帶刪除 {total_quotes} 筆報價" if total_quotes else ""))

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="實際刪除（預設只預覽）")
    parser.add_argument("--force", action="store_true", help="即使有報價也照刪")
    args = parser.parse_args()
    return asyncio.run(run(args.apply, args.force))


if __name__ == "__main__":
    sys.exit(main())
