"""強健統計的測試。

這些函式決定公開的錨點價格，算錯就是整個防刷機制失效，
所以連邊界條件都要測。
"""

from __future__ import annotations

import statistics as pystat
from decimal import Decimal

import pytest

from app.services.statistics import (
    MIN_SAMPLES_FOR_TRIM,
    haversine_km,
    iqr_bounds,
    mean,
    median,
    quartiles,
    sigma_distance,
    stdev,
    trimmed_mean,
    weighted_median,
    within_band,
)

D = Decimal


# ---------------------------------------------------------------------------
# 中位數
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], None),
        ([5], D(5)),
        ([1, 2, 3], D(2)),
        ([1, 2, 3, 4], D("2.5")),
        ([3, 1, 2], D(2)),          # 未排序也要對
        ([1, 1, 1, 100], D(1)),     # 極端值不影響
    ],
)
def test_median(values, expected) -> None:
    assert median(values) == expected


def test_median_matches_stdlib() -> None:
    for values in ([1, 5, 2, 8, 7], [4, 2], [10, 20, 30, 40, 50, 60]):
        assert float(median(values)) == pytest.approx(pystat.median(values))


def test_median_resists_extreme_value() -> None:
    """這是選中位數而不是平均的整個理由。"""
    honest = [20, 21, 22, 23, 24]
    attacked = honest + [999999]
    assert median(attacked) == D("22.5")      # 幾乎沒動
    assert mean(attacked) > D(100000)         # 平均整個被拉走


# ---------------------------------------------------------------------------
# 四分位數與 IQR
# ---------------------------------------------------------------------------


def test_quartiles_linear_interpolation() -> None:
    q = quartiles([1, 2, 3, 4, 5])
    assert q is not None
    assert (q.q1, q.q2, q.q3) == (D(2), D(3), D(4))
    assert q.iqr == D(2)


def test_quartiles_single_value() -> None:
    q = quartiles([7])
    assert q is not None and q.q1 == q.q2 == q.q3 == D(7)


def test_quartiles_empty() -> None:
    assert quartiles([]) is None


def test_iqr_bounds_flags_outlier() -> None:
    values = [10, 12, 13, 14, 15, 16, 18, 100]
    b = iqr_bounds(values)
    assert b is not None
    assert not b.contains(D(100))
    assert b.contains(D(14))


def test_iqr_multiplier_widens_band() -> None:
    # Q1=12.75, Q3=16.5, IQR=3.75
    # k=1.5 -> 上界 22.125（40 是離群）；k=8 -> 上界 46.5（40 進得來）
    values = [10, 12, 13, 14, 15, 16, 18, 40]
    tight = iqr_bounds(values, 1.5)
    loose = iqr_bounds(values, 8.0)
    assert tight is not None and loose is not None
    assert loose.upper > tight.upper
    assert not tight.contains(D(40))
    assert loose.contains(D(40))


def test_iqr_bounds_identical_values() -> None:
    """全部一樣時 IQR 為 0，容許區間縮成一點——不能因此把正常值當離群。"""
    b = iqr_bounds([30, 30, 30, 30])
    assert b is not None and b.lower == b.upper == D(30)
    assert b.contains(D(30))


# ---------------------------------------------------------------------------
# 截尾均值
# ---------------------------------------------------------------------------


def test_trimmed_mean_removes_extremes() -> None:
    values = [10, 12, 13, 14, 15, 16, 18, 100]
    plain = mean(values)
    trimmed = trimmed_mean(values, 0.1)
    assert trimmed is not None and plain is not None
    assert trimmed < plain
    # 去掉頭尾各一筆後是 12..18 的平均
    assert trimmed == pytest.approx(D(sum([12, 13, 14, 15, 16, 18])) / 6)


def test_trimmed_mean_always_cuts_at_least_one() -> None:
    """8 筆的 10% 無條件捨去是 0，不能因此退化成算術平均。"""
    values = [10, 12, 13, 14, 15, 16, 18, 100]
    assert trimmed_mean(values, 0.1) != mean(values)


