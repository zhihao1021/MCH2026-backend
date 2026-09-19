"""個人檔案與位置的 API schema。

位置有兩種呈現：`LocationOut` 給本人看（完整），
`PublicLocationOut` 給別人看（依 `location_visibility` decide 要露多少）。
兩者分開才不會不小心把住家地址回給所有人。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.data.countries import (
    Country,
    Subdivision,
    default_currency,
    default_unit_system,
    get_country,
    list_subdivisions,
    lookup_subdivision,
    subdivision_children,
)
from app.models.enums import LocationVisibility, UnitSystem, UserRole
from app.models.user import User

# ISO 3166-2：國碼 + 連字號 + 1-3 碼英數
SUBDIVISION_PATTERN = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$")

# `approximate` 模式下座標保留的小數位數。2 位約等於 1.1 公里，
# 足以看出「在哪個區」但無法定位到門牌。
_APPROX_DIGITS = 2

# 地址由大到小書寫的國家（國 → 省 → 市 → 街）。其餘一律由小到大。
# CLDR 沒有收錄地址書寫順序，所以只能維護這份清單；漏掉某個國家的
# 後果只是排列順序不符當地習慣，不會出錯。
_BIG_TO_SMALL_ADDRESS = frozenset({"TW", "JP", "CN", "KR", "HK", "MO", "HU", "VN"})


def _valid_timezones() -> set[str]:
    return available_timezones()


# ---------------------------------------------------------------------------
# 參考資料（前端組表單用）
# ---------------------------------------------------------------------------


class SubdivisionOut(BaseModel):
    code: str = Field(description="ISO 3166-2，例如 TW-YUN、UG-E、UG-203")
    name: str = Field(description="已依請求語系解析好的名稱")
    name_en: str = Field(description="ISO 的羅馬字名稱")
    type: str = Field(description="ISO 的類型，例如 County / District / Region")
    level: int = Field(description="1 = 一級行政區，2 = 其下一層")
    parent_code: str | None = Field(
        default=None, description="level=2 時指向所屬的一級行政區"
    )
    # 這一區底下還有沒有下一層，前端據此決定要不要再顯示一個選單
    has_children: bool = False

    @classmethod
    def from_data(cls, s: Subdivision, locale: str) -> "SubdivisionOut":
        return cls(
            code=s.code,
            name=s.display_name(locale),
            name_en=s.name,
            type=s.type,
            level=s.level,
            parent_code=s.parent_code,
            has_children=bool(s.level == 1 and subdivision_children(s.code)),
        )


class CountryOut(BaseModel):
    code: str = Field(description="ISO 3166-1 alpha-2")
    name: str
    name_en: str
    dialing_code: str
    currency: str
    default_locale: str
    default_timezone: str
    unit_system: UnitSystem
    # 一級行政區在該國叫什麼，直接當表單標籤用
    subdivision_label: str
    subdivision_count: int = 0
    # 有第二層的國家（例如烏干達：4 個 Region 底下有 135 個 District）
    # 才有值。前端據此決定要不要顯示第二個下拉選單
    subdivision_label_level2: str | None = None
    subdivision_count_level2: int = 0
    has_second_level: bool = False
    postal_code_example: str | None = None
    # False 代表 ISO 3166-2 沒有收錄這個國家的行政區（多是小島），
    # 前端請改用自由輸入的 locality
    has_subdivision_data: bool

    @classmethod
    def from_data(cls, c: Country, locale: str) -> "CountryOut":
        return cls(
            code=c.code,
            name=c.display_name(locale),
            name_en=c.name_en,
            dialing_code=c.dialing_code,
            currency=c.currency,
            default_locale=c.default_locale,
            default_timezone=c.default_timezone,
            unit_system=c.unit_system,
            subdivision_label=c.subdivision_label,
            subdivision_count=c.subdivision_count,
            subdivision_label_level2=c.subdivision_label_level2,
            subdivision_count_level2=c.subdivision_count_level2,
            has_second_level=c.has_second_level,
            postal_code_example=c.postal_code_example,
            has_subdivision_data=c.has_subdivision_data,
        )


# ---------------------------------------------------------------------------
# 位置
# ---------------------------------------------------------------------------


class LocationIn(BaseModel):
    """登記位置。整筆取代，沒帶的欄位會被清空。

    只有 `country_code` 是必填——不同國家的地址結構差太多，
    強制要求行政區或郵遞區號會擋掉一部分使用者。
    """

    model_config = ConfigDict(extra="forbid")

    country_code: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2")
    subdivision_code: str | None = Field(
        default=None,
        max_length=8,
        description="ISO 3166-2。僅限已收錄行政區清單的國家（見 GET /v1/geo/countries）",
    )
    locality: str | None = Field(default=None, max_length=120, description="市 / 鎮 / 區")
    address_line: str | None = Field(
        default=None, max_length=200, description="街道地址，只有 visibility=exact 才對外顯示"
    )
    postal_code: str | None = Field(default=None, max_length=16)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: str | None = Field(
        default=None, max_length=64, description="IANA 時區；省略則用該國預設"
    )
    visibility: LocationVisibility = LocationVisibility.REGION

    @field_validator("country_code")
    @classmethod
    def _check_country(cls, v: str) -> str:
        code = v.upper()
        if get_country(code) is None:
            raise ValueError(
                f"尚未支援的國家代碼 {code!r}。"
                "支援清單見 GET /v1/geo/countries；新增一國只需在 app/data/countries.py 補一筆"
            )
        return code

    @field_validator("subdivision_code")
    @classmethod
    def _upper_subdivision(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        code = v.strip().upper()
        if not SUBDIVISION_PATTERN.match(code):
            raise ValueError(f"{code!r} 不是合法的 ISO 3166-2 代碼，格式應為 TW-YUN 這種")
        return code

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        tz = v.strip()
        if tz not in _valid_timezones():
            raise ValueError(f"{tz!r} 不是有效的 IANA 時區")
        return tz

    @field_validator("locality", "address_line", "postal_code")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "LocationIn":
        # 經緯度要嘛都給要嘛都不給，只給一個存下來也沒意義
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude 與 longitude 必須成對提供")

        if self.subdivision_code:
            if not self.subdivision_code.startswith(f"{self.country_code}-"):
                raise ValueError(
                    f"subdivision_code {self.subdivision_code!r} 不屬於國家 {self.country_code!r}"
                )
            if not list_subdivisions(self.country_code):
                raise ValueError(
                    f"ISO 3166-2 沒有收錄 {self.country_code} 的行政區，"
                    "請改用自由輸入的 locality"
                )
            # 一級或二級都接受：烏干達這種國家實際要用的是第二層的 district，
            # 強迫只能填第一層（4 個大區）等於沒有意義
            if lookup_subdivision(self.subdivision_code) is None:
                raise ValueError(
                    f"{self.subdivision_code!r} 不在 {self.country_code} 的行政區清單中"
                )
        return self


class LocationOut(BaseModel):
    """本人視角，完整資料。"""

    country_code: str
    country_name: str
    subdivision_code: str | None = None
    subdivision_name: str | None = None
    locality: str | None = None
    address_line: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    visibility: LocationVisibility
    updated_at: datetime | None = None
    # 組好的單行地址，前端不必自己拼（各國順序不同，這裡由後端決定）
    formatted: str

    @classmethod
    def from_model(cls, user: User, locale: str) -> "LocationOut":
        country = get_country(user.country_code)
        sub = lookup_subdivision(user.subdivision_code)
        return cls(
            country_code=user.country_code,
            country_name=country.display_name(locale) if country else user.country_code,
            subdivision_code=user.subdivision_code,
            subdivision_name=sub.display_name(locale) if sub else None,
            locality=user.locality,
            address_line=user.address_line,
            postal_code=user.postal_code,
            latitude=user.latitude,
            longitude=user.longitude,
            timezone=user.timezone,
            visibility=user.location_visibility,
            updated_at=user.location_updated_at,
            formatted=format_address(user, locale, full=True),
        )


class PublicLocationOut(BaseModel):
    """別人視角。內容依 `location_visibility` 遞減。"""

    country_code: str
    country_name: str
    subdivision_code: str | None = None
    subdivision_name: str | None = None
    locality: str | None = None
    address_line: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # 座標的精度說明，讓前端知道該不該畫成一個點
    precision: str
    formatted: str

    @classmethod
    def from_model(cls, user: User, locale: str) -> "PublicLocationOut":
        vis = user.location_visibility
        country = get_country(user.country_code)
        sub = lookup_subdivision(user.subdivision_code)

        show_region = vis in (
            LocationVisibility.EXACT,
            LocationVisibility.APPROXIMATE,
            LocationVisibility.REGION,
        )
        show_exact = vis is LocationVisibility.EXACT

        lat = lon = None
        precision = "hidden"
        if show_exact and user.latitude is not None:
            lat, lon = user.latitude, user.longitude
            precision = "exact"
        elif vis is LocationVisibility.APPROXIMATE and user.latitude is not None:
            lat = round(user.latitude, _APPROX_DIGITS)
            lon = round(user.longitude, _APPROX_DIGITS) if user.longitude is not None else None
            precision = "approximate_1km"

        return cls(
            country_code=user.country_code,
            country_name=country.display_name(locale) if country else user.country_code,
            subdivision_code=user.subdivision_code if show_region else None,
            subdivision_name=sub.display_name(locale) if (sub and show_region) else None,
            locality=user.locality if show_region else None,
            address_line=user.address_line if show_exact else None,
            latitude=lat,
            longitude=lon,
            precision=precision,
            formatted=format_address(user, locale, full=show_exact, region_only=not show_region),
        )


def format_address(
    user: User, locale: str, *, full: bool = False, region_only: bool = False
) -> str:
    """組成單行地址。

    華語圈與日本是「大到小」（臺灣 臺北市 大安區 …），
    歐美是「小到大」（123 Main St, Springfield, IL, United States）。
    這裡依國家決定順序，前端就不用各自處理。
    """
    country = get_country(user.country_code)
    sub = lookup_subdivision(user.subdivision_code)

    country_name = country.display_name(locale) if country else user.country_code
    if region_only:
        return country_name

    parts: list[str] = [country_name]
    if sub:
        # 選的是第二層（例如烏干達的 district）時，把上一層的大區也補進去，
        # 否則地址會從國家直接跳到一個沒有上下文的地名
        if sub.parent_code:
            parent = lookup_subdivision(sub.parent_code)
            if parent is not None:
                parts.append(parent.display_name(locale))
        parts.append(sub.display_name(locale))
    if user.locality:
        parts.append(user.locality)
    if full and user.address_line:
        parts.append(user.address_line)

    big_to_small = user.country_code in _BIG_TO_SMALL_ADDRESS
    if big_to_small:
        if full and user.postal_code:
            parts.insert(0, user.postal_code)
        return " ".join(p for p in parts if p)

    parts.reverse()  # 變成小到大
    if full and user.postal_code:
        # 郵遞區號插在國名之前，符合英文地址慣例
        parts.insert(len(parts) - 1, user.postal_code)
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# 個人檔案
# ---------------------------------------------------------------------------


class ProfileUpdate(BaseModel):
    """可自行修改的個人檔案欄位。

    `role` 不在這裡：身分在註冊時綁定，要更正得走 `PATCH /v1/admin/users/{id}`。
    位置也不在這裡，走 `PUT /v1/me/location`。
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=80)
    business_name: str | None = Field(default=None, max_length=120, description="農場名 / 商號")
    bio: str | None = Field(default=None, max_length=500)
    avatar_url: str | None = Field(default=None, max_length=500)
    website_url: str | None = Field(default=None, max_length=500)
    locale: str | None = Field(default=None, max_length=16)
    preferred_currency: str | None = Field(default=None, min_length=3, max_length=3)
    unit_system: UnitSystem | None = None
    contact_phone_public: bool | None = Field(
        default=None, description="新報價預設是否公開電話"
    )

    @field_validator("preferred_currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        if not v:
            return None
        code = v.strip().upper()
        if not code.isalpha():
            raise ValueError("preferred_currency 必須是 ISO 4217 的三個字母，例如 TWD")
        return code

    @field_validator("avatar_url", "website_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        url = v.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("網址必須以 http:// 或 https:// 開頭")
        return url

    @field_validator("display_name", "business_name", "bio")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class PublicUserOut(BaseModel):
    """公開的個人檔案。給報價清單點進去看賣家用。

    電話不在這裡：要不要露出電話是「每一筆報價」的決定，
    見 `QuoteOut.seller`。
    """

    id: uuid.UUID
    display_name: str | None = None
    business_name: str | None = None
    role: UserRole
    bio: str | None = None
    avatar_url: str | None = None
    website_url: str | None = None
    location: PublicLocationOut
    active_quote_count: int = 0
    member_since: datetime

    @classmethod
    def from_model(cls, user: User, locale: str, *, active_quotes: int = 0) -> "PublicUserOut":
        return cls(
            id=user.id,
            display_name=user.display_name,
            business_name=user.business_name,
            role=user.role,
            bio=user.bio,
            avatar_url=user.avatar_url,
            website_url=user.website_url,
            location=PublicLocationOut.from_model(user, locale),
            active_quote_count=active_quotes,
            member_since=user.created_at,
        )


def effective_currency(user: User) -> str:
    """使用者沒指定幣別時，依所在國家推出來。"""
    return user.preferred_currency or default_currency(user.country_code)


def effective_unit_system(user: User) -> UnitSystem:
    """同上：沒指定就跟著國家走（美國是英制，其餘公制）。"""
    return user.unit_system or default_unit_system(user.country_code)
