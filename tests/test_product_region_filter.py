"""品項搜尋的產地過濾。

只驗證 SQL 組得對不對（不連資料庫）；實際筆數由
`scripts/smoke_test.py` 對真實資料驗。
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.core import country_scope
from app.core.config import settings
from app.services.catalog import search_products


@pytest.fixture(autouse=True)
def _clear_scope_cache():
    country_scope.reset_scope()
    yield
    country_scope.reset_scope()


def test_signature_accepts_location_filters() -> None:
    sig = inspect.signature(search_products)
    for name in ("region", "country_code", "market_id"):
        assert name in sig.parameters, f"search_products 少了 {name} 參數"
        assert sig.parameters[name].default is None


def test_filters_are_keyword_only() -> None:
    """全部是 keyword-only，加參數時不會因為位置錯亂而傳錯。"""
    sig = inspect.signature(search_products)
    for name in ("region", "country_code", "market_id"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# 子查詢的形狀
# ---------------------------------------------------------------------------


def _build_subquery(*, region=None, country_code=None, market_id=None):
    """把 search_products 裡那段子查詢重建一次，用來檢查 SQL。

    直接呼叫 search_products 需要 session，這裡只關心 WHERE 組得對不對。
    """
    from sqlalchemy import select

    from app.models.catalog import Market
    from app.models.price import OfficialPrice

    located = (
        select(OfficialPrice.product_id)
        .join(Market, Market.id == OfficialPrice.market_id)
        .where(OfficialPrice.product_id.is_not(None))
    )
    if region:
        located = located.where(Market.region == region)
    if country_code:
        located = located.where(Market.country_code == country_code.upper())
    if market_id is not None:
        located = located.where(OfficialPrice.market_id == market_id)
    return country_scope.apply(located, Market.country_code)


def test_region_adds_market_join() -> None:
    sql = str(_build_subquery(region="Iganga"))
    assert "JOIN markets" in sql
    assert "markets.region" in sql


def test_country_code_is_upper_cased() -> None:
    stmt = _build_subquery(country_code="ug")
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    assert "'UG'" in str(compiled)


def test_market_id_filters_on_price_table() -> None:
    sql = str(_build_subquery(market_id=uuid.uuid4()))
    assert "official_prices.market_id" in sql


def test_filters_compose() -> None:
    sql = str(_build_subquery(region="Iganga", country_code="UG"))
    assert "markets.region" in sql
    assert "markets.country_code" in sql


def test_null_product_id_excluded() -> None:
    """未對照到標準品項的價格列 product_id 是 NULL，不能讓它們影響結果。"""
    sql = str(_build_subquery(region="Iganga"))
    assert "product_id IS NOT NULL" in sql


def test_demo_scope_applies_to_subquery(monkeypatch) -> None:
    """不套 Demo 範圍的話，可以用 country_code 繞過被藏起來的國家。"""
    monkeypatch.setattr(settings, "demo_visible_countries", [])
    monkeypatch.setattr(settings, "demo_hidden_countries", ["TW"])
    country_scope.reset_scope()

    stmt = _build_subquery(country_code="TW")
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # 同時有 = 'TW' 與 NOT IN ('TW')，交集為空 —— 藏起來的國家問不出東西
    assert "NOT IN" in sql.upper()


def test_no_scope_condition_when_inactive(monkeypatch) -> None:
    monkeypatch.setattr(settings, "demo_visible_countries", [])
    monkeypatch.setattr(settings, "demo_hidden_countries", [])
    country_scope.reset_scope()
    sql = str(_build_subquery(region="Iganga")).upper()
    assert "NOT IN" not in sql
