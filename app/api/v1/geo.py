"""國家與行政區的參考資料端點。

前端要讓使用者登記位置，就得知道「這個國家的一級行政區有哪些」、
「電話國碼是多少」、「預設幣別是什麼」。把這些交給後端，
前端就不必為每個國家各寫一份硬編碼的清單。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import Locale
from app.core.errors import NotFoundError
from app.data.countries import get_country, list_countries, list_subdivisions
from app.schemas.profile import CountryOut, SubdivisionOut

router = APIRouter(prefix="/geo", tags=["geo"])


@router.get("/countries", response_model=list[CountryOut], summary="支援的國家清單")
async def countries(locale: Locale) -> list[CountryOut]:
    """回傳所有支援的國家與其預設值（ISO 3166-1，242 個）。

    依請求語系的國名排序，可直接餵給下拉選單。

    `has_subdivision_data=false` 的國家 ISO 3166-2 沒有收錄行政區（多是小島），
    前端請改用自由輸入的 `locality` 欄位。
    """
    return [CountryOut.from_data(c, locale) for c in list_countries(locale)]


@router.get("/countries/{code}", response_model=CountryOut, summary="單一國家")
async def country(code: str, locale: Locale) -> CountryOut:
    data = get_country(code)
    if data is None:
        raise NotFoundError(f"尚未支援的國家代碼 {code.upper()!r}", code="unsupported_country")
    return CountryOut.from_data(data, locale)


@router.get(
    "/countries/{code}/subdivisions",
    response_model=list[SubdivisionOut],
    summary="行政區（ISO 3166-2）",
)
async def subdivisions(
    code: str,
    locale: Locale,
    parent: Annotated[
        str | None,
        Query(description="只列這個一級行政區底下的下一層，例如 parent=UG-E"),
    ] = None,
    level: Annotated[
        int | None,
        Query(ge=1, le=2, description="1 = 一級行政區（預設），2 = 全國的第二層"),
    ] = None,
) -> list[SubdivisionOut]:
    """預設只回一級行政區。

    有些國家的一級行政區太粗，實際要用的在第二層——例如烏干達的一級是
    4 個 Region，真正的行政單位是底下 135 個 District。這種國家的
    `has_second_level` 為 true，可以用 `parent=UG-E` 往下鑽，
    或 `level=2` 一次取得全國的 District。

    回空陣列代表這個國家 ISO 3166-2 沒有收錄，不是錯誤。
    """
    data = get_country(code)
    if data is None:
        raise NotFoundError(f"尚未支援的國家代碼 {code.upper()!r}", code="unsupported_country")
    items = list_subdivisions(data.code, parent=parent, level=level)
    return [SubdivisionOut.from_data(s, locale) for s in items]
