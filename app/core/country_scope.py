"""Demo 用的國家範圍過濾。

資料庫裡同時有台灣（七萬多筆行情、22 個市場）與烏干達的資料。
Demo 給烏干達的使用者看時，台灣那一大堆會把畫面洗掉，
所以需要一個開關把它整個藏起來——但**不是刪掉**，
只是讀取層不回傳，關掉開關就全部回來。

`.env` 兩個設定，白名單優先：

    DEMO_VISIBLE_COUNTRIES=UG     # 只顯示烏干達
    DEMO_HIDDEN_COUNTRIES=TW      # 或者只藏掉台灣

兩個都留空（預設）就是不過濾。

## 過濾範圍

只影響**讀取**：市場、官方行情、地區清單、民間報價、資料來源。

**不影響**註冊與寫入——台灣使用者照樣能註冊、報價，只是在
Demo 設定下看不到自己的資料。這是刻意的：過濾是展示用的視角，
不是權限控制，更不該讓人以為資料不見了。

也不影響 `/v1/geo/countries`：那是給使用者選國家的參考清單，
藏起來會讓人沒辦法填自己的所在地。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import ColumnElement

from app.core.config import settings


@dataclass(frozen=True)
class CountryScope:
    visible: frozenset[str]
    hidden: frozenset[str]

    @property
    def is_active(self) -> bool:
        return bool(self.visible or self.hidden)

    def allows(self, country_code: str | None) -> bool:
        if not self.is_active:
            return True
        if country_code is None:
            # 沒有國家資訊的資料在有白名單時一律藏起來——
            # 寧可少顯示，也不要在 Demo 中冒出來路不明的東西
            return not self.visible
        code = country_code.upper()
        if self.visible:
            return code in self.visible
        return code not in self.hidden

    def describe(self) -> dict[str, list[str] | bool]:
        return {
            "active": self.is_active,
            "visible_countries": sorted(self.visible),
            "hidden_countries": sorted(self.hidden),
        }


@lru_cache(maxsize=1)
def get_scope() -> CountryScope:
    return CountryScope(
        visible=frozenset(c.upper() for c in settings.demo_visible_countries),
        hidden=frozenset(c.upper() for c in settings.demo_hidden_countries),
    )


def reset_scope() -> None:
    """測試用：改了 settings 之後要呼叫，否則會拿到快取的舊值。"""
    get_scope.cache_clear()


def condition(column: ColumnElement) -> ColumnElement | None:
    """回傳要加進 WHERE 的條件；沒設定範圍就回 None。

    用法：

        cond = country_scope.condition(Market.country_code)
        if cond is not None:
            stmt = stmt.where(cond)
    """
    scope = get_scope()
    if not scope.is_active:
        return None
    if scope.visible:
        return column.in_(sorted(scope.visible))
    return column.notin_(sorted(scope.hidden))


def apply(stmt, column: ColumnElement):
    """把範圍條件加到 statement 上。沒設定就原樣回傳。"""
    cond = condition(column)
    return stmt if cond is None else stmt.where(cond)
