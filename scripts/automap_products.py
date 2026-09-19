"""把來源的品項代碼自動對照到平台標準品項。

    # 先看會對到什麼（預設就是 dry-run，不寫入）
    python scripts/automap_products.py --source tw_moa

    # 確認沒問題再寫入
    python scripts/automap_products.py --source tw_moa --apply

    # 連沒對到的也列出來，方便決定要新增哪些品項或別名
    python scripts/automap_products.py --source tw_moa --show-unmatched

對照規則（由嚴到寬，先中先贏）：

1. `external_name` 與某個 `product_names.name` 完全相同；
2. **基底名**（第一個 `-` 之前）與某個 `product_names.name` 完全相同。

比對對象包含**所有語系與別名**，所以 MOA 的「甘藍」能對到 slug `cabbage`
（它的 zh-Hant 別名裡有「甘藍」），不需要另外維護一份對照表。

自動對到的會標成 `is_confirmed = False`，人工確認過的（`is_confirmed = True`）
永遠不會被這支工具動到。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_scope
from app.models.catalog import DataSource, Product, ProductName, ProductSourceMapping
from app.models.price import OfficialPrice

# 基底名相同但其實是別的東西，絕對不能自動對照。
# 例：「蘿蔔-甜菜根」基底是「蘿蔔」，會誤對到白蘿蔔。
EXCLUDE_EXACT: dict[str, set[str]] = {
    "tw_moa": {
        "蘿蔔-甜菜根",      # 甜菜根
        "蘿蔔-櫻桃",        # 櫻桃蘿蔔
        "蘿蔔乾",           # 加工品
        "大蒜-蔥蒜",        # 蒜，不是蔥
        "進口丹頂(蔥花)",    # 花卉
    },
    "ug_namis": {
        # NAMIS 同時列了 "Matooke" 與 "Matooke (kg)"，兩者都標示單位為 Kg，
        # 但前者的價格是每「串」（25,000 UGX 上下）、後者才是每公斤（1,000 上下）。
        # 對照上去會讓同一個品項的價格序列差 20 倍，所以每串那筆留給人工——
        # 要收的話得先在對照表上設好 unit_factor（一串大約幾公斤）。
        "Matooke",
    },
}

# 基底名本身就模稜兩可，一律不自動對照，留給人工。
EXCLUDE_BASE: dict[str, set[str]] = {
    "tw_moa": {"其他", "進口"},
}


def base_name(name: str) -> str:
    """取基底名：第一個 `-` 之前的部分。'甘藍-初秋' -> '甘藍'"""
    return name.split("-", 1)[0].strip()


async def build_name_index(session: AsyncSession) -> dict[str, list[tuple[str, str]]]:
    """{名稱: [(product_id, slug), ...]}，涵蓋所有語系與別名。"""
    rows = await session.execute(
        select(ProductName.name, ProductName.product_id, Product.slug)
        .join(Product, Product.id == ProductName.product_id)
        .where(Product.is_active.is_(True))
    )
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, product_id, slug in rows:
        index[name.strip()].append((str(product_id), slug))
    return index


async def run(source_key: str, apply: bool, show_unmatched: bool, confirm: bool) -> int:
    exclude_exact = EXCLUDE_EXACT.get(source_key, set())
    exclude_base = EXCLUDE_BASE.get(source_key, set())

    async with session_scope() as session:
        ds = await session.scalar(select(DataSource).where(DataSource.key == source_key))
        if ds is None:
            print(f"找不到資料來源 {source_key!r}", file=sys.stderr)
            return 2

        index = await build_name_index(session)
        print(f"品項名稱索引：{len(index)} 個名稱（含別名與各語系）\n")

        mappings = list(
            (
                await session.execute(
                    select(ProductSourceMapping).where(
                        ProductSourceMapping.source_id == ds.id,
                        ProductSourceMapping.product_id.is_(None),
                        ProductSourceMapping.is_confirmed.is_(False),
                    )
                )
            ).scalars()
        )
        print(f"待對照的代碼：{len(mappings)} 筆")

        matched: dict[str, list[tuple[ProductSourceMapping, str]]] = defaultdict(list)
        ambiguous: list[tuple[str, list[str]]] = []
        unmatched: list[str] = []

        for m in mappings:
            name = (m.external_name or "").strip()
            if not name or name in exclude_exact:
                unmatched.append(name or m.external_code)
                continue

            hits = index.get(name)
            rule = "全名"
            if not hits:
                bn = base_name(name)
                if bn in exclude_base:
                    unmatched.append(name)
                    continue
                hits = index.get(bn)
                rule = "基底名"

            if not hits:
                unmatched.append(name)
                continue

            # 同一個名稱對到多個品項就別猜了，交給人工
            slugs = {slug for _, slug in hits}
            if len(slugs) > 1:
                ambiguous.append((name, sorted(slugs)))
                continue

            product_id, slug = hits[0]
            matched[slug].append((m, rule))

        total_matched = sum(len(v) for v in matched.values())
        print(f"可對照：{total_matched} 筆 -> {len(matched)} 個品項")
        print(f"名稱歧義：{len(ambiguous)} 筆")
        print(f"對不到：{len(unmatched)} 筆\n")

        for slug in sorted(matched):
            items = matched[slug]
            names = [m.external_name for m, _ in items]
            print(f"  {slug:16} {len(items):3} 筆  {names[:5]}{' …' if len(names) > 5 else ''}")

        if ambiguous:
            print("\n名稱歧義（需人工處理）：")
            for name, slugs in ambiguous[:20]:
                print(f"  {name} -> {slugs}")

        if show_unmatched:
            print(f"\n對不到的（{len(unmatched)} 筆）：")
            bases = sorted({base_name(n) for n in unmatched})
            for i in range(0, len(bases), 6):
                print("  " + "  ".join(f"{b:10}" for b in bases[i : i + 6]))

        if not apply:
            print("\n（dry-run，未寫入。加 --apply 才會實際寫入）")
            return 0

        # -- 寫入 ----------------------------------------------------------
        written = backfilled = 0
        for slug, items in matched.items():
            product_id = await session.scalar(select(Product.id).where(Product.slug == slug))
            ids = [m.id for m, _ in items]
            await session.execute(
                update(ProductSourceMapping)
                .where(ProductSourceMapping.id.in_(ids))
                .values(product_id=product_id, is_confirmed=confirm)
            )
            res = await session.execute(
                update(OfficialPrice)
                .where(OfficialPrice.mapping_id.in_(ids))
                .values(product_id=product_id)
            )
            written += len(ids)
            backfilled += res.rowcount or 0

        print(f"\n已寫入 {written} 筆對照，回填 {backfilled} 筆價格的 product_id")
        print(f"is_confirmed = {confirm}")

        # 全量同步：把所有價格列的 product_id 對回它自己的對照表。
        #
        # 只回填「這次新對照的」是不夠的——如果有 ingest 正在跑，它在開始時
        # 就快取了當時的對照表（product_id 還是 NULL），之後 upsert 會把
        # 已經回填好的 product_id 又蓋成 NULL。這個全量同步是冪等的，
        # 放在最後一律跑一次，就不用去記「當時有沒有 ingest 在跑」。
        resync = await session.execute(
            update(OfficialPrice)
            .where(
                OfficialPrice.source_id == ds.id,
                OfficialPrice.mapping_id == ProductSourceMapping.id,
                OfficialPrice.product_id.is_distinct_from(ProductSourceMapping.product_id),
            )
            .values(product_id=ProductSourceMapping.product_id)
        )
        print(f"全量同步：修正 {resync.rowcount or 0} 筆不一致的 product_id")

        remaining = await session.scalar(
            select(func.count())
            .select_from(ProductSourceMapping)
            .where(
                ProductSourceMapping.source_id == ds.id,
                ProductSourceMapping.product_id.is_(None),
            )
        )
        print(f"仍未對照：{remaining} 筆")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", required=True, help="資料來源 key，例如 tw_moa")
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設只試算）")
    parser.add_argument("--show-unmatched", action="store_true", help="列出對不到的基底名")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="把結果標成人工已確認（之後不會被這支工具再動）",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.source, args.apply, args.show_unmatched, args.confirm))


if __name__ == "__main__":
    sys.exit(main())
