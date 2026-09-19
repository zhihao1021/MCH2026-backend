"""把 extension 產出的 RawPrice 正規化並寫進資料庫。

Extension 與資料庫之間所有的髒活都在這裡：
建市場、建代碼對照、換算單位、批次 upsert、記錄執行結果。
Extension 本身完全不需要知道這些。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_scope
from app.extensions.base import FetchWindow, PriceSource, RawMarket, RawPrice
from app.extensions.http import http_client
from app.extensions.registry import LoadedExtension, registry
from app.models.catalog import DataSource, Market, Product, ProductSourceMapping
from app.models.enums import IngestStatus
from app.models.price import IngestRun, OfficialPrice

logger = logging.getLogger(__name__)

# 每累積這麼多筆就寫一次 DB。太小會拖慢，太大會吃記憶體並拉長交易時間。
BATCH_SIZE = 500


@dataclass
class IngestResult:
    source_key: str
    status: IngestStatus
    window_start: date
    window_end: date
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    markets_created: int = 0
    mappings_created: int = 0
    duration_ms: int = 0
    error: str | None = None
    # 被略過的原因統計，例如 {"invalid_price": 3}
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "status": self.status.value,
            "window": {"start": self.window_start.isoformat(), "end": self.window_end.isoformat()},
            "fetched": self.fetched,
            "written": self.written,
            "skipped": self.skipped,
            "markets_created": self.markets_created,
            "mappings_created": self.mappings_created,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "skip_reasons": self.skip_reasons,
        }


# ---------------------------------------------------------------------------
# data_sources 同步
# ---------------------------------------------------------------------------


async def sync_source_row(session: AsyncSession, ext: LoadedExtension) -> DataSource:
    """把 manifest 的內容 upsert 進 data_sources，回傳該列。"""
    m = ext.manifest
    stmt = (
        pg_insert(DataSource)
        .values(
            key=m.key,
            name=m.name,
            country_code=m.country_code,
            timezone=m.timezone,
            currency=m.currency,
            homepage_url=m.homepage_url,
            license=m.license,
            version=m.version,
            is_installed=True,
        )
        .on_conflict_do_update(
            index_elements=[DataSource.key],
            set_={
                "name": m.name,
                "country_code": m.country_code,
                "timezone": m.timezone,
                "currency": m.currency,
                "homepage_url": m.homepage_url,
                "license": m.license,
                "version": m.version,
                "is_installed": True,
            },
        )
        .returning(DataSource)
    )
    row = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return row


async def sync_all_source_rows(session: AsyncSession) -> list[DataSource]:
    """啟動時呼叫：登記所有已載入的 extension，並把消失的標成未安裝。"""
    loaded = registry.all()
    rows = [await sync_source_row(session, ext) for ext in loaded]
    keys = [ext.key for ext in loaded]
    if keys:
        await session.execute(
            update(DataSource).where(DataSource.key.notin_(keys)).values(is_installed=False)
        )
    else:
        await session.execute(update(DataSource).values(is_installed=False))
    return rows


# ---------------------------------------------------------------------------
# 執行
# ---------------------------------------------------------------------------


async def run_source(
    key: str,
    *,
    start: date | None = None,
    end: date | None = None,
    trigger: str = "schedule",
    is_backfill: bool = False,
) -> IngestResult:
    """跑完一個來源。區間超過 manifest.max_window_days 時自動切段。"""
    ext = registry.require(key)
    today = datetime.now(UTC).date()
    end = end or today
    start = start or (end - timedelta(days=ext.manifest.lookback_days))
    if start > end:
        start = end

    total = IngestResult(
        source_key=key, status=IngestStatus.SUCCESS, window_start=start, window_end=end
    )
    began = time.perf_counter()

    async with http_client() as client:
        source = ext.instantiate(client)
        try:
            await source.setup()
        except Exception as exc:
            total.status = IngestStatus.FAILED
            total.error = f"setup failed: {type(exc).__name__}: {exc}"
            total.duration_ms = int((time.perf_counter() - began) * 1000)
            await _record_failure(ext, total, trigger)
            return total

        try:
            for chunk_start, chunk_end in _split_window(start, end, ext.manifest.max_window_days):
                part = await _run_window(
                    ext, source, chunk_start, chunk_end, trigger=trigger, is_backfill=is_backfill
                )
                _merge(total, part)
                if part.status is IngestStatus.FAILED:
                    total.status = IngestStatus.FAILED
                    total.error = part.error
                    break
        finally:
            try:
                await source.teardown()
            except Exception:  # pragma: no cover - teardown 失敗不該蓋掉主結果
                logger.exception("extension %s teardown 失敗", key)

    total.duration_ms = int((time.perf_counter() - began) * 1000)
    if total.status is not IngestStatus.FAILED and total.skipped and total.written:
        total.status = IngestStatus.PARTIAL
    return total


async def run_all(*, trigger: str = "schedule") -> list[IngestResult]:
    results: list[IngestResult] = []
    for ext in registry.all():
        try:
            results.append(await run_source(ext.key, trigger=trigger))
        except Exception as exc:  # pragma: no cover - 單一來源壞掉不影響其他
            logger.exception("來源 %s 執行時未預期失敗", ext.key)
            today = datetime.now(UTC).date()
            results.append(
                IngestResult(
                    source_key=ext.key,
                    status=IngestStatus.FAILED,
                    window_start=today,
                    window_end=today,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


async def _run_window(
    ext: LoadedExtension,
    source: PriceSource,
    start: date,
    end: date,
    *,
    trigger: str,
    is_backfill: bool,
) -> IngestResult:
    result = IngestResult(
        source_key=ext.key, status=IngestStatus.SUCCESS, window_start=start, window_end=end
    )
    began = time.perf_counter()

    async with session_scope() as session:
        ds = await sync_source_row(session, ext)
        run = IngestRun(
            source_id=ds.id,
            source_key=ext.key,
            status=IngestStatus.RUNNING,
            trigger=trigger,
            window_start=start,
            window_end=end,
        )
        session.add(run)
        await session.flush()

        window = FetchWindow(start=start, end=end, cursor=ds.cursor or {}, is_backfill=is_backfill)
        loader = _Loader(session, ext, ds)

        try:
            if ext.manifest.provides_markets:
                markets = await source.fetch_markets()
                result.markets_created += await loader.upsert_markets(markets)

            batch: list[RawPrice] = []
            async for raw in source.fetch_prices(window):
                result.fetched += 1
                batch.append(raw)
                if len(batch) >= BATCH_SIZE:
                    await loader.write_batch(batch, result)
                    batch.clear()
            if batch:
                await loader.write_batch(batch, result)

            result.markets_created += loader.markets_created
            result.mappings_created += loader.mappings_created

            ds.cursor = await source.next_cursor(window, result.fetched)
            ds.last_run_at = datetime.now(UTC)
            ds.last_success_at = ds.last_run_at
            ds.last_error = None
            run.status = IngestStatus.SUCCESS if not result.skipped else IngestStatus.PARTIAL
            result.status = run.status
        except Exception as exc:
            logger.exception("來源 %s 抓取失敗（%s ~ %s）", ext.key, start, end)
            message = f"{type(exc).__name__}: {exc}"[:2000]
            result.status = IngestStatus.FAILED
            result.error = message
            run.status = IngestStatus.FAILED
            run.error = message
            ds.last_run_at = datetime.now(UTC)
            ds.last_error = message
            # 保留原游標，下次從同一點重試

        result.duration_ms = int((time.perf_counter() - began) * 1000)
        run.records_fetched = result.fetched
        run.records_written = result.written
        run.records_skipped = result.skipped
        run.markets_created = result.markets_created
        run.mappings_created = result.mappings_created
        run.finished_at = datetime.now(UTC)
        run.duration_ms = result.duration_ms

    return result


async def _record_failure(ext: LoadedExtension, result: IngestResult, trigger: str) -> None:
    async with session_scope() as session:
        ds = await sync_source_row(session, ext)
        ds.last_run_at = datetime.now(UTC)
        ds.last_error = result.error
        session.add(
            IngestRun(
                source_id=ds.id,
                source_key=ext.key,
                status=IngestStatus.FAILED,
                trigger=trigger,
                window_start=result.window_start,
                window_end=result.window_end,
                error=result.error,
                finished_at=datetime.now(UTC),
                duration_ms=result.duration_ms,
            )
        )


# ---------------------------------------------------------------------------
# 寫入
# ---------------------------------------------------------------------------


class _Loader:
    """單次 ingest 的寫入器。市場與對照表在記憶體快取，避免每筆都查一次。"""

    def __init__(self, session: AsyncSession, ext: LoadedExtension, ds: DataSource) -> None:
        self.session = session
        self.ext = ext
        self.ds = ds
        self._markets: dict[str, Market] = {}
        self._mappings: dict[str, ProductSourceMapping] = {}
        self._units: dict[str, str] = {}  # product_id -> default_unit
        self.markets_created = 0
        self.mappings_created = 0

    async def upsert_markets(self, markets: Sequence[RawMarket]) -> int:
        created = 0
        for m in markets:
            existing = await self._get_market(m.external_id)
            if existing is None:
                existing = Market(
                    source_id=self.ds.id,
                    external_id=m.external_id,
                    country_code=self.ext.manifest.country_code,
                    name=m.name,
                    timezone=m.timezone or self.ext.manifest.timezone,
                )
                self.session.add(existing)
                created += 1
            existing.name = m.name
            existing.name_en = m.name_en or existing.name_en
            existing.region = m.region or existing.region
            existing.latitude = m.latitude if m.latitude is not None else existing.latitude
            existing.longitude = m.longitude if m.longitude is not None else existing.longitude
            existing.is_active = m.is_active
            existing.raw = m.raw or existing.raw
            self._markets[m.external_id] = existing
        await self.session.flush()
        return created

    async def write_batch(self, batch: Sequence[RawPrice], result: IngestResult) -> None:
        rows: list[dict[str, Any]] = []
        for raw in batch:
            row = await self._to_row(raw, result)
            if row is not None:
                rows.append(row)
        if not rows:
            return

        stmt = pg_insert(OfficialPrice).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_official_prices_source_market_mapping_date_grade",
            set_={
                "product_id": stmt.excluded.product_id,
                "currency": stmt.excluded.currency,
                "unit": stmt.excluded.unit,
                "price_avg": stmt.excluded.price_avg,
                "price_high": stmt.excluded.price_high,
                "price_mid": stmt.excluded.price_mid,
                "price_low": stmt.excluded.price_low,
                "volume": stmt.excluded.volume,
                "volume_unit": stmt.excluded.volume_unit,
                "raw": stmt.excluded.raw,
                "ingested_at": datetime.now(UTC),
            },
        )
        await self.session.execute(stmt)
        result.written += len(rows)

    async def _to_row(self, raw: RawPrice, result: IngestResult) -> dict[str, Any] | None:
        market = await self._ensure_market(raw)
        mapping = await self._ensure_mapping(raw)

        unit = raw.unit or self.ext.manifest.default_unit
        factor = mapping.unit_factor or 1.0
        target_unit = self._units.get(str(mapping.product_id)) if mapping.product_id else None

        prices = {
            "price_avg": raw.effective_avg(),
            "price_high": raw.price_high,
            "price_mid": raw.price_mid,
            "price_low": raw.price_low,
        }
        volume = raw.volume

        # unit_factor 的定義：1 個來源單位 = factor 個品項標準單位。
        # 因此價格要除以 factor、數量要乘以 factor，兩邊才會落在同一個尺度。
        if target_unit and factor and factor != 1.0:
            try:
                f = Decimal(str(factor))
                prices = {k: (v / f if v is not None else None) for k, v in prices.items()}
                volume = volume * f if volume is not None else None
                unit = target_unit
            except (InvalidOperation, ZeroDivisionError):
                result.skipped += 1
                result.skip_reasons["bad_unit_factor"] = (
                    result.skip_reasons.get("bad_unit_factor", 0) + 1
                )
                return None

        if all(v is None for v in prices.values()):
            result.skipped += 1
            result.skip_reasons["no_price"] = result.skip_reasons.get("no_price", 0) + 1
            return None

        return {
            "source_id": self.ds.id,
            "market_id": market.id,
            "mapping_id": mapping.id,
            "product_id": mapping.product_id,
            "trade_date": raw.trade_date,
            "currency": raw.currency or self.ext.manifest.currency,
            "unit": unit,
            "grade": raw.grade or "",
            **prices,
            "volume": volume,
            "volume_unit": raw.volume_unit,
            "raw": raw.raw,
            "ingested_at": datetime.now(UTC),
        }

    async def _ensure_market(self, raw: RawPrice) -> Market:
        market = await self._get_market(raw.market_external_id)
        if market is None:
            market = Market(
                source_id=self.ds.id,
                external_id=raw.market_external_id,
                country_code=self.ext.manifest.country_code,
                name=raw.market_name or raw.market_external_id,
                timezone=self.ext.manifest.timezone,
            )
            self.session.add(market)
            await self.session.flush()
            self.markets_created += 1
            self._markets[raw.market_external_id] = market
        return market

    async def _get_market(self, external_id: str) -> Market | None:
        if external_id in self._markets:
            return self._markets[external_id]
        stmt = select(Market).where(
            Market.source_id == self.ds.id, Market.external_id == external_id
        )
        market = (await self.session.execute(stmt)).scalar_one_or_none()
        if market is not None:
            self._markets[external_id] = market
        return market

    async def _ensure_mapping(self, raw: RawPrice) -> ProductSourceMapping:
        """取得代碼對照；沒有就建一筆待對應（product_id = NULL）的紀錄。

        沒對應到標準品項也照樣收資料——之後補上對應再回填 product_id 即可，
        總比把資料丟掉好。
        """
        cached = self._mappings.get(raw.product_code)
        if cached is not None:
            return cached

        stmt = select(ProductSourceMapping).where(
            ProductSourceMapping.source_id == self.ds.id,
            ProductSourceMapping.external_code == raw.product_code,
        )
        mapping = (await self.session.execute(stmt)).scalar_one_or_none()

        if mapping is None:
            mapping = ProductSourceMapping(
                source_id=self.ds.id,
                external_code=raw.product_code,
                external_name=raw.product_name,
                source_unit=raw.unit or self.ext.manifest.default_unit,
                unit_factor=1.0,
                raw={},
            )
            self.session.add(mapping)
            await self.session.flush()
            self.mappings_created += 1
        elif raw.product_name and not mapping.external_name:
            mapping.external_name = raw.product_name

        if mapping.product_id and str(mapping.product_id) not in self._units:
            unit = await self.session.scalar(
                select(Product.default_unit).where(Product.id == mapping.product_id)
            )
            if unit:
                self._units[str(mapping.product_id)] = unit

        self._mappings[raw.product_code] = mapping
        return mapping


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _split_window(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _merge(total: IngestResult, part: IngestResult) -> None:
    total.fetched += part.fetched
    total.written += part.written
    total.skipped += part.skipped
    total.markets_created += part.markets_created
    total.mappings_created += part.mappings_created
    total.window_end = max(total.window_end, part.window_end)
    for reason, count in part.skip_reasons.items():
        total.skip_reasons[reason] = total.skip_reasons.get(reason, 0) + count


async def backfill_mapping_product(
    session: AsyncSession, mapping_id: Any, product_id: Any
) -> int:
    """把某個對照的品項改掉，並回填既有價格列的 product_id。"""
    await session.execute(
        update(ProductSourceMapping)
        .where(ProductSourceMapping.id == mapping_id)
        .values(product_id=product_id, is_confirmed=True)
    )
    res = await session.execute(
        update(OfficialPrice)
        .where(OfficialPrice.mapping_id == mapping_id)
        .values(product_id=product_id)
    )
    return res.rowcount or 0


async def iter_results(results: Sequence[IngestResult]) -> AsyncIterator[dict[str, Any]]:
    for r in results:
        yield r.as_dict()
