"""維運端點。全部需要 X-Admin-Token。

這些不是給 App 用的，是給部署後手動觸發抓取、補品項對照、看排程狀態用的。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.core.deps import AdminGuard, DbSession, Paging
from app.core.pagination import Page
from app.extensions.registry import registry
from app.models.price import IngestRun
from app.models.user import User
from app.schemas.auth import AdminUserRoleUpdate, UserOut
from app.schemas.catalog import (
    IngestRunOut,
    MappingAssign,
    MappingOut,
    ProductCreate,
    ProductDetailOut,
)
from app.core.errors import NotFoundError
from app.services import auth as auth_service
from app.services import catalog as catalog_service
from app.services import ingest as ingest_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[AdminGuard])


@router.post("/sources/{key}/sync", summary="手動觸發一個來源的抓取")
async def sync_source(
    key: str,
    start: Annotated[date | None, Query(description="起始交易日，省略則用 lookback_days")] = None,
    end: Annotated[date | None, Query(description="結束交易日，省略則用今天")] = None,
) -> dict[str, Any]:
    """同步執行並回傳結果。回補大範圍時請注意 HTTP 逾時。"""
    result = await ingest_service.run_source(
        key, start=start, end=end, trigger="manual", is_backfill=start is not None
    )
    return result.as_dict()


@router.post("/sources/sync-all", summary="觸發所有來源的抓取")
async def sync_all() -> dict[str, Any]:
    results = await ingest_service.run_all(trigger="manual")
    return {"results": [r.as_dict() for r in results]}


@router.post("/sources/reload", summary="重新掃描 extensions 目錄")
async def reload_sources(session: DbSession) -> dict[str, Any]:
    """改了 extension 資料夾之後不用重啟服務。

    注意：Python 模組已經 import 過的不會重新載入，程式碼有改動仍需重啟。
    這裡主要處理「新增或移除整個 extension 資料夾」的情況。
    """
    registry.discover(force=True)
    await ingest_service.sync_all_source_rows(session)
    return {
        "loaded": registry.keys(),
        "errors": [e.as_dict() for e in registry.errors],
    }


@router.get("/ingest-runs", response_model=Page[IngestRunOut], summary="抓取執行紀錄")
async def list_runs(
    session: DbSession,
    paging: Paging,
    source_key: Annotated[str | None, Query()] = None,
) -> Page[IngestRunOut]:
    from sqlalchemy import func

    stmt = select(IngestRun).order_by(IngestRun.started_at.desc())
    count_stmt = select(func.count()).select_from(IngestRun)
    if source_key:
        stmt = stmt.where(IngestRun.source_key == source_key)
        count_stmt = count_stmt.where(IngestRun.source_key == source_key)

    rows = list(
        (await session.execute(stmt.limit(paging.limit).offset(paging.offset))).scalars()
    )
    total = (await session.scalar(count_stmt)) or 0
    return Page.build([IngestRunOut.model_validate(r) for r in rows], total, paging)


@router.get("/mappings", response_model=Page[MappingOut], summary="來源代碼對照")
async def list_mappings(
    session: DbSession,
    paging: Paging,
    source_key: Annotated[str | None, Query()] = None,
    unmapped_only: Annotated[bool, Query(description="只看還沒對應到品項的")] = False,
) -> Page[MappingOut]:
    items, total = await catalog_service.list_mappings(
        session, params=paging, source_key=source_key, unmapped_only=unmapped_only
    )
    return Page.build([MappingOut.from_model(m) for m in items], total, paging)


@router.put("/mappings/{mapping_id}", response_model=MappingOut, summary="指定對照的品項")
async def assign_mapping(
    mapping_id: uuid.UUID,
    payload: MappingAssign,
    session: DbSession,
) -> MappingOut:
    """把來源代碼接到標準品項上，並回填既有價格列的 product_id。"""
    mapping = await catalog_service.get_mapping(session, mapping_id)
    await catalog_service.get_product(session, payload.product_id)  # 確認品項存在
    mapping.unit_factor = payload.unit_factor
    await ingest_service.backfill_mapping_product(session, mapping_id, payload.product_id)
    await session.refresh(mapping)
    return MappingOut.from_model(mapping)


@router.post(
    "/products",
    response_model=ProductDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增標準品項",
)
async def create_product(
    payload: ProductCreate,
    session: DbSession,
    locale: Annotated[str, Query(description="回應要用哪個語系的名稱")] = "zh-Hant",
) -> ProductDetailOut:
    product = await catalog_service.create_product(
        session,
        slug=payload.slug,
        category=payload.category,
        default_unit=payload.default_unit,
        names=[(n.locale, n.name, n.is_primary) for n in payload.names],
        image_url=payload.image_url,
        image_source=payload.image_source,
        image_source_url=payload.image_source_url,
        image_license=payload.image_license,
        image_author=payload.image_author,
        popularity=payload.popularity,
    )
    return ProductDetailOut.from_model(product, locale)


@router.get("/scheduler", summary="排程狀態")
async def scheduler_status() -> dict[str, Any]:
    from app.services.scheduler import job_status

    return {"jobs": job_status()}


@router.patch("/users/{user_id}", response_model=UserOut, summary="更正使用者身分")
async def set_user_role(
    user_id: uuid.UUID,
    payload: AdminUserRoleUpdate,
    session: DbSession,
) -> UserOut:
    """身分在註冊時綁定、使用者改不了，這裡是唯一的更正管道。

    既有報價的 `role_snapshot` 不會被回溯修改——那記錄的是報價當下的事實。
    """
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found", code="user_not_found")
    await auth_service.admin_set_role(session, user, payload.role, reason=payload.reason)
    return UserOut.model_validate(user)


@router.get("/users", response_model=Page[UserOut], summary="使用者清單")
async def list_users(
    session: DbSession,
    paging: Paging,
    role: Annotated[str | None, Query(description="只看某個身分")] = None,
    phone: Annotated[str | None, Query(description="號碼片段搜尋")] = None,
) -> Page[UserOut]:
    from sqlalchemy import func

    stmt = select(User).order_by(User.created_at.desc())
    count_stmt = select(func.count()).select_from(User)
    if role:
        stmt = stmt.where(User.role == role)
        count_stmt = count_stmt.where(User.role == role)
    if phone:
        stmt = stmt.where(User.phone.ilike(f"%{phone}%"))
        count_stmt = count_stmt.where(User.phone.ilike(f"%{phone}%"))

    rows = list((await session.execute(stmt.limit(paging.limit).offset(paging.offset))).scalars())
    total = (await session.scalar(count_stmt)) or 0
    return Page.build([UserOut.model_validate(u) for u in rows], total, paging)
