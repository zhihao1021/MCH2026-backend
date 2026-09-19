"""立即執行 extension 的抓取，不需要啟動 API 服務。

    # 列出已載入的來源
    python scripts/run_ingest.py --list

    # 跑一個來源（區間用 manifest.lookback_days）
    python scripts/run_ingest.py demo_mock

    # 指定區間回補
    python scripts/run_ingest.py demo_mock --start 2026-09-01 --end 2026-09-18

    # 全部來源
    python scripts/run_ingest.py --all

    # 只看 extension 會吐什麼，不寫資料庫
    python scripts/run_ingest.py demo_mock --dry-run --limit 5

開發新 extension 時建議先用 `--dry-run` 確認解析正確，再真的寫入。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, timedelta

from app.core.errors import NotFoundError
from app.core.logging import setup_logging
from app.extensions.base import FetchWindow
from app.extensions.http import http_client
from app.extensions.registry import registry
from app.services import ingest as ingest_service


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def list_sources() -> int:
    registry.discover(force=True)
    exts = registry.all()
    if not exts:
        print("沒有載入任何 extension")
    for ext in exts:
        m = ext.manifest
        print(f"{m.key:16} {m.name}")
        print(f"{'':16} {m.country_code} / {m.currency} / {m.timezone}")
        print(f"{'':16} 排程 {m.schedule}  回補 {m.lookback_days} 天  單次上限 {m.max_window_days} 天")
    for err in registry.errors:
        print(f"[載入失敗] {err.key}: {err.reason} — {err.detail}")
    return 1 if registry.errors else 0


async def dry_run(key: str, start: date | None, end: date | None, limit: int) -> int:
    """只呼叫 extension，把結果印出來，完全不碰資料庫。"""
    registry.discover(force=True)
    ext = registry.require(key)

    end = end or datetime.now(UTC).date()
    start = start or (end - timedelta(days=ext.manifest.lookback_days))
    window = FetchWindow(start=start, end=end, is_backfill=True)
    print(f"dry-run {key}：{start} ~ {end}（不寫入資料庫）\n")

    async with http_client() as client:
        source = ext.instantiate(client)
        await source.setup()
        try:
            markets = await source.fetch_markets()
            print(f"market {len(markets)} 筆：")
            for m in markets[:limit]:
                print(f"  {m.external_id:10} {m.name}  region={m.region}")
            if len(markets) > limit:
                print(f"  ... 另外 {len(markets) - limit} 筆")

            print(f"\nprice（前 {limit} 筆）：")
            count = 0
            async for row in source.fetch_prices(window):
                if count < limit:
                    print(
                        f"  {row.trade_date}  {row.market_external_id:10}"
                        f" {row.product_code:10} {row.product_name or '':12}"
                        f" avg={row.price_avg} high={row.price_high} low={row.price_low}"
                        f" {row.currency or ext.manifest.currency}/{row.unit or ext.manifest.default_unit}"
                    )
                count += 1
            print(f"\n總共產出 {count} 筆")
        finally:
            await source.teardown()
    return 0


def _report(result: ingest_service.IngestResult) -> None:
    d = result.as_dict()
    print(
        f"{d['source_key']:16} {d['status']:8}"
        f" 抓取 {d['fetched']:6} 寫入 {d['written']:6} 略過 {d['skipped']:4}"
        f" 市場+{d['markets_created']} 對照+{d['mappings_created']}"
        f" {d['duration_ms']}ms"
    )
    if d["skip_reasons"]:
        print(f"{'':16} 略過原因：{d['skip_reasons']}")
    if d["error"]:
        print(f"{'':16} 錯誤：{d['error']}")


async def run_one(key: str, start: date | None, end: date | None) -> int:
    registry.discover(force=True)
    registry.require(key)  # 先確認存在，錯字才不會跑一半才發現
    result = await ingest_service.run_source(
        key, start=start, end=end, trigger="cli", is_backfill=start is not None
    )
    _report(result)
    return 0 if result.status.value != "failed" else 1


async def run_all() -> int:
    registry.discover(force=True)
    results = await ingest_service.run_all(trigger="cli")
    for result in results:
        _report(result)
    return 0 if all(r.status.value != "failed" for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("key", nargs="?", help="要執行的 extension key（資料夾名）")
    parser.add_argument("--all", action="store_true", help="執行所有已載入的來源")
    parser.add_argument("--list", action="store_true", help="列出已載入的來源後結束")
    parser.add_argument("--start", help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--end", help="結束交易日 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="只印出結果，不寫資料庫")
    parser.add_argument("--limit", type=int, default=10, help="dry-run 時印幾筆（預設 10）")
    args = parser.parse_args()

    setup_logging()

    if args.list:
        return asyncio.run(list_sources())
    if args.all:
        return asyncio.run(run_all())
    if not args.key:
        parser.error("請指定 extension key，或用 --all / --list")

    try:
        start, end = _parse_date(args.start), _parse_date(args.end)
    except ValueError as exc:
        print(f"日期格式錯誤（要 YYYY-MM-DD）：{exc}", file=sys.stderr)
        return 2

    try:
        if args.dry_run:
            return asyncio.run(dry_run(args.key, start, end, args.limit))
        return asyncio.run(run_one(args.key, start, end))
    except NotFoundError:
        # 打錯 key 是最常見的操作失誤，不需要整串 traceback
        print(f"找不到 extension {args.key!r}", file=sys.stderr)
        print(f"可用的有：{', '.join(registry.keys()) or '（無）'}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中斷", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
