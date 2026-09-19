"""Demo 國家範圍過濾的測試。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import country_scope
from app.core.config import settings
from app.models.catalog import Market
from app.models.quote import Quote


@pytest.fixture(autouse=True)
def _clear_scope_cache():
    """scope 有 lru_cache，每個測試前後都要清掉，否則會拿到別的測試的設定。"""
    country_scope.reset_scope()
    yield
    country_scope.reset_scope()


def _set(monkeypatch, visible: list[str], hidden: list[str]) -> None:
    monkeypatch.setattr(settings, "demo_visible_countries", visible)
    monkeypatch.setattr(settings, "demo_hidden_countries", hidden)
    country_scope.reset_scope()


# ---------------------------------------------------------------------------
# 預設：不過濾
# ---------------------------------------------------------------------------


def test_inactive_by_default(monkeypatch) -> None:
    _set(monkeypatch, [], [])
    scope = country_scope.get_scope()
    assert scope.is_active is False
    assert scope.allows("TW") is True
    assert scope.allows("UG") is True
    assert scope.allows(None) is True
    assert country_scope.condition(Market.country_code) is None


def test_apply_is_noop_when_inactive(monkeypatch) -> None:
    _set(monkeypatch, [], [])
    stmt = select(Market)
    assert country_scope.apply(stmt, Market.country_code) is stmt


# ---------------------------------------------------------------------------
# 黑名單
# ---------------------------------------------------------------------------


def test_hidden_list(monkeypatch) -> None:
    _set(monkeypatch, [], ["TW"])
    scope = country_scope.get_scope()
    assert scope.is_active is True
    assert scope.allows("TW") is False
    assert scope.allows("UG") is True
    assert scope.allows("JP") is True
    # 沒有國家資訊的，黑名單模式下放行
    assert scope.allows(None) is True


def test_hidden_list_is_case_insensitive(monkeypatch) -> None:
    _set(monkeypatch, [], ["tw"])
    assert country_scope.get_scope().allows("TW") is False
    assert country_scope.get_scope().allows("tw") is False


def test_hidden_multiple(monkeypatch) -> None:
    _set(monkeypatch, [], ["TW", "JP"])
    scope = country_scope.get_scope()
    assert scope.allows("TW") is False
    assert scope.allows("JP") is False
    assert scope.allows("UG") is True


# ---------------------------------------------------------------------------
# 白名單
# ---------------------------------------------------------------------------


def test_visible_list(monkeypatch) -> None:
    _set(monkeypatch, ["UG"], [])
    scope = country_scope.get_scope()
    assert scope.allows("UG") is True
    assert scope.allows("TW") is False
    assert scope.allows("JP") is False
    # 白名單模式下，沒有國家資訊的一律藏起來
    assert scope.allows(None) is False


def test_visible_wins_over_hidden(monkeypatch) -> None:
    """兩個都設了以白名單為準，不然語意會互相打架。"""
    _set(monkeypatch, ["UG"], ["UG", "TW"])
    scope = country_scope.get_scope()
    assert scope.allows("UG") is True
    assert scope.allows("TW") is False


# ---------------------------------------------------------------------------
# SQL 條件
# ---------------------------------------------------------------------------


def test_condition_uses_in_for_visible(monkeypatch) -> None:
    _set(monkeypatch, ["UG"], [])
    sql = str(country_scope.condition(Market.country_code))
    assert "IN" in sql.upper()


def test_condition_uses_not_in_for_hidden(monkeypatch) -> None:
    _set(monkeypatch, [], ["TW"])
    sql = str(country_scope.condition(Market.country_code))
    assert "NOT IN" in sql.upper()


def test_condition_works_on_quote_column(monkeypatch) -> None:
    """報價與市場是不同的表，兩邊都要能套用。"""
    _set(monkeypatch, [], ["TW"])
    assert country_scope.condition(Quote.country_code) is not None


def test_apply_adds_where(monkeypatch) -> None:
    _set(monkeypatch, [], ["TW"])
    stmt = select(Market)
    filtered = country_scope.apply(stmt, Market.country_code)
    assert filtered is not stmt
    assert "WHERE" in str(filtered).upper()


# ---------------------------------------------------------------------------
# describe（給維運看目前設定）
# ---------------------------------------------------------------------------


def test_describe(monkeypatch) -> None:
    _set(monkeypatch, [], ["TW", "jp"])
    d = country_scope.get_scope().describe()
    assert d["active"] is True
    assert d["hidden_countries"] == ["JP", "TW"]
    assert d["visible_countries"] == []
