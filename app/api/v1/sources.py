"""資料來源（extension）的公開端點。

讓 App 能顯示「資料來自哪裡、更新到什麼時候」，也方便部署後確認
某個 extension 到底有沒有被載進來。
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import DbSession
from app.core.errors import NotFoundError
from app.extensions.registry import registry
from app.models.catalog import DataSource
from app.schemas.catalog import SourceListOut, SourceLoadErrorOut, SourceOut

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=SourceListOut, summary="資料來源清單")
async def list_sources(session: DbSession) -> SourceListOut:
    rows = {
        row.key: row for row in (await session.execute(select(DataSource))).scalars()
    }
    loaded = {ext.key: ext for ext in registry.all()}

    out: list[SourceOut] = []
    # 以已載入的 extension 為主，再補上 DB 裡有但目前沒載入的（例如剛移除）
    for key in sorted(set(rows) | set(loaded)):
        ext = loaded.get(key)
        row = rows.get(key)
        manifest = ext.manifest if ext else None
        out.append(
            SourceOut(
                key=key,
                name=manifest.name if manifest else (row.name if row else key),
                country_code=manifest.country_code if manifest else (row.country_code if row else "--"),
                currency=manifest.currency if manifest else (row.currency if row else "---"),
                timezone=manifest.timezone if manifest else (row.timezone if row else "UTC"),
                version=manifest.version if manifest else (row.version if row else None),
                description=manifest.description if manifest else None,
                homepage_url=manifest.homepage_url if manifest else (row.homepage_url if row else None),
                license=manifest.license if manifest else (row.license if row else None),
                schedule=manifest.schedule if manifest else None,
                installed=ext is not None,
                enabled=bool(row.is_enabled) if row else True,
                last_run_at=row.last_run_at if row else None,
                last_success_at=row.last_success_at if row else None,
                last_error=row.last_error if row else None,
            )
        )

    return SourceListOut(
        sources=out,
        load_errors=[SourceLoadErrorOut(**e.as_dict()) for e in registry.errors],
    )


@router.get("/{key}", response_model=SourceOut, summary="單一資料來源")
async def get_source(key: str, session: DbSession) -> SourceOut:
    row = await session.scalar(select(DataSource).where(DataSource.key == key))
    ext = None
    if key in registry.keys():
        ext = registry.get(key)
    if row is None and ext is None:
        raise NotFoundError(f"Unknown source {key!r}", code="unknown_source")

    manifest = ext.manifest if ext else None
    return SourceOut(
        key=key,
        name=manifest.name if manifest else row.name,
        country_code=manifest.country_code if manifest else row.country_code,
        currency=manifest.currency if manifest else row.currency,
        timezone=manifest.timezone if manifest else row.timezone,
        version=manifest.version if manifest else row.version,
        description=manifest.description if manifest else None,
        homepage_url=manifest.homepage_url if manifest else row.homepage_url,
        license=manifest.license if manifest else row.license,
        schedule=manifest.schedule if manifest else None,
        installed=ext is not None,
        enabled=bool(row.is_enabled) if row else True,
        last_run_at=row.last_run_at if row else None,
        last_success_at=row.last_success_at if row else None,
        last_error=row.last_error if row else None,
    )
