"""強健統計（robust statistics）。

意向價格是使用者自由填的，一筆惡意的 1 元就能把算術平均拉垮。
所以公開的錨點價格一律用**中位數**或**截尾均值**，並先用 IQR 把離群值剔掉。

這裡全部是純函式，不碰資料庫也不碰 ORM——統計邏輯是最該被單獨測的部分，
不應該混在查詢裡。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Quartiles:
    q1: Decimal
    q2: Decimal  # 中位數
    q3: Decimal

    @property
    def iqr(self) -> Decimal:
        return self.q3 - self.q1


@dataclass(frozen=True)
class OutlierBounds:
    """IQR 法的容許區間。落在區間外的視為離群值。"""

    lower: Decimal
    upper: Decimal

    def contains(self, value: Decimal) -> bool:
        return self.lower <= value <= self.upper


def _to_decimal(values: Sequence[Decimal | float | int]) -> list[Decimal]:
    return [v if isinstance(v, Decimal) else Decimal(str(v)) for v in values]


def median(values: Sequence[Decimal | float | int]) -> Decimal | None:
    """中位數。偶數筆取中間兩個的平均。"""
    data = sorted(_to_decimal(values))
    n = len(data)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2


def quartiles(values: Sequence[Decimal | float | int]) -> Quartiles | None:
    """四分位數，用線性內插（與 numpy 的預設 'linear' 一致）。

    樣本太少時四分位數沒有意義，但仍回傳可用的值——
    要不要因為樣本太少而不做過濾，由呼叫端決定。
    """
    data = sorted(_to_decimal(values))
    n = len(data)
    if n == 0:
        return None
    return Quartiles(
        q1=_percentile(data, Decimal("0.25")),
        q2=_percentile(data, Decimal("0.5")),
        q3=_percentile(data, Decimal("0.75")),
    )


def _percentile(sorted_data: list[Decimal], fraction: Decimal) -> Decimal:
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    pos = (Decimal(n) - 1) * fraction
    low = int(pos)
    high = min(low + 1, n - 1)
    weight = pos - Decimal(low)
    return sorted_data[low] + (sorted_data[high] - sorted_data[low]) * weight


def iqr_bounds(
    values: Sequence[Decimal | float | int], multiplier: float = 1.5
) -> OutlierBounds | None:
    """容許區間 [Q1 - k*IQR, Q3 + k*IQR]，預設 k = 1.5。"""
    q = quartiles(values)
    if q is None:
        return None
    k = Decimal(str(multiplier))
    return OutlierBounds(lower=q.q1 - k * q.iqr, upper=q.q3 + k * q.iqr)


# 樣本少於這個數就不截尾：截完剩不到 3 筆的平均沒有意義
MIN_SAMPLES_FOR_TRIM = 5
# 截尾後至少要留下的筆數
MIN_SAMPLES_KEPT = 3


def trimmed_mean(
    values: Sequence[Decimal | float | int], trim_fraction: float = 0.1
) -> Decimal | None:
    """截尾均值：前後各去掉 `trim_fraction` 比例後取平均。

    筆數用無條件捨去，但**至少去掉 1 筆**——否則樣本一少（例如 8 筆的 10%
    捨去後是 0）就完全不截，退化成算術平均，失去截尾的意義。
    PRD 要求「移除前後各 10%～20%」，8 筆去掉 1 筆是 12.5%，仍在範圍內。

    樣本不足 `MIN_SAMPLES_FOR_TRIM` 筆時不截尾（剩太少的平均沒意義），
    截尾也不會讓剩下的少於 `MIN_SAMPLES_KEPT` 筆。
    """
    data = sorted(_to_decimal(values))
    n = len(data)
    if n == 0:
        return None

    cut = 0
    if n >= MIN_SAMPLES_FOR_TRIM:
        cut = max(1, int(n * trim_fraction))
        # 兩邊都要砍，別把樣本砍到不夠用
        while cut > 0 and n - 2 * cut < MIN_SAMPLES_KEPT:
            cut -= 1

    kept = data[cut : n - cut] if cut else data
    return sum(kept) / Decimal(len(kept))


def mean(values: Sequence[Decimal | float | int]) -> Decimal | None:
    data = _to_decimal(values)
    if not data:
        return None
    return sum(data) / Decimal(len(data))


def stdev(values: Sequence[Decimal | float | int]) -> Decimal | None:
    """母體標準差。樣本少於 2 筆時回 None。"""
    data = _to_decimal(values)
    n = len(data)
    if n < 2:
        return None
    avg = sum(data) / Decimal(n)
    variance = sum((x - avg) ** 2 for x in data) / Decimal(n)
    return Decimal(str(math.sqrt(float(variance))))


def weighted_median(
    pairs: Sequence[tuple[Decimal | float | int, float]],
) -> Decimal | None:
    """加權中位數：累積權重首次達到總權重一半的那個值。

    信譽高的使用者影響力較大，但**不會**像加權平均那樣被單一極端值拉走——
    權重再高也只能把中位數推到相鄰的樣本，這是選中位數而非平均的主因。
    """
    data = sorted(
        ((v if isinstance(v, Decimal) else Decimal(str(v)), w) for v, w in pairs if w > 0),
        key=lambda p: p[0],
    )
    if not data:
        return None
    total = sum(w for _, w in data)
    if total <= 0:
        return None

    half = total / 2
    cumulative = 0.0
    for index, (value, weight) in enumerate(data):
        cumulative += weight
        if cumulative > half:
            return value
        if math.isclose(cumulative, half, rel_tol=1e-9):
            # 剛好落在分界上，取與下一個值的平均，行為與無權重的中位數一致
            nxt = data[index + 1][0] if index + 1 < len(data) else value
            return (value + nxt) / 2
    return data[-1][0]


def within_band(value: Decimal, anchor: Decimal, band_pct: float) -> bool:
    """`value` 是否落在 `anchor` 的 ±band_pct% 內。錨點為 0 時只有相等才算。"""
    if anchor == 0:
        return value == 0
    band = abs(anchor) * Decimal(str(band_pct)) / Decimal(100)
    return abs(value - anchor) <= band


def sigma_distance(value: Decimal, avg: Decimal, sd: Decimal | None) -> float | None:
    """`value` 偏離平均幾個標準差。標準差為 0 或 None 時回 None。"""
    if sd is None or sd == 0:
        return None
    return float(abs(value - avg) / sd)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """兩點間的大圓距離（公里）。地理圍欄用。"""
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lat = p2 - p1
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
