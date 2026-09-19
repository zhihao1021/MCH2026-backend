"""收藏作物的測試。

需要資料庫的部分（加入 / 取消 / 上限 / 價格聚合）由
`scripts/smoke_test.py` 對真實服務跑過；這裡測不需要 DB 的形狀與規則。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.catalog import Product, ProductName
from app.models.enums import ProductCategory
from app.schemas.favorite import FavoriteListOut, FavoriteOut, FavoritePriceOut
from app.services.favorites import (
    LOOKBACK_DAYS,
    MAX_FAVORITES,
    FavoriteItem,
    FavoritePrice,
    _q,
)


def _product(slug: str = "cabbage", zh: str = "高麗菜") -> Product:
    p = Product(
        slug=slug, category=ProductCategory.VEGETABLE, default_unit="kg", popularity=100
    )
    p.id = uuid.uuid4()
    p.names = [
        ProductName(locale="zh-Hant", name=zh, is_primary=True),
        ProductName(locale="en", name="Cabbage", is_primary=True),
    ]
    return p


# ---------------------------------------------------------------------------
# 設定值
# ---------------------------------------------------------------------------


def test_limits_are_sane() -> None:
    # 功能機一頁列不完太多收藏；上限要存在但不能小到沒用
    assert 5 <= MAX_FAVORITES <= 100
    # 各市場休市日不同，只看今天多半查不到
    assert LOOKBACK_DAYS >= 7


# ---------------------------------------------------------------------------
# 價格四捨五入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (Decimal("21.163"), Decimal("21.16")),
        (Decimal("21.166"), Decimal("21.17")),
        # Decimal.quantize 預設是 ROUND_HALF_EVEN（銀行家捨入），
        # 所以 .165 進到偶數的 .16 而不是 .17。與 prices.py 的 _q 行為一致，
        # 兩邊顯示的價格才不會差一分。
        (Decimal("21.165"), Decimal("21.16")),
        (Decimal("21.175"), Decimal("21.18")),
        (0, Decimal("0.00")),
        (1450, Decimal("1450.00")),
    ],
)
def test_quantize(value, expected) -> None:
    assert _q(value) == expected


# ---------------------------------------------------------------------------
# 回應形狀
# ---------------------------------------------------------------------------


def test_favorite_out_with_price() -> None:
    item = FavoriteItem(
        product=_product(),
        favorited_at=datetime(2026, 9, 20, tzinfo=UTC),
        latest=FavoritePrice(
            trade_date=date(2026, 9, 19),
            price_avg=Decimal("21.16"),
            currency="TWD",
            unit="kg",
            market_name="三重區",
            market_count=12,
            change_pct=-22.02,
        ),
    )
    out = FavoriteOut.from_item(item, "zh-Hant")
    assert out.product.name == "高麗菜"
    assert out.latest is not None
    assert out.latest.price_avg == Decimal("21.16")
    assert out.latest.market_count == 12
    assert out.latest.change_pct == -22.02


def test_favorite_out_localises_name() -> None:
    item = FavoriteItem(product=_product(), favorited_at=datetime.now(UTC), latest=None)
    assert FavoriteOut.from_item(item, "en").product.name == "Cabbage"


def test_favorite_out_without_price() -> None:
    """非產季或該國沒有資料源時 latest 會是 null，不能因此爆掉。"""
    item = FavoriteItem(product=_product(), favorited_at=datetime.now(UTC), latest=None)
    out = FavoriteOut.from_item(item, "zh-Hant")
    assert out.latest is None


def test_favorite_price_allows_null_change() -> None:
    """只有一天資料時算不出漲跌。"""
    p = FavoritePriceOut(
        trade_date=date(2026, 9, 19),
        price_avg=Decimal("100"),
        currency="UGX",
        unit="kg",
        market_name="Mbale",
        market_count=1,
    )
    assert p.change_pct is None


def test_favorite_price_allows_null_avg() -> None:
    """來源只給高低價時 price_avg 可能是 null。"""
    p = FavoritePriceOut(
        trade_date=date(2026, 9, 19),
        currency="UGX",
        unit="kg",
        market_name="Mbale",
        market_count=1,
    )
    assert p.price_avg is None


def test_favorite_price_requires_market_count() -> None:
    with pytest.raises(ValidationError):
        FavoritePriceOut(
            trade_date=date(2026, 9, 19), currency="UGX", unit="kg", market_name="Mbale"
        )  # type: ignore[call-arg]


def test_list_out_reports_limit_and_country() -> None:
    out = FavoriteListOut(items=[], total=0, limit=MAX_FAVORITES, country_code="UG")
    assert out.limit == MAX_FAVORITES
    assert out.country_code == "UG"
    assert out.items == []
