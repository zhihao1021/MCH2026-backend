"""消費者回報的零售價。

這些測試不碰資料庫（見 conftest 的說明），所以聚焦在純邏輯：
店名正規化、單位價換算、以及與意向價格**刻意不同**的那幾個決定。
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models.enums import RetailExclusion, StoreType
from app.services import retail as rs
from app.services.intents import ratio_guard_bounds

D = Decimal


# ---------------------------------------------------------------------------
# 店名正規化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("Big Bazaar", None), ("big bazaar", None)),
        (("Big  Bazaar", None), ("Big Bazaar", None)),
        (("  Big Bazaar  ", None), ("Big Bazaar", None)),
        (("BigBazaar", "Koramangala"), ("bigbazaar", "koramangala")),
    ],
)
def test_store_key_treats_these_as_the_same_store(a, b) -> None:
    """冷卻期綁在 store_key 上，正規化不夠就等於改個大小寫即可重複灌。"""
    assert rs.store_key_of(*a) == rs.store_key_of(*b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("Big Bazaar", None), ("Big Bazaar", "Koramangala")),
        (("Big Bazaar", "Indiranagar"), ("Big Bazaar", "Koramangala")),
        (("Reliance Fresh", None), ("More Supermarket", None)),
    ],
)
def test_store_key_keeps_different_branches_apart(a, b) -> None:
    """不同分店是不同店家——同一個人在兩家分店看到不同價格都該收。"""
    assert rs.store_key_of(*a) != rs.store_key_of(*b)


def test_store_key_is_bounded() -> None:
    """欄位長度 160，超長店名不能讓寫入炸掉。"""
    assert len(rs.store_key_of("x" * 500, "y" * 500)) <= 160


# ---------------------------------------------------------------------------
# 與意向價格刻意不同的地方
# ---------------------------------------------------------------------------


def test_no_below_floor_exclusion() -> None:
    """零售價低只是看到特價，不是惡意壓價——擋掉會讓看板失真。"""
    assert not hasattr(RetailExclusion, "BELOW_FLOOR")
    assert "below_floor" not in {e.value for e in RetailExclusion}


def test_submit_takes_no_floor_parameters() -> None:
    sig = inspect.signature(rs.submit)
    assert "floor" not in sig.parameters
    # 反之，這些是零售特有的
    for name in ("store_name", "store_type", "pack_size", "observed_on", "is_promotion"):
        assert name in sig.parameters


def test_cooldown_is_scoped_to_a_store() -> None:
    """意向是一人一筆，零售回報是一人一店一筆。"""
    sig = inspect.signature(rs.cooldown_remaining)
    assert "store_key" in sig.parameters


def test_retail_cooldown_is_much_shorter_than_intent() -> None:
    """超市改價頻繁，沿用意向的 7 天會讓資料永遠是舊的。"""
    assert settings.retail_cooldown_hours < settings.intent_cooldown_days * 24


# ---------------------------------------------------------------------------
# 離群排除：沿用倍率護欄，但不做 IQR
# ---------------------------------------------------------------------------


def test_channel_spread_is_not_treated_as_outlier() -> None:
    """便利商店貴一倍是常態，不能被當成離群值砍掉。

    這正是零售看板不做 IQR 的理由：量販 38、超市 45、便利商店 92 的分布，
    IQR 會把便利商店整個通路判成離群。
    """
    prices = [D(38), D(44), D(45), D(45), D(41), D(92)]
    g = ratio_guard_bounds(prices)
    assert g is not None
    assert g[0] <= D(92) <= g[1]


def test_order_of_magnitude_report_is_excluded() -> None:
    """₹900/kg 的洋蔥不是通路差異，是打錯或亂填。"""
    prices = [D(38), D(44), D(45), D(45), D(41), D(900)]
    g = ratio_guard_bounds(prices)
    assert g is not None
    assert not (g[0] <= D(900) <= g[1])


def test_summary_does_not_use_iqr() -> None:
    src = inspect.getsource(rs.summarise)
    assert "iqr_bounds" not in src
    assert "ratio_guard_bounds" in src


# ---------------------------------------------------------------------------
# 單位價換算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observed", "pack", "expected"),
    [
        ("40.00", "0.5", "80.00"),      # 500g 賣 40 -> 每 kg 80
        ("120.00", "2", "60.00"),
        ("35.00", "1", "35.00"),
        ("35.00", None, "35.00"),       # 沒填包裝就是單位價
        ("10.00", "3", "3.33"),         # 除不盡要四捨五入到分
    ],
)
def test_unit_price_conversion(observed, pack, expected) -> None:
    assert rs.unit_price_of(D(observed), D(pack) if pack else None) == D(expected)


def test_unit_price_ignores_nonsense_pack_size() -> None:
    """0 或負數會讓換算炸開，退回標價本身而不是丟例外——
    schema 已經擋住了，這裡是最後一道防線。"""
    assert rs.unit_price_of(D("35.00"), D(0)) == D("35.00")
    assert rs.unit_price_of(D("35.00"), D(-1)) == D("35.00")


def test_store_types_cover_the_common_channels() -> None:
    values = {e.value for e in StoreType}
    for expected in ("supermarket", "hypermarket", "convenience", "wet_market", "online"):
        assert expected in values
