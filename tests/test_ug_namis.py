"""ug_namis extension 的單元測試（不打網路）。

這個來源是網頁解析，最容易靜默出錯的是欄位對位：
表頭一個市場兩欄（W.P / R.P），數錯一格不會報錯，
只會把 A 市場的零售價記成 B 市場的批發價。所以要測。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from bs4 import BeautifulSoup

from app.extensions.base import ExtensionConfig, FetchWindow, SourceContext
from app.extensions.ug_namis.source import (
    DEFAULT_EXCLUDED_CATEGORIES,
    GRADE_RETAIL,
    GRADE_WHOLESALE,
    UgNamisConfig,
    UgNamisSource,
)

# 仿真實頁面的結構：兩個市場、每個市場兩欄，外加一個不相干的表格在前面，
# 確認靠標題文字定位而不是靠「第幾張表」。
FIXTURE = """
<html><body>
  <h3>Fuel Prices</h3>
  <table><thead><tr><th>Location</th><th>Station</th></tr></thead>
    <tbody><tr><td>Iganga</td><td>Shell</td></tr></tbody></table>

  <h3 class="label">Consumer Market Prices</h3>
  <table>
    <thead>
      <tr><th>Commodity</th><th>Unit</th>
          <th colspan="2">Mbale</th><th colspan="2">Owino</th></tr>
      <tr><th></th><th></th>
          <th>W.P</th><th>R.P</th><th>W.P</th><th>R.P</th></tr>
    </thead>
    <tbody>
      <tr><td>Cereals &gt; Maize Grain</td><td>Kg</td>
          <td>750</td><td>950</td><td>1,100</td><td>1,400</td></tr>
      <tr><td>Root Crops &gt; Cassava - Fresh</td><td>Kg</td>
          <td></td><td></td><td>800</td><td>1,000</td></tr>
      <tr><td>Animal Products &gt; Beef</td><td>Kg</td>
          <td>14,000</td><td>16,000</td><td></td><td></td></tr>
      <tr><td>Vegetables &gt; Tomatoes</td><td>Kg</td>
          <td>-</td><td>0</td><td>850</td><td>1,200</td></tr>
      <tr><td>Plainname</td><td>Kg</td>
          <td>100</td><td></td><td></td><td></td></tr>
    </tbody>
  </table>
</body></html>
"""


def make_source(**config) -> UgNamisSource:
    """建一個不會真的連線的 source。"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=FIXTURE)
    )
    ctx = SourceContext(
        key="ug_namis",
        config=UgNamisConfig(**config),
        http=httpx.AsyncClient(transport=transport),
        logger=logging.getLogger("test.ug_namis"),
    )
    return UgNamisSource(ctx)


def today_in_uganda() -> date:
    return datetime.now(ZoneInfo("Africa/Kampala")).date()


async def collect(source: UgNamisSource, window: FetchWindow | None = None):
    day = today_in_uganda()
    window = window or FetchWindow(start=day, end=day)
    return [r async for r in source.fetch_prices(window)]


# ---------------------------------------------------------------------------
# manifest 與設定
# ---------------------------------------------------------------------------


def test_manifest_basics() -> None:
    m = UgNamisSource.manifest
    assert m.key == "ug_namis"
    assert m.country_code == "UG"
    assert m.currency == "UGX"
    assert m.timezone == "Africa/Kampala"
    assert m.default_unit == "kg"


def test_manifest_does_not_pretend_to_have_history() -> None:
    """網站只有最新快照，往回補是抓不到東西的。"""
    m = UgNamisSource.manifest
    assert m.lookback_days == 0
    assert m.max_window_days == 1


def test_config_defaults_exclude_non_crops() -> None:
    cfg = UgNamisConfig()
    assert tuple(cfg.exclude_categories) == DEFAULT_EXCLUDED_CATEGORIES
    assert cfg.include_retail is True
    assert cfg.markets == []


