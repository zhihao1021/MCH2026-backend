"""Extension 框架的契約測試。

新增 extension 時最容易踩的坑都在這裡：manifest 格式、目錄名與 key
必須一致、`SOURCE` 要匯出、`fetch_prices` 必須是 async generator。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest

from app.core.config import settings
from app.core.errors import NotFoundError
from app.extensions.base import (
    ExtensionConfig,
    FetchWindow,
    PriceSource,
    RawPrice,
    SourceContext,
    SourceManifest,
)
from app.extensions.http import build_client
from app.extensions.registry import ExtensionRegistry, registry
from app.models.enums import IngestStatus
from app.services.ingest import IngestResult, _merge, _split_window

# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_manifest_normalises_case() -> None:
    m = SourceManifest(key="tw_moa", name="X", country_code="tw", currency="twd")
    assert m.country_code == "TW"
    assert m.currency == "TWD"


@pytest.mark.parametrize("bad_key", ["Bad-Key", "1abc", "a", "has space", "UPPER"])
def test_manifest_rejects_bad_keys(bad_key: str) -> None:
    with pytest.raises(ValueError):
        SourceManifest(key=bad_key, name="X", country_code="TW", currency="TWD")


def test_manifest_rejects_bad_cron() -> None:
    with pytest.raises(ValueError):
        SourceManifest(
            key="ok_key", name="X", country_code="TW", currency="TWD", schedule="* * *"
        )


# ---------------------------------------------------------------------------
# RawPrice / FetchWindow
# ---------------------------------------------------------------------------


def test_raw_price_requires_at_least_one_price() -> None:
    with pytest.raises(ValueError):
        RawPrice(market_external_id="M1", product_code="P1", trade_date=date(2026, 9, 1))


def test_effective_avg_prefers_explicit_average() -> None:
    p = RawPrice(
        market_external_id="M1",
        product_code="P1",
        trade_date=date(2026, 9, 1),
        price_avg=Decimal("10"),
        price_high=Decimal("30"),
        price_low=Decimal("2"),
    )
    assert p.effective_avg() == Decimal("10")


def test_effective_avg_falls_back_to_high_low_midpoint() -> None:
    p = RawPrice(
        market_external_id="M1",
        product_code="P1",
        trade_date=date(2026, 9, 1),
        price_high=Decimal("30"),
        price_low=Decimal("10"),
    )
    assert p.effective_avg() == Decimal("20")


def test_raw_price_upper_cases_currency() -> None:
    p = RawPrice(
        market_external_id="M1",
        product_code="P1",
        trade_date=date(2026, 9, 1),
        currency="jpy",
        price_avg=Decimal("1"),
    )
    assert p.currency == "JPY"


def test_fetch_window_rejects_reversed_range() -> None:
    with pytest.raises(ValueError):
        FetchWindow(start=date(2026, 9, 10), end=date(2026, 9, 1))


def test_fetch_window_days_is_inclusive() -> None:
    assert FetchWindow(start=date(2026, 9, 1), end=date(2026, 9, 3)).days == 3


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_discovers_demo_mock() -> None:
    registry.discover(force=True)
    assert "demo_mock" in registry.keys()
    assert registry.errors == []


MISMATCH_SRC = '''
from app.extensions.base import PriceSource, SourceManifest


class S(PriceSource):
    manifest = SourceManifest(
        key="some_other_key", name="X", country_code="TW", currency="TWD"
    )

    async def fetch_prices(self, window):
        yield None


SOURCE = S
'''

CONFIG_SRC = '''
from pydantic import Field

from app.extensions.base import ExtensionConfig, PriceSource, SourceManifest


class C(ExtensionConfig):
    api_key: str = Field(default="")


class S(PriceSource):
    manifest = SourceManifest(
        key="zz_config_test", name="X", country_code="TW", currency="TWD"
    )
    config_model = C

    async def fetch_prices(self, window):
        yield None


SOURCE = S
'''


def test_registry_rejects_key_mismatch(temp_extension) -> None:
    """資料夾名與 manifest.key 不一致時要被擋下，而不是默默載入。"""
    temp_extension("zz_mismatch_test", MISMATCH_SRC)
    reg = ExtensionRegistry()
    reg.discover(force=True)
    assert "zz_mismatch_test" not in reg.keys()
    assert [e.reason for e in reg.errors if e.key == "zz_mismatch_test"] == ["key_mismatch"]


def test_registry_reports_missing_source_attr(temp_extension) -> None:
    temp_extension("zz_nosource_test", "VALUE = 1\n")
    reg = ExtensionRegistry()
    reg.discover(force=True)
    assert "zz_nosource_test" not in reg.keys()
    assert [e.reason for e in reg.errors if e.key == "zz_nosource_test"] == [
        "missing_source_attr"
    ]


def test_registry_rejects_non_price_source(temp_extension) -> None:
    temp_extension("zz_badtype_test", "SOURCE = 42\n")
    reg = ExtensionRegistry()
    reg.discover(force=True)
    assert [e.reason for e in reg.errors if e.key == "zz_badtype_test"] == [
        "invalid_source_attr"
    ]


def test_registry_reports_broken_import(temp_extension) -> None:
    temp_extension("zz_broken_test", "raise RuntimeError('boom')\n")
    reg = ExtensionRegistry()
    reg.discover(force=True)
    assert [e.reason for e in reg.errors if e.key == "zz_broken_test"] == ["import_failed"]


def test_registry_reports_invalid_config(temp_extension, monkeypatch) -> None:
    """EXTENSIONS_CONFIG 給了 extension 不認得的欄位時要明確報錯。"""
    temp_extension("zz_config_test", CONFIG_SRC)
    monkeypatch.setattr(settings, "extensions_config", {"zz_config_test": {"nope": 1}})
    reg = ExtensionRegistry()
    reg.discover(force=True)
    assert [e.reason for e in reg.errors if e.key == "zz_config_test"] == ["invalid_config"]


def test_registry_honours_disabled_list(monkeypatch) -> None:
    monkeypatch.setattr(settings, "extensions_disabled", ["demo_mock"])
    reg = ExtensionRegistry()
    reg.discover(force=True)
    assert "demo_mock" not in reg.keys()


def test_registry_honours_enabled_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(settings, "extensions_enabled", ["nothing_here"])
    reg = ExtensionRegistry()
    reg.discover(force=True)
    assert reg.keys() == []


def test_unknown_extension_raises() -> None:
    registry.discover(force=True)
    with pytest.raises(NotFoundError):
        registry.get("does_not_exist")


# ---------------------------------------------------------------------------
# demo_mock
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_demo_mock_is_deterministic() -> None:
    registry.discover(force=True)
    ext = registry.get("demo_mock")
    window = FetchWindow(start=date(2026, 9, 14), end=date(2026, 9, 16))

    async def collect() -> list[RawPrice]:
        client = build_client()
        try:
            source = ext.instantiate(client)
            return [r async for r in source.fetch_prices(window)]
        finally:
            await client.aclose()

    first, second = await collect(), await collect()
    assert len(first) == 30  # 3 個工作日 x 2 市場 x 5 品項
    assert [r.price_avg for r in first] == [r.price_avg for r in second]


@pytest.mark.anyio
async def test_demo_mock_skips_sundays() -> None:
    registry.discover(force=True)
    ext = registry.get("demo_mock")
    # 2026-09-20 是星期日
    window = FetchWindow(start=date(2026, 9, 20), end=date(2026, 9, 20))
    client = build_client()
    try:
        source = ext.instantiate(client)
        rows = [r async for r in source.fetch_prices(window)]
    finally:
        await client.aclose()
    assert rows == []


@pytest.mark.anyio
async def test_demo_mock_config_filters_markets(monkeypatch) -> None:
    monkeypatch.setattr(settings, "extensions_config", {"demo_mock": {"markets": ["DM01"]}})
    reg = ExtensionRegistry()
    reg.discover(force=True)
    client = build_client()
    try:
        source = reg.get("demo_mock").instantiate(client)
        markets = await source.fetch_markets()
    finally:
        await client.aclose()
    assert [m.external_id for m in markets] == ["DM01"]


@pytest.mark.anyio
async def test_custom_source_can_be_registered() -> None:
    """第三方 extension 不必放進目錄也能被測試。"""

    class MySource(PriceSource):
        manifest = SourceManifest(
            key="unit_test_src", name="Unit Test", country_code="JP", currency="JPY"
        )

        async def fetch_prices(self, window: FetchWindow) -> AsyncIterator[RawPrice]:
            yield RawPrice(
                market_external_id="TOKYO",
                product_code="X1",
                trade_date=window.start,
                price_avg=Decimal("100"),
            )

    reg = ExtensionRegistry()
    reg.register(MySource)
    ext = reg.get("unit_test_src")
    assert ext.manifest.currency == "JPY"

    client = build_client()
    try:
        src = ext.instantiate(client)
        window = FetchWindow(start=date(2026, 9, 1), end=date(2026, 9, 1))
        rows = [r async for r in src.fetch_prices(window)]
    finally:
        await client.aclose()
    # 沒指定幣別時留 None，由 ingest 依 manifest 補上
    assert len(rows) == 1 and rows[0].currency is None


def test_price_source_cannot_be_instantiated_without_fetch_prices() -> None:
    class Incomplete(PriceSource):
        manifest = SourceManifest(
            key="incomplete_src", name="X", country_code="TW", currency="TWD"
        )

    ctx = SourceContext(
        key="incomplete_src",
        config=ExtensionConfig(),
        http=build_client(),
        logger=logging.getLogger("test"),
    )
    with pytest.raises(TypeError):
        Incomplete(ctx)  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# ingest 工具
# ---------------------------------------------------------------------------


def test_split_window_respects_max_days() -> None:
    chunks = _split_window(date(2026, 1, 1), date(2026, 1, 10), 3)
    assert chunks == [
        (date(2026, 1, 1), date(2026, 1, 3)),
        (date(2026, 1, 4), date(2026, 1, 6)),
        (date(2026, 1, 7), date(2026, 1, 9)),
        (date(2026, 1, 10), date(2026, 1, 10)),
    ]


def test_split_window_single_day() -> None:
    assert _split_window(date(2026, 1, 1), date(2026, 1, 1), 31) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]


def test_merge_accumulates_counters_and_reasons() -> None:
    total = IngestResult(
        source_key="k",
        status=IngestStatus.SUCCESS,
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 1),
    )
    part = IngestResult(
        source_key="k",
        status=IngestStatus.SUCCESS,
        window_start=date(2026, 1, 2),
        window_end=date(2026, 1, 5),
        fetched=10,
        written=8,
        skipped=2,
        skip_reasons={"no_price": 2},
    )
    _merge(total, part)
    _merge(total, part)
    assert total.fetched == 20
    assert total.written == 16
    assert total.skip_reasons == {"no_price": 4}
    assert total.window_end == date(2026, 1, 5)
