"""品項、市場與代碼對照的查詢邏輯。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from slugify import slugify
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import PageParams
from app.models.catalog import DataSource, Market, Product, ProductName, ProductSourceMapping
from app.models.enums import ProductCategory


async def search_products(
    session: AsyncSession,
    *,
    params: PageParams,
    q: str | None = None,
    category: ProductCategory | None = None,
    locale: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[Product], int]:
    """依關鍵字搜尋品項。

    比對範圍是 product_names 的所有語系與別名，因此「高麗菜」「甘藍」
    「cabbage」都找得到同一個品項。
    """
    stmt: Select = select(Product).options(selectinload(Product.names))
    count_stmt = select(func.count(func.distinct(Product.id))).select_from(Product)

    if not include_inactive:
        stmt = stmt.where(Product.is_active.is_(True))
        count_stmt = count_stmt.where(Product.is_active.is_(True))
    if category is not None:
        stmt = stmt.where(Product.category == category)
        count_stmt = count_stmt.where(Product.category == category)

    if q:
        term = q.strip()
        if term:
            name_match = select(ProductName.product_id).where(
                ProductName.name.ilike(f"%{term}%")
            )
            if locale:
                # 指定語系時不排除其他語系，只是讓該語系優先；別名仍要能命中
                name_match = name_match.where(
                    or_(ProductName.locale == locale, ProductName.locale != locale)
                )
            cond = or_(Product.id.in_(name_match), Product.slug.ilike(f"%{term}%"))
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

    stmt = stmt.order_by(Product.popularity.desc(), Product.slug).limit(params.limit).offset(
        params.offset
    )
    items = list((await session.execute(stmt)).scalars().unique())
    total = (await session.scalar(count_stmt)) or 0
    return items, total


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product:
    product = await session.scalar(
        select(Product).options(selectinload(Product.names)).where(Product.id == product_id)
    )
    if product is None:
        raise NotFoundError("Product not found", code="product_not_found")
    return product


async def get_product_by_slug(session: AsyncSession, slug: str) -> Product:
    product = await session.scalar(
        select(Product).options(selectinload(Product.names)).where(Product.slug == slug)
    )
    if product is None:
        raise NotFoundError("Product not found", code="product_not_found")
    return product


async def resolve_product(session: AsyncSession, ref: str) -> Product:
    """允許前端用 UUID 或 slug 指到同一個品項。"""
    try:
        return await get_product(session, uuid.UUID(ref))
    except ValueError:
        return await get_product_by_slug(session, ref)


async def create_product(
    session: AsyncSession,
    *,
    slug: str | None,
    category: ProductCategory,
    default_unit: str,
    names: Sequence[tuple[str, str, bool]],
    image_url: str | None = None,
    popularity: int = 0,
) -> Product:
    """建立品項。`names` 是 (locale, name, is_primary) 的序列。"""
    if not names:
        raise ConflictError("至少要提供一個名稱", code="product_needs_name")

    slug = slug or slugify(names[0][1]) or uuid.uuid4().hex[:12]
    exists = await session.scalar(select(Product.id).where(Product.slug == slug))
    if exists:
        raise ConflictError(f"slug {slug!r} 已存在", code="product_slug_taken")

    product = Product(
        slug=slug,
        category=category,
        default_unit=default_unit,
        image_url=image_url,
        popularity=popularity,
    )
    seen: set[tuple[str, str]] = set()
    for locale, name, is_primary in names:
        key = (locale, name)
        if key in seen:
            continue
        seen.add(key)
        product.names.append(ProductName(locale=locale, name=name, is_primary=is_primary))
    session.add(product)
    await session.flush()
    return product


async def list_markets(
    session: AsyncSession,
    *,
    params: PageParams,
    country_code: str | None = None,
    source_key: str | None = None,
    q: str | None = None,
    only_active: bool = True,
) -> tuple[list[Market], int]:
    stmt: Select = select(Market)
    count_stmt = select(func.count()).select_from(Market)

    conditions = []
    if only_active:
        conditions.append(Market.is_active.is_(True))
    if country_code:
        conditions.append(Market.country_code == country_code.upper())
    if q:
        conditions.append(
            or_(Market.name.ilike(f"%{q}%"), Market.name_en.ilike(f"%{q}%"))
        )
    if source_key:
        sub = select(DataSource.id).where(DataSource.key == source_key)
        conditions.append(Market.source_id.in_(sub))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    stmt = stmt.order_by(Market.country_code, Market.name).limit(params.limit).offset(params.offset)
    items = list((await session.execute(stmt)).scalars())
    total = (await session.scalar(count_stmt)) or 0
    return items, total


async def get_market(session: AsyncSession, market_id: uuid.UUID) -> Market:
    market = await session.get(Market, market_id)
    if market is None:
        raise NotFoundError("Market not found", code="market_not_found")
    return market


async def list_mappings(
    session: AsyncSession,
    *,
    params: PageParams,
    source_key: str | None = None,
    unmapped_only: bool = False,
) -> tuple[list[ProductSourceMapping], int]:
    """列出來源代碼對照。維運時用 `unmapped_only=true` 找出還沒對應的品項。"""
    stmt: Select = select(ProductSourceMapping)
    count_stmt = select(func.count()).select_from(ProductSourceMapping)

    conditions = []
    if source_key:
        sub = select(DataSource.id).where(DataSource.key == source_key)
        conditions.append(ProductSourceMapping.source_id.in_(sub))
    if unmapped_only:
        conditions.append(ProductSourceMapping.product_id.is_(None))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    stmt = (
        stmt.order_by(ProductSourceMapping.external_code)
        .limit(params.limit)
        .offset(params.offset)
    )
    items = list((await session.execute(stmt)).scalars())
    total = (await session.scalar(count_stmt)) or 0
    return items, total


async def get_mapping(session: AsyncSession, mapping_id: uuid.UUID) -> ProductSourceMapping:
    mapping = await session.get(ProductSourceMapping, mapping_id)
    if mapping is None:
        raise NotFoundError("Mapping not found", code="mapping_not_found")
    return mapping