def test_config_rejects_unknown_field() -> None:
    with pytest.raises(Exception):
        UgNamisConfig(nope=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 表格定位與市場
# ---------------------------------------------------------------------------


def test_finds_the_right_table_not_just_the_first_one() -> None:
    source = make_source()
    table = source._find_table(BeautifulSoup(FIXTURE, "html.parser"))
    assert table is not None
    assert "Commodity" in table.get_text()
    assert "Station" not in table.get_text()   # 不是那張燃料價格表


@pytest.mark.anyio
async def test_fetch_markets() -> None:
    markets = await make_source().fetch_markets()
    assert [m.external_id for m in markets] == ["mbale", "owino"]
    assert [m.name for m in markets] == ["Mbale", "Owino"]
    assert all(m.timezone == "Africa/Kampala" for m in markets)


@pytest.mark.anyio
async def test_markets_can_be_filtered() -> None:
    markets = await make_source(markets=["owino"]).fetch_markets()
    assert [m.external_id for m in markets] == ["owino"]


@pytest.mark.anyio
async def test_markets_carry_a_region() -> None:
    """沒有 region 的市場不會出現在 /v1/markets/regions 的地區選單裡。"""
    markets = await make_source().fetch_markets()
    assert all(m.region for m in markets), [(m.name, m.region) for m in markets]
    regions = {m.external_id: m.region for m in markets}
    assert regions["mbale"] == "Mbale"      # 市場名就是 district 名
    assert regions["owino"] == "Kampala"    # 靠覆蓋表：Owino 市場在坎帕拉


def test_district_resolution_uses_iso_names() -> None:
    """用 ISO 3166-2 的 district 名稱，才不會跟平台其他地方對不起來。"""
    source = make_source()
    assert source._district_for("iganga", "Iganga") == "Iganga"
    assert source._district_for("mbarara", "Mbarara") == "Mbarara"
    # 大小寫與空白不該影響比對
    assert source._district_for("soroti", "  soroti ") == "Soroti"
    # 對不上又沒有覆蓋就留空，而不是亂猜一個
    assert source._district_for("nowhere", "Nowhere Market") is None


def test_every_real_namis_market_resolves_to_a_district() -> None:
    """NAMIS 目前的 9 個市場都要對得到 district，缺一個就少一個地區選項。"""
    source = make_source()
    live_markets = [
        "Fort Portal", "Hoima", "Iganga", "Lira", "Mbale",
        "Mbarara", "Owino", "Soroti", "Tororo",
    ]
    from slugify import slugify

    unresolved = [
        name for name in live_markets
        if source._district_for(slugify(name), name) is None
    ]
    assert unresolved == []


# ---------------------------------------------------------------------------
# 欄位對位：這裡錯了不會報錯，只會把價格記到別的市場去
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_columns_map_to_the_right_market_and_price_type() -> None:
    rows = await collect(make_source())
    maize = {
        (r.market_external_id, r.grade): r.price_avg
        for r in rows if r.product_code == "cereals:maize-grain"
    }
    assert maize == {
        ("mbale", GRADE_WHOLESALE): Decimal("750"),
        ("mbale", GRADE_RETAIL): Decimal("950"),
        ("owino", GRADE_WHOLESALE): Decimal("1100"),   # 千分位要被吃掉
        ("owino", GRADE_RETAIL): Decimal("1400"),
    }


@pytest.mark.anyio
async def test_empty_cells_produce_no_rows() -> None:
    """某個市場當天沒回報就是沒有資料，不能補 0。"""
    rows = await collect(make_source())
    cassava = [r for r in rows if r.product_code == "root-crops:cassava-fresh"]
    assert {r.market_external_id for r in cassava} == {"owino"}


@pytest.mark.anyio
async def test_placeholder_values_are_ignored() -> None:
    """`-` 與 0 都是佔位符，不是真的價格。"""
    rows = await collect(make_source())
    tomatoes = {
        (r.market_external_id, r.grade) for r in rows
        if r.product_code == "vegetables:tomatoes"
    }
    assert tomatoes == {("owino", GRADE_WHOLESALE), ("owino", GRADE_RETAIL)}


@pytest.mark.anyio
async def test_non_crop_categories_are_excluded_by_default() -> None:
    rows = await collect(make_source())
    assert not [r for r in rows if "beef" in r.product_code]
    assert "Animal Products" not in {r.raw["category"] for r in rows}


@pytest.mark.anyio
async def test_non_crops_can_be_switched_back_on() -> None:
    rows = await collect(make_source(exclude_categories=[]))
    beef = [r for r in rows if r.product_code == "animal-products:beef"]
    assert len(beef) == 2
    assert beef[0].price_avg == Decimal("14000")


@pytest.mark.anyio
async def test_retail_can_be_switched_off() -> None:
    rows = await collect(make_source(include_retail=False))
    assert {r.grade for r in rows} == {GRADE_WHOLESALE}


@pytest.mark.anyio
async def test_commodity_without_category_still_works() -> None:
    rows = await collect(make_source())
    plain = [r for r in rows if r.product_code == "plainname"]
    assert len(plain) == 1
    assert plain[0].product_name == "Plainname"
    assert plain[0].raw["category"] == ""


# ---------------------------------------------------------------------------
# 日期
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trade_date_is_today_in_uganda() -> None:
    """頁面上沒有日期，只能記成來源當地的今天——不是伺服器的今天。"""
    rows = await collect(make_source())
    assert {r.trade_date for r in rows} == {today_in_uganda()}


@pytest.mark.anyio
async def test_historical_window_yields_nothing() -> None:
    """與其塞一個假日期，不如明白地讓那一段沒有資料。"""
    source = make_source()
    old = FetchWindow(start=date(2020, 1, 1), end=date(2020, 1, 31))
    assert await collect(source, old) == []


@pytest.mark.anyio
async def test_window_spanning_today_still_works() -> None:
    day = today_in_uganda()
    window = FetchWindow(start=day.replace(day=1), end=day)
    rows = await collect(make_source(), window)
    assert rows and {r.trade_date for r in rows} == {day}


# ---------------------------------------------------------------------------
# 代碼穩定性
# ---------------------------------------------------------------------------


def test_product_code_is_slugified_and_stable() -> None:
    source = make_source()
    assert source._product_code("Cereals", "Maize Grain") == "cereals:maize-grain"
    # 排版變動（大小寫、多餘空白、標點）不該讓代碼跑掉
    assert source._product_code("CEREALS", "  Maize   Grain ") == "cereals:maize-grain"
    assert source._product_code("Root Crops", "Rice - Super") == "root-crops:rice-super"
    assert source._product_code("", "Matooke") == "matooke"


def test_product_code_fits_the_column() -> None:
    source = make_source()
    code = source._product_code("Traditional Cash Crops", "Coffee " * 20)
    assert len(code) <= 80


def test_split_commodity() -> None:
    source = make_source()
    assert source._split_commodity("Cereals > Maize Grain") == ("Cereals", "Maize Grain")
    assert source._split_commodity("  Fruits   >   Pineapples ") == ("Fruits", "Pineapples")
    assert source._split_commodity("Matooke") == ("", "Matooke")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1,750", Decimal("1750")), ("750", Decimal("750")),
     ("1,234.56", Decimal("1234.56")), ("", None), ("-", None),
     ("0", None), ("N/A", None), ("  ", None)],
)
def test_value_parsing(text: str, expected: Decimal | None) -> None:
    assert make_source()._value([text], 0) == expected


def test_value_out_of_range_is_none() -> None:
    assert make_source()._value(["100"], 5) is None
