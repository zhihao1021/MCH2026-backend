"""國家與行政區的參考資料端點。

前端要讓使用者登記位置，就得知道「這個國家的一級行政區有哪些」、
「電話國碼是多少」、「預設幣別是什麼」。把這些交給後端，
前端就不必為每個國家各寫一份硬編碼的清單。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import Locale
from app.core.errors import NotFoundError
from app.data.countries import get_country, list_countries, list_subdivisions
from app.schemas.profile import CountryOut, SubdivisionOut

router = APIRouter(prefix="/geo", tags=["geo"])


@router.get("/countries", response_model=list[CountryOut], summary="支援的國家清單")
async def countries(locale: Locale) -> list[CountryOut]:
    """回傳所有支援的國家與其預設值。

    `has_subdivision_data=false` 的國家還沒收錄行政區清單，
    前端請改用自由輸入的 `locality` 欄位。
    """
    return [CountryOut.from_data(c, locale) for c in list_countries()]


@router.get("/countries/{code}", response_model=CountryOut, summary="單一國家")
async def country(code: str, locale: Locale) -> CountryOut:
    data = get_country(code)
    if data is None:
        raise NotFoundError(f"尚未支援的國家代碼 {code.upper()!r}", code="unsupported_country")
    return CountryOut.from_data(data, locale)


@router.get(
    "/countries/{code}/subdivisions",
    response_model=list[SubdivisionOut],
    summary="一級行政區（ISO 3166-2）",
)
async def subdivisions(code: str, locale: Locale) -> list[SubdivisionOut]:
    """回空陣列代表這個國家還沒收錄清單，不是錯誤。"""
    data = get_country(code)
    if data is None:
        raise NotFoundError(f"尚未支援的國家代碼 {code.upper()!r}", code="unsupported_country")
    return [SubdivisionOut.from_data(s, locale) for s in list_subdivisions(data.code)]
