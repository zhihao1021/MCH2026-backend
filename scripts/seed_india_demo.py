"""把 demo_mock 的印度作物接到平台的標準品項上。

    python scripts/seed_india_demo.py            # 看看會做什麼，不寫入
    python scripts/seed_india_demo.py --apply

做兩件事：

1. **補齊缺少的品項**。目錄裡多數作物平台已經有了（`rice`、`onion`、
   `eggplant`…），只有印度特有的幾樣沒有，例如 Tur dal、Chana。
2. **建立代碼對照**。`IN-ONION` → `onion`，讓 ingest 進來的價格認得出
   是哪個作物；沒有這一步，價格雖然入庫了但 `product_id` 是 NULL，
   App 端整批看不到。

跟 `automap_products.py` 的差別：那支是用**名稱**去猜，這支是照
`catalogue.py` 宣告的 slug **明確指定**。目錄是作者寫死的事實，
不需要猜，也就不會有 Groundnut / 落花生 這種同物異名的誤判。

可以重複執行；已存在的品項與已確認的對照都不會被覆寫。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.logging import setup_logging
from app.extensions.demo_mock.catalogue import PRODUCTS, DemoProduct
from app.models.catalog import DataSource, Product, ProductName, ProductSourceMapping

SOURCE_KEY = "demo_mock"

# 品項名稱的語系。印地語用 hi，讓印度使用者看到母語名稱
LOCALES = ("en", "hi", "zh-Hant")


def _names_of(p: DemoProduct) -> list[tuple[str, str]]:
    return [("en", p.name_en), ("hi", p.name_hi), ("zh-Hant", p.name_zh)]


async def ensure_products(session: AsyncSession, apply: bool) -> dict[str, Product]:
    """確保目錄裡每個 slug 都有對應的 Product，回 {slug: Product}。"""
    slugs = {p.slug for p in PRODUCTS}
    existing = {
        p.slug: p
        for p in (await session.execute(select(Product).where(Product.slug.in_(slugs)))).scalars()
    }

    created: list[str] = []
    for item in PRODUCTS:
        if item.slug in existing:
            continue
        created.append(item.slug)
        if not apply:
            continue
        # names 一次建好再 add，避免 flush 後再讀屬性觸發 MissingGreenlet
        product = Product(
            slug=item.slug,
            category=item.category,
            default_unit="kg",
            names=[
                ProductName(locale=loc, name=name, is_primary=True)
                for loc, name in _names_of(item)
            ],
        )
        session.add(product)
        existing[item.slug] = product

    print(f"品項：已存在 {len(slugs) - len(created)} 個，需新增 {len(created)} 個")
    for slug in created:
        print(f"  + {slug}")
    return existing


async def link_mappings(
    session: AsyncSession, products: dict[str, Product], apply: bool
) -> None:
    """把 demo_mock 的來源代碼指到正確的品項。"""
    source = (
        await session.execute(select(DataSource).where(DataSource.key == SOURCE_KEY))
    ).scalar_one_or_none()
    if source is None:
        print(f"找不到資料來源 {SOURCE_KEY}——請先跑一次 ingest 讓它註冊", file=sys.stderr)
        return

    rows = {
        m.external_code: m
        for m in (
            await session.execute(
                select(ProductSourceMapping).where(ProductSourceMapping.source_id == source.id)
            )
        ).scalars()
    }

    linked = skipped = missing = 0
    for item in PRODUCTS:
        mapping = rows.get(item.code)
        if mapping is None:
            # 這個作物這次的抓取區間內不在產季，還沒產生過價格
            missing += 1
            continue
        product = products.get(item.slug)
        if product is None:
            continue
        if mapping.product_id is not None and mapping.is_confirmed:
            skipped += 1
            continue
        linked += 1
        if apply:
            mapping.product_id = product.id
            mapping.external_name = item.name_en
            mapping.source_unit = "kg"
            mapping.unit_factor = 1.0
            # 目錄是作者寫死的，不是猜的，所以直接標成已確認，
            # 免得日後 automap 用名稱把它蓋掉
            mapping.is_confirmed = True

    print(f"對照：可連結 {linked} 筆，已確認略過 {skipped} 筆，尚無價格 {missing} 筆")


async def resync_prices(session: AsyncSession) -> int:
    """把已入庫價格的 product_id 依對照表重算。

    **少了這步整件事就白做。** ingest 當下對照還不存在，那批價格的
    `product_id` 全是 NULL；之後補上對照並不會自動回填，App 端依然看不到。

    寫成冪等的整批更新（`IS DISTINCT FROM` 只碰真的不一樣的列），
    重複執行安全。這也是為什麼不能在 ingest 進行中跑——正在跑的 loader
    手上是舊的對照快取，會把剛回填的值又覆寫回 NULL。
    """
    result = await session.execute(
        text(
            """
            UPDATE official_prices AS op
               SET product_id = psm.product_id
              FROM product_source_mappings AS psm
              JOIN data_sources AS ds ON ds.id = psm.source_id
             WHERE ds.key = :key
               AND op.mapping_id = psm.id
               AND op.product_id IS DISTINCT FROM psm.product_id
            """
        ),
        {"key": SOURCE_KEY},
    )
    return result.rowcount or 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="實際寫入資料庫")
    args = parser.parse_args()

    setup_logging()
    async with SessionLocal() as session:
        products = await ensure_products(session, args.apply)
        if args.apply:
            # 先 flush 讓新品項拿到 id，對照才連得上
            await session.flush()
        await link_mappings(session, products, args.apply)
        if args.apply:
            await session.flush()
            n = await resync_prices(session)
            print(f"價格回填：{n} 筆的 product_id 已更新")
            await session.commit()
            print("\n已寫入。")
        else:
            print("\n（dry-run，未寫入。加 --apply 才會實際寫入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
