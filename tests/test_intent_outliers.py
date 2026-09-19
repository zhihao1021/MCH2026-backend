"""意向價格的離群排除：倍率護欄 + IQR 兩層。

回報經過兩輪：

1. tomato (UG) 送 1000 / 1050 / 980 / 1100 / 15000 五筆，看板回
   `upper_bound: 1250`、`max_price: 15000`、`exclusions: {}`。
2. 修掉回報欄位後仍然沒排除，因為第一版只是讓「沒有濾」這件事可見，
   並沒有改變行為。

真正的缺口是：IQR 需要 8 筆才啟用，所以樣本少的區域完全沒有防線。
補法不是把 IQR 門檻調低（五筆的四分位數不可靠，會誤殺真實價差），
而是加一層以中位數為基準的倍率護欄——中位數本身就抗極端值，
三筆就能用，而且只擋量級層次的離譜值。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.intents import iqr_filter_enabled, ratio_guard_bounds
from app.services.statistics import iqr_bounds, median

D = Decimal


def _kept(values: list[Decimal]) -> list[Decimal]:
    """套用倍率護欄後留下的值。"""
    g = ratio_guard_bounds(values)
    if g is None:
        return list(values)
    return [v for v in values if g[0] <= v <= g[1]]


# ---------------------------------------------------------------------------
# 回報的兩個案例
# ---------------------------------------------------------------------------


def test_reported_case_one_is_now_excluded() -> None:
    """1000/1050/980/1100/15000 —— 15000 是中位數 1050 的 14 倍。"""
    values = [D(1000), D(1050), D(980), D(1100), D(15000)]
    assert len(values) < settings.intent_min_samples_for_iqr   # IQR 仍然不啟用
    kept = _kept(values)
    assert D(15000) not in kept
    assert len(kept) == 4
    assert median(kept) == D(1025)


def test_reported_case_two_is_now_excluded() -> None:
    """第二輪回報：錨點 850、upper_bound 1020，卻混進一筆 12000。"""
    values = [D(800), D(850), D(900), D(1000), D(12000)]
    kept = _kept(values)
    assert D(12000) not in kept
    assert median(kept) == D(875)


# ---------------------------------------------------------------------------
# 護欄本身
# ---------------------------------------------------------------------------


def test_ratio_guard_needs_three_samples() -> None:
    assert ratio_guard_bounds([D(100), D(9999)]) is None
    assert ratio_guard_bounds([D(100), D(110), D(9999)]) is not None


def test_ratio_guard_is_centred_on_median() -> None:
    g = ratio_guard_bounds([D(100), D(100), D(100)])
    assert g == (D(100) / D("5.0"), D(100) * D("5.0"))


def test_ratio_guard_catches_both_directions() -> None:
    """灌低價跟灌高價一樣要擋——壓低錨點也是操縱。"""
    values = [D(1000), D(1000), D(1000), D(1), D(100000)]
    kept = _kept(values)
    assert kept == [D(1000), D(1000), D(1000)]


def test_ratio_guard_keeps_legitimate_spread() -> None:
    """品質價差、產地價差不該被誤殺：3 倍以內全部留下。"""
    values = [D(500), D(800), D(1000), D(1200), D(1500)]
    assert _kept(values) == values


def test_ratio_guard_boundary_is_inclusive() -> None:
    """剛好 5 倍留下，超過一點才排除。"""
    values = [D(100), D(100), D(100), D(500)]
    assert D(500) in _kept(values)
    values = [D(100), D(100), D(100), D("500.01")]
    assert D("500.01") not in _kept(values)


def test_ratio_guard_ignores_nonpositive_median() -> None:
    """中位數為 0 算不出比例，這時不能濾（否則會把全部砍光）。"""
    assert ratio_guard_bounds([D(0), D(0), D(0)]) is None


def test_ratio_guard_inverts_once_attackers_hold_the_majority() -> None:
    """**已知限制**，明確寫成測試免得日後誤以為是 bug。

    護欄的基準是中位數，所以攻擊者只要佔過半就能把基準整個搬走，
    這時被排除的反而是誠實的那一邊。任何以中位數為錨的機制都有這個
    性質，多加一層倍率護欄並不會改善它。

    過半灌票不是離群值問題，防線在別處：信譽權重、冷卻期、影子封禁、
    機房 IP 偵測——那些擋的是「同一個人灌很多筆」。
    """
    values = [D(100), D(100), D(5000), D(5000), D(5000)]
    kept = _kept(values)
    assert kept == [D(5000), D(5000), D(5000)]
    assert D(100) not in kept


# ---------------------------------------------------------------------------
# 兩層的互動
# ---------------------------------------------------------------------------


def test_guard_runs_before_iqr_so_bounds_are_not_inflated() -> None:
    """護欄必須先跑。

    單獨一筆極端值其實撼動不了四分位數，IQR 自己擋得住。會出問題的是
    **協同灌水**：兩筆以上的極端值一起把 Q3 撐高、容許區間跟著放寬，
    幅度較小的那筆（這裡是 2500）就能躲過 IQR 混進樣本。

    先過倍率護欄砍掉量級層次的假值，IQR 才會在乾淨的分布上判斷。
    """
    values = [D(x) for x in (980, 1000, 1020, 1050, 1080, 1100, 2500, 15000, 16000)]

    naive = iqr_bounds(values, settings.intent_iqr_multiplier)
    guarded = iqr_bounds(_kept(values), settings.intent_iqr_multiplier)
    assert naive is not None and guarded is not None

    assert naive.contains(D(2500))        # 沒先過護欄時 2500 混得過去
    assert not guarded.contains(D(2500))  # 砍掉 15000/16000 之後就露餡
    assert guarded.upper < naive.upper


def test_single_extreme_value_does_not_need_the_ordering() -> None:
    """對照組：只有一筆極端值時 IQR 本來就不會被撐開。

    寫下來是為了標清楚護欄真正的守備範圍——它補的是協同灌水，
    不是「IQR 擋不住單一離群值」。
    """
    values = [D(x) for x in (980, 1000, 1020, 1050, 1080, 1100, 1120, 2500, 15000)]
    naive = iqr_bounds(values, settings.intent_iqr_multiplier)
    assert naive is not None
    assert not naive.contains(D(2500))


def test_iqr_still_gated_on_sample_count() -> None:
    """護欄補的是樣本不足時的缺口，不是拿來取代 IQR 的門檻。"""
    bounds = iqr_bounds([D(1000)] * 8, settings.intent_iqr_multiplier)
    assert iqr_filter_enabled(settings.intent_min_samples_for_iqr - 1, bounds) is False
    assert iqr_filter_enabled(settings.intent_min_samples_for_iqr, bounds) is True


def test_iqr_never_runs_without_bounds() -> None:
    assert iqr_filter_enabled(100, None) is False


# ---------------------------------------------------------------------------
# 對外契約
# ---------------------------------------------------------------------------


def test_summary_reports_whether_filtering_ran() -> None:
    from app.services.intents import IntentSummary

    fields = IntentSummary.__dataclass_fields__
    assert "outlier_filter_active" in fields
    assert "min_samples_for_outlier_filter" in fields


def test_schema_exposes_the_flag() -> None:
    from app.schemas.intent import IntentSummaryOut

    assert "outlier_filter_active" in IntentSummaryOut.model_fields
    assert "min_samples_for_outlier_filter" in IntentSummaryOut.model_fields


def test_protection_starts_below_the_iqr_threshold() -> None:
    """對外公布的門檻要是「開始有防護」的那個，不是 IQR 的。"""
    assert settings.intent_min_samples_for_ratio_guard < settings.intent_min_samples_for_iqr
