"""IQR 離群排除的樣本數門檻。

回報的狀況：tomato (UG) 送了 1000 / 1050 / 980 / 1100 / 15000 五筆，
看板回 `upper_bound: 1250`、`max_price: 15000`、`exclusions: {}`。
文件寫「區間外視為離群值」，但 15000 明明在區間外卻照樣計入。

原因不是統計算錯，是**門檻**：樣本未達 `INTENT_MIN_SAMPLES_FOR_IQR`（預設 8）
時整段排除邏輯會跳過，可是 `lower_bound` / `upper_bound` 仍照常回報——
等於對外公布了一組沒有在執行的界線。

門檻本身是對的（5 筆的四分位數不具意義，硬濾會錯殺真實需求），
所以修法是讓回應誠實說出「這次沒有濾」，而不是把門檻調低。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.intents import outlier_filter_enabled
from app.services.statistics import iqr_bounds

D = Decimal

# 回報中的那五筆
REPORTED = [D(1000), D(1050), D(980), D(1100), D(15000)]


def test_reported_case_filter_is_inactive() -> None:
    """5 筆未達預設門檻 8，所以不濾——這就是 exclusions 為空的原因。"""
    assert len(REPORTED) < settings.intent_min_samples_for_iqr
    bounds = iqr_bounds(REPORTED, settings.intent_iqr_multiplier)
    assert outlier_filter_enabled(len(REPORTED), bounds) is False


def test_reported_case_bounds_would_have_caught_it() -> None:
    """界線算得出來也算得對，只是沒被執行——這正是誤導呼叫端的地方。"""
    bounds = iqr_bounds(REPORTED, settings.intent_iqr_multiplier)
    assert bounds is not None
    assert not bounds.contains(D(15000))
    assert bounds.contains(D(1050))


def test_filter_activates_at_threshold(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intent_min_samples_for_iqr", 8)
    bounds = iqr_bounds([D(1000)] * 8, settings.intent_iqr_multiplier)
    assert outlier_filter_enabled(7, bounds) is False
    assert outlier_filter_enabled(8, bounds) is True
    assert outlier_filter_enabled(9, bounds) is True


def test_filter_never_runs_without_bounds() -> None:
    """樣本數再多，算不出界線就不能濾。"""
    assert outlier_filter_enabled(100, None) is False


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_tiny_samples_never_filtered(n: int) -> None:
    bounds = iqr_bounds([D(10), D(11), D(12)], settings.intent_iqr_multiplier)
    assert outlier_filter_enabled(n, bounds) is False


def test_summary_reports_the_flag() -> None:
    """IntentSummary 一定要帶著旗標，否則呼叫端無從得知界線沒生效。"""
    from app.services.intents import IntentSummary

    fields = IntentSummary.__dataclass_fields__
    assert "outlier_filter_active" in fields
    assert "min_samples_for_outlier_filter" in fields


def test_schema_exposes_the_flag() -> None:
    from app.schemas.intent import IntentSummaryOut

    assert "outlier_filter_active" in IntentSummaryOut.model_fields
    assert "min_samples_for_outlier_filter" in IntentSummaryOut.model_fields
