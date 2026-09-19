"""FastAPI 應用進入點。"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.core.database import dispose_engine, engine, session_scope
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.extensions.registry import registry
from app.schemas.common import HealthOut
from app.services import ingest as ingest_service
from app.services.scheduler import shutdown_scheduler, start_scheduler

VERSION = "0.1.0"

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    setup_logging()
    logger.info("啟動 %s（%s）", settings.app_name, settings.environment)

    # 1. 掃描 extension
    registry.discover(force=True)
    for err in registry.errors:
        logger.error("extension 載入失敗：%s", err.as_dict())

    # 2. 把 extension 登記進 data_sources。DB 還沒 migrate 時不要讓服務起不來，
    #    否則第一次部署會卡在雞生蛋的問題上。
    try:
        async with session_scope() as session:
            await ingest_service.sync_all_source_rows(session)
    except Exception:
        logger.exception("同步 data_sources 失敗，請確認資料庫已執行 alembic upgrade head")

    # 3. 排程
    start_scheduler()
    if settings.scheduler_run_on_startup:
        import asyncio

        asyncio.create_task(ingest_service.run_all(trigger="startup"))

    try:
        yield
    finally:
        shutdown_scheduler()
        await dispose_engine()
        logger.info("已關閉")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=VERSION,
        description=(
            "農產品即時價格 API：官方批發行情（由 extension 提供，可擴充多國）"
            "與小農 / 盤商自行報價。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/healthz", response_model=HealthOut, tags=["meta"], summary="健康檢查")
    async def healthz() -> HealthOut:
        db_status = "ok"
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            db_status = f"error: {type(exc).__name__}"

        return HealthOut(
            status="ok" if db_status == "ok" else "degraded",
            environment=settings.environment,
            version=VERSION,
            database=db_status,
            extensions_loaded=len(registry.keys()),
            extensions_failed=len(registry.errors),
        )

    return app


app = create_app()
