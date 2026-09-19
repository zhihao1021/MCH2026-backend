"""背景排程：定時跑各 extension 的 ingest，並清理過期報價。

每個 extension 的執行頻率來自它自己的 `manifest.schedule`，
因此新增一個資料來源不需要動排程設定。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.database import session_scope
from app.extensions.registry import registry
from app.services import ingest
from app.services.quotes import expire_stale_quotes

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _run_source_job(key: str) -> None:
    logger.info("排程觸發 ingest：%s", key)
    result = await ingest.run_source(key, trigger="schedule")
    logger.info("ingest %s 結束：%s", key, result.as_dict())


async def _expire_quotes_job() -> None:
    async with session_scope() as session:
        count = await expire_stale_quotes(session)
    if count:
        logger.info("已將 %d 筆過期報價標記為 expired", count)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)

    for ext in registry.all():
        trigger = CronTrigger.from_crontab(
            ext.manifest.schedule, timezone=settings.scheduler_timezone
        )
        scheduler.add_job(
            _run_source_job,
            trigger=trigger,
            args=[ext.key],
            id=f"ingest:{ext.key}",
            name=f"Ingest {ext.manifest.name}",
            max_instances=1,      # 同一來源不併行，避免重複抓
            coalesce=True,        # 服務停機期間堆積的觸發只補跑一次
            misfire_grace_time=600,
            replace_existing=True,
        )
        logger.info("已排程 %s：%s", ext.key, ext.manifest.schedule)

    scheduler.add_job(
        _expire_quotes_job,
        trigger=CronTrigger.from_crontab("*/10 * * * *", timezone=settings.scheduler_timezone),
        id="quotes:expire",
        name="Expire stale quotes",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false，不啟動排程")
        return None
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = build_scheduler()
    _scheduler.start()
    logger.info("排程已啟動，共 %d 個工作", len(_scheduler.get_jobs()))
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("排程已停止")
    _scheduler = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def job_status() -> list[dict[str, object]]:
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "name": job.name,
            "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None,
        }
        for job in _scheduler.get_jobs()
    ]