def test_trimmed_mean_skips_small_samples() -> None:
    values = [1, 2, 3, 100]
    assert len(values) < MIN_SAMPLES_FOR_TRIM
    assert trimmed_mean(values, 0.1) == mean(values)


def test_trimmed_mean_keeps_minimum_samples() -> None:
    """大比例截尾也不能把樣本砍光。"""
    values = [1, 2, 3, 4, 5]
    assert trimmed_mean(values, 0.4) is not None


def test_trimmed_mean_empty() -> None:
    assert trimmed_mean([]) is None


# ---------------------------------------------------------------------------
# 加權中位數
# ---------------------------------------------------------------------------


def test_weighted_median_equals_median_when_uniform() -> None:
    values = [10, 20, 30, 40, 50]
    assert weighted_median([(v, 1.0) for v in values]) == median(values)


def test_weighted_median_follows_weight() -> None:
    """信譽高的人把錨點拉向自己，但只能拉到相鄰樣本。"""
    pairs = [(10, 10.0), (20, 1.0), (30, 1.0), (40, 1.0)]
    assert weighted_median(pairs) == D(10)


def test_weighted_median_ignores_zero_weight() -> None:
    """權重 0（例如被降權的刷票者）完全不影響結果。"""
    honest = [(20, 1.0), (21, 1.0), (22, 1.0)]
    assert weighted_median(honest + [(999999, 0.0)]) == weighted_median(honest)


def test_weighted_median_all_zero_weight() -> None:
    assert weighted_median([(10, 0.0), (20, 0.0)]) is None


def test_weighted_median_empty() -> None:
    assert weighted_median([]) is None


# ---------------------------------------------------------------------------
# 離散程度
# ---------------------------------------------------------------------------


def test_stdev_needs_two_samples() -> None:
    assert stdev([]) is None
    assert stdev([5]) is None
    assert stdev([5, 5]) == 0


def test_stdev_matches_stdlib_population() -> None:
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    assert float(stdev(values)) == pytest.approx(pystat.pstdev(values))


def test_sigma_distance() -> None:
    assert sigma_distance(D(10), D(0), D(5)) == pytest.approx(2.0)
    assert sigma_distance(D(10), D(0), D(0)) is None
    assert sigma_distance(D(10), D(0), None) is None


# ---------------------------------------------------------------------------
# 共識區間
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "anchor", "band", "expected"),
    [
        (D(100), D(100), 15, True),
        (D(115), D(100), 15, True),     # 剛好在邊界上
        (D(116), D(100), 15, False),
        (D(85), D(100), 15, True),
        (D(84), D(100), 15, False),
        (D(0), D(0), 15, True),         # 錨點為 0 的邊界
        (D(1), D(0), 15, False),
    ],
)
def test_within_band(value, anchor, band, expected) -> None:
    assert within_band(value, anchor, band) is expected


# ---------------------------------------------------------------------------
# 地理距離
# ---------------------------------------------------------------------------


def test_haversine_same_point() -> None:
    assert haversine_km(25.0, 121.0, 25.0, 121.0) == pytest.approx(0.0)


def test_haversine_known_distance() -> None:
    # 台北車站 -> 高雄車站，實際約 296 km
    d = haversine_km(25.0478, 121.5170, 22.6395, 120.3020)
    assert 290 < d < 305


def test_haversine_is_symmetric() -> None:
    a = haversine_km(1.0827, 34.175, 0.6093, 33.4686)
    b = haversine_km(0.6093, 33.4686, 1.0827, 34.175)
    assert a == pytest.approx(b)


def test_haversine_within_geofence() -> None:
    """15 公里圍欄：同一個城市內應該落在範圍內。"""
    # 台北車站 -> 松山機場，約 5 km
    assert haversine_km(25.0478, 121.5170, 25.0697, 121.5520) < 15
