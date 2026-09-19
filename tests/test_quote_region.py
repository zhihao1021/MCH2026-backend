"""報價的地區篩選。

背景：`quotes.region` 是**顯示字串**，而且來源不只一個——
`create_quote` 寫的是 ISO 行政區顯示名（`臺北市`），批次 seed 寫的是
資料源自訂的市場地區名（`台北市`）。兩者字面不相等，用等值比對會漏掉
一半資料，使用者就會看到「我明明報價成功了卻不在列表裡」。

所以：精確篩選用 `subdivision_code`（ISO 3166-2），
`region` 只做寬鬆比對。
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.services.quotes import _region_regex, list_quote_regions, list_quotes


def test_list_quotes_accepts_subdivision_code() -> None:
    sig = inspect.signature(list_quotes)
    assert "subdivision_code" in sig.parameters
    assert sig.parameters["subdivision_code"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["subdivision_code"].default is None


def test_quote_regions_helper_exists() -> None:
    """前端的地區選單要有東西可用，而且必須是報價實際存在的地區。"""
    assert inspect.iscoroutinefunction(list_quote_regions)


# ---------------------------------------------------------------------------
# 異體字比對
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "stored", "should_match"),
    [
        ("臺北市", "臺北市", True),
        ("臺北市", "台北市", True),   # 這組是整個 bug 的核心
        ("台北市", "臺北市", True),
        ("台北市", "台北市", True),
        ("台南市", "臺南市", True),
        ("台中市", "台北市", False),
        ("宜蘭縣", "宜蘭縣", True),
        ("Iganga", "Iganga", True),
        ("Iganga", "Mbale", False),
    ],
)
def test_region_regex_matches_variants(query: str, stored: str, should_match: bool) -> None:
    # PostgreSQL 的 ~* 與 Python 的 re 在這個樣式上語意一致
    assert bool(re.match(_region_regex(query), stored)) is should_match


def test_region_regex_is_anchored() -> None:
    """不能讓「台北」意外命中「台北縣某某市」。"""
    pattern = _region_regex("台北市")
    assert pattern.startswith("^") and pattern.endswith("$")
    assert not re.match(pattern, "新台北市")
    assert not re.match(pattern, "台北市中正區")


def test_region_regex_escapes_metacharacters() -> None:
    """地名理論上不含正規表達式的特殊字元，但輸入是使用者給的。"""
    pattern = _region_regex("a.b")
    assert re.match(pattern, "a.b")
    assert not re.match(pattern, "axb")


def test_region_regex_strips_whitespace() -> None:
    assert _region_regex("  宜蘭縣  ") == _region_regex("宜蘭縣")


def test_region_regex_handles_empty() -> None:
    assert _region_regex("") == "^$"
