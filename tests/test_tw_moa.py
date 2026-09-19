"""tw_moa extension 的單元測試（不打網路）。

民國年換算與市場對照表是最容易出錯、又最容易靜默出錯的兩塊：
日期錯一年不會報錯、只會查不到資料；市場漏一個也不會報錯、
只會少收那個市場所有的價格。所以要測。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions.tw_moa.source import (
    _DEFAULT_MARKETS,
    _parse_roc_date,
    _roc,
    TwMoaSource,
)

# 台灣的直轄市 / 縣 / 市，用來檢查對照表填的是真的行政區
VALID_REGIONS = {
    "台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市",
    "基隆市", "新竹市", "嘉義市",
    "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣", "嘉義縣",
    "屏東縣", "宜蘭縣", "花蓮縣", "台東縣", "澎湖縣", "金門縣", "連江縣",
}


# ---------------------------------------------------------------------------
# 民國年
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 9, 3), "115.09.03"),
        (date(2026, 12, 31), "115.12.31"),
        (date(2011, 1, 1), "100.01.01"),
        (date(1912, 1, 1), "1.01.01"),
    ],
)
def test_roc_format(day: date, expected: str) -> None:
    assert _roc(day) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("115.09.16", date(2026, 9, 16)),
        ("115.9.6", date(2026, 9, 6)),       # 不補零
        ("104.12.31", date(2015, 12, 31)),
        ("115/09/16", date(2026, 9, 16)),    # 換個分隔符
        ("2026.09.16", date(2026, 9, 16)),   # 已經是西元年就原樣用
    ],
)
def test_parse_roc_date(raw: str, expected: date) -> None:
    assert _parse_roc_date(raw) == expected


@pytest.mark.parametrize("bad", ["", "115.09", "abc", "115.09.16.01"])
def test_parse_roc_date_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_roc_date(bad)


def test_roc_roundtrip() -> None:
    for day in (date(2026, 1, 1), date(2026, 6, 15), date(2026, 12, 31)):
        assert _parse_roc_date(_roc(day)) == day


# ---------------------------------------------------------------------------
# 市場對照表
# ---------------------------------------------------------------------------


def test_every_market_has_name_and_region() -> None:
    """region 只能靠這張表補——MOA 的價格 API 沒有縣市欄位。

    漏填會讓 markets.region 是 NULL，前端的「選地區」就少一個市場。
    """
    for code, value in _DEFAULT_MARKETS.items():
        assert isinstance(value, tuple) and len(value) == 2, f"{code} 的值格式不對"
        name, region = value
        assert name, f"{code} 沒有名稱"
        assert region, f"{code} 沒有縣市"


def test_regions_are_real_administrative_areas() -> None:
    for code, (_, region) in _DEFAULT_MARKETS.items():
        assert region in VALID_REGIONS, f"{code} 的縣市 {region!r} 不是有效的行政區"


def test_market_codes_are_digit_strings() -> None:
    for code in _DEFAULT_MARKETS:
        assert code.isdigit(), f"市場代碼 {code!r} 應該是數字字串"


def test_known_markets_present() -> None:
    """這些是實際觀測到有交易的市場，少一個就會整個市場漏抓。"""
    observed = {
        "104", "105", "109", "220", "241", "260", "338", "400", "420", "423",
        "512", "514", "540", "600", "648", "700", "800", "830", "930", "950",
    }
    missing = observed - set(_DEFAULT_MARKETS)
    assert not missing, f"對照表缺少已知市場：{sorted(missing)}"


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_manifest_basics() -> None:
    m = TwMoaSource.manifest
    assert m.key == "tw_moa"
    assert m.country_code == "TW"
    assert m.currency == "TWD"
    assert m.timezone == "Asia/Taipei"
    assert m.default_unit == "kg"
    # MOA 會回頭訂正數字，lookback 太小會永遠拿到第一版
    assert m.lookback_days >= 3


def test_config_defaults() -> None:
    cfg = TwMoaSource.config_model()
    assert cfg.api_key == ""
    assert cfg.markets == []
    assert cfg.request_delay >= 0


def test_config_rejects_unknown_field() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TwMoaSource.config_model(nope=1)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _make_source() -> TwMoaSource:
    import logging

    from app.extensions.base import SourceContext
    from app.extensions.http import build_client

    return TwMoaSource(
        SourceContext(
            key="tw_moa",
            config=TwMoaSource.config_model(),
            http=build_client(),
            logger=logging.getLogger("test"),
        )
    )


def test_parse_row() -> None:
    src = _make_source()
    row = {
        "TransDate": "115.09.16", "TcType": "N05", "CropCode": "11",
        "CropName": "椰子", "MarketCode": "104", "MarketName": "台北二",
        "Upper_Price": 27.8, "Middle_Price": 15.4, "Lower_Price": 12.7,
        "Avg_Price": 17.4, "Trans_Quantity": 1010.0,
    }
    price = src._parse(row)
    assert price is not None
    assert price.trade_date == date(2026, 9, 16)
    assert price.market_external_id == "104"
    assert price.product_code == "11"
    assert price.product_name == "椰子"
    assert price.price_avg == Decimal("17.4")
    assert price.unit == "kg"
    assert price.grade == ""          # 沒有分級時必須是空字串，不是 None
    assert price.raw == row


def test_parse_skips_all_zero_prices() -> None:
    src = _make_source()
    row = {
        "TransDate": "115.09.16", "CropCode": "11", "MarketCode": "104",
        "Upper_Price": 0, "Middle_Price": 0, "Lower_Price": 0, "Avg_Price": 0,
    }
    assert src._parse(row) is None


def test_parse_skips_missing_crop_code() -> None:
    src = _make_source()
    row = {"TransDate": "115.09.16", "MarketCode": "104", "Avg_Price": 10}
    assert src._parse(row) is None


def test_parse_survives_broken_row() -> None:
    """單筆壞掉要回 None 讓上層跳過，不能讓整批 ingest 陣亡。"""
    src = _make_source()
    assert src._parse({"TransDate": "not-a-date", "CropCode": "11", "Avg_Price": 1}) is None
    assert src._parse({}) is None
